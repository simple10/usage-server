"""JSON API and UI (SPEC §5.5, §5.7, §5.8)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from . import queries
from .config import Settings
from .db import Database
from .prices import Prices
from .spool import SpoolReader

log = logging.getLogger("usage_server.api")

UI_FILE = Path(__file__).parent / "ui" / "index.html"
BRIDGE_HEADER = "x-usage-bridge-token"
LABEL_KINDS = ("api_key", "auth")


class ManagementKeyCheck:
    """Validates a proxy management key by calling a cheap management endpoint.
    Answers are cached for 5 minutes, keyed by a hash of the key."""

    def __init__(self, cpa_url: str, ttl: float = 300, opener: Callable | None = None):
        self.url = cpa_url.rstrip("/") + "/v0/management/debug" if cpa_url else ""
        self.ttl = ttl
        self._cache: dict[str, tuple[bool, float]] = {}
        self._lock = threading.Lock()
        self._open = opener or (lambda req: urllib.request.urlopen(req, timeout=5))

    def check(self, key: str) -> bool:
        if not self.url or not key:
            return False
        h = hashlib.sha256(key.encode()).hexdigest()
        now = time.time()
        with self._lock:
            hit = self._cache.get(h)
            if hit and now - hit[1] < self.ttl:
                return hit[0]
        ok = False
        try:
            req = urllib.request.Request(self.url, headers={"Authorization": f"Bearer {key}"})
            with self._open(req) as resp:
                ok = 200 <= resp.status < 300
        except urllib.error.HTTPError as e:
            ok = False
            if e.code >= 500:
                return False  # do not cache server trouble
        except Exception:
            return False  # unreachable: do not cache
        with self._lock:
            if len(self._cache) > 1000:
                self._cache.clear()
            self._cache[h] = (ok, now)
        return ok


def _eq(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


class App:
    def __init__(self, settings: Settings, db: Database, prices: Prices, spool: SpoolReader | None,
                 scanner: Any = None, mgmt_check: ManagementKeyCheck | None = None):
        self.s = settings
        self.db = db
        self.prices = prices
        self.spool = spool
        self.scanner = scanner
        self.mgmt = mgmt_check or ManagementKeyCheck(settings.cpa_url)
        self.started = time.time()

    # -- auth --
    def authorized(self, request: Request) -> bool:
        bt = request.headers.get(BRIDGE_HEADER, "")
        if bt:
            return _eq(bt, self.s.bridge_token)
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return False
        token = auth[7:].strip()
        if any(_eq(token, t) for t in self.s.api_tokens):
            return True
        return self.mgmt.check(token)

    def route(self, fn):
        async def handler(request: Request) -> Response:
            body = await request.body() if request.method == "POST" else b""
            if len(body) > 1_000_000:
                return JSONResponse({"error": "body too large"}, status_code=413)
            if not await run_in_threadpool(self.authorized, request):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            try:
                return await run_in_threadpool(fn, request, body)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            except Exception:
                log.exception("api error on %s", request.url.path)
                return JSONResponse({"error": "internal error"}, status_code=500)
        return handler

    def _params(self, request: Request) -> tuple[float, float, dict[str, str]]:
        p = dict(request.query_params)
        frm, to = queries.time_range(p)
        filters = {k: p[k] for k in queries.FILTERS if p.get(k)}
        if p.get("source") in queries.SOURCES:
            filters["source"] = p["source"]
        return frm, to, filters

    # -- endpoints --
    def health(self, request: Request, body: bytes = b"") -> Response:
        out: dict[str, Any] = {"ok": True, "version": os.environ.get("USAGE_SERVER_VERSION", "dev"), "uptime_s": round(time.time() - self.started), "server_time": time.time()}
        if self.spool:
            st = self.spool.plugin_status()
            out["plugin"] = st
            out["spool"] = {**self.spool.stats, **self.spool.lag()}
            if st and st.get("updated_at"):
                from .spool import parse_ts
                ts = parse_ts(st["updated_at"])
                out["plugin_age_s"] = round(time.time() - ts) if ts else None
        out["prices"] = self.prices.status()
        if self.scanner:
            out["parser"] = self.scanner.status()
        with self.db.read() as r:
            row = r.execute("SELECT COUNT(*), MAX(ts) FROM requests").fetchone()
            out["requests"] = {"count": row[0], "last_ts": row[1]}
        return JSONResponse(out)

    def summary(self, request: Request, body: bytes = b"") -> Response:
        frm, to, f = self._params(request)
        with self.db.read() as r:
            return JSONResponse(queries.summary(r, self.prices, frm, to, f))

    def breakdown(self, request: Request, body: bytes = b"") -> Response:
        frm, to, f = self._params(request)
        group = request.query_params.get("group", "model")
        if group not in ("api_key", "auth", "provider", "model", "session", "project"):
            raise ValueError("group must be api_key, auth, provider, model, session or project")
        limit = int(request.query_params.get("limit", "100") or 100)
        sort = request.query_params.get("sort", "cost")
        if sort not in ("cost", "requests", "input", "output", "cache_read", "failed"):
            raise ValueError("bad sort")
        with self.db.read() as r:
            rows = queries.rows_for(r, self.prices, group, frm, to, f, limit=min(max(limit, 1), 1000), sort=sort)
        return JSONResponse({"group": group, "from": frm, "to": to, "rows": rows})

    def timeseries(self, request: Request, body: bytes = b"") -> Response:
        frm, to, f = self._params(request)
        q = request.query_params
        group = q.get("group") or None
        if group and group not in ("api_key", "auth", "provider", "model", "session", "project"):
            raise ValueError("bad group")
        top = min(max(int(q.get("top", "6") or 6), 1), 20)
        metric = q.get("metric", "cost")
        if metric not in ("cost", "tokens"):
            raise ValueError("metric must be cost or tokens")
        with self.db.read() as r:
            return JSONResponse(queries.timeseries(r, self.prices, frm, to, q.get("bucket", "day"), group, top, f, metric))

    def sessions(self, request: Request, body: bytes = b"") -> Response:
        frm, to, _ = self._params(request)
        q = request.query_params
        with self.db.read() as r:
            rows = queries.sessions_list(r, self.prices, frm, to, q.get("q", ""), q.get("harness", ""),
                                         q.get("has_transcript", ""), min(int(q.get("limit", "200") or 200), 2000))
        return JSONResponse({"from": frm, "to": to, "rows": rows})

    def session(self, request: Request, body: bytes = b"") -> Response:
        sid = request.query_params.get("id", "").strip()
        if not sid:
            raise ValueError("id is required")
        with self.db.read() as r:
            d = queries.session_detail(r, self.prices, sid)
        if d is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(d)

    def reparse(self, request: Request, body: bytes = b"") -> Response:
        sid = request.query_params.get("id", "").strip()
        if not sid:
            raise ValueError("id is required")
        if not self.scanner:
            return JSONResponse({"error": "transcript scanner not running"}, status_code=503)
        queued = self.scanner.request_reparse(sid)
        return JSONResponse({"queued": queued}, status_code=202 if queued else 404)

    def labels_get(self, request: Request, body: bytes = b"") -> Response:
        with self.db.read() as r:
            rows = [dict(x) for x in r.execute("SELECT kind, key, label FROM labels ORDER BY kind, key")]
        return JSONResponse({"labels": rows})

    def labels_post(self, request: Request, body: bytes = b"") -> Response:
        try:
            data = json.loads(body or b"null")
        except ValueError:
            raise ValueError("body must be JSON")
        items = data if isinstance(data, list) else [data]
        with self.db.write() as w:
            for it in items:
                if not isinstance(it, dict):
                    raise ValueError("each label must be an object")
                kind, key, label = it.get("kind"), it.get("key"), it.get("label")
                if kind not in LABEL_KINDS or not isinstance(key, str) or not key or len(key) > 256:
                    raise ValueError("kind must be api_key or auth, key a non-empty string")
                if label is None or (isinstance(label, str) and not label.strip()):
                    w.execute("DELETE FROM labels WHERE kind = ? AND key = ?", (kind, key))
                elif isinstance(label, str) and len(label) <= 200:
                    w.execute("INSERT INTO labels(kind, key, label) VALUES (?, ?, ?) "
                              "ON CONFLICT(kind, key) DO UPDATE SET label = excluded.label", (kind, key, label.strip()))
                else:
                    raise ValueError("label must be a string of at most 200 characters")
        return self.labels_get(request)

    def prices_get(self, request: Request, body: bytes = b"") -> Response:
        q = request.query_params
        with self.db.read() as r:
            used = []
            for row in r.execute("SELECT COALESCE(provider,'') p, COALESCE(model,'') m, COUNT(*) n FROM requests GROUP BY p, m ORDER BY n DESC"):
                p = self.prices.lookup(row["p"], row["m"])
                used.append({"provider": row["p"], "model": row["m"], "requests": row["n"],
                             "price": None if p is None else {"provider": p.provider, "model": p.model, "input": p.input,
                                                              "output": p.output, "cache_read": p.cache_read,
                                                              "cache_write": p.cache_write, "tiers": p.tiers}})
            unknown = queries.unknown_models(r, self.prices)
            table = None
            if q.get("all") in ("1", "true"):
                table = [dict(x) for x in r.execute("SELECT provider, model, input, output, cache_read, cache_write FROM prices ORDER BY provider, model")]
        return JSONResponse({"status": self.prices.status(), "used": used, "unknown": unknown, "table": table})

    # -- UI --
    async def ui(self, request: Request) -> Response:
        try:
            html = UI_FILE.read_text()
        except OSError:
            return HTMLResponse("<h1>UI missing</h1>", status_code=500)
        return HTMLResponse(html, headers={
            "Cache-Control": "no-cache",
            "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'",
        })

    def starlette(self) -> Starlette:
        r = self.route
        routes = [
            Route("/", lambda req: RedirectResponse("/ui/")),
            Route("/ui/", self.ui),
            Route("/ui", lambda req: RedirectResponse("/ui/")),
            Route("/healthz", lambda req: JSONResponse({"ok": True})),
            Route("/api/health", r(self.health)),
            Route("/api/summary", r(self.summary)),
            Route("/api/breakdown", r(self.breakdown)),
            Route("/api/timeseries", r(self.timeseries)),
            Route("/api/sessions", r(self.sessions)),
            Route("/api/session", r(self.session)),
            Route("/api/session/reparse", r(self.reparse), methods=["POST"]),
            Route("/api/labels", r(self.labels_get), methods=["GET"]),
            Route("/api/labels", r(self.labels_post), methods=["POST"]),
            Route("/api/prices", r(self.prices_get)),
        ]
        return Starlette(routes=routes)


def dumps(o: Any) -> str:
    return json.dumps(o, separators=(",", ":"))
