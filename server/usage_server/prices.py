"""models.dev price sync, price lookup and cost (SPEC §5.6).

Prices are dollars per million tokens. Costs are never stored: they are
computed when a request is served, so a price update also corrects old data.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .db import Database

log = logging.getLogger("usage_server.prices")

PREFERRED = ("anthropic", "openai", "google", "xai", "deepseek", "mistral")
_SUFFIX = re.compile(r"\[[^\]]*\]$")
_DATE = re.compile(r"(-\d{8}|-\d{4}-\d{2}-\d{2}|@\d{8})$")


@dataclass
class Price:
    provider: str
    model: str
    input: float = 0.0
    output: float = 0.0
    cache_read: float | None = None
    cache_write: float | None = None
    tiers: list[dict[str, Any]] = field(default_factory=list)  # sorted by size

    def for_prompt(self, prompt_tokens: int) -> "Price":
        """The price that applies to one request with this many prompt tokens."""
        chosen = None
        for t in self.tiers:
            if prompt_tokens > t["size"]:
                chosen = t
        if chosen is None:
            return self
        scale = (chosen.get("input") or self.input) / self.input if self.input else 1.0
        return Price(
            self.provider, self.model,
            input=chosen.get("input", self.input),
            output=chosen.get("output", self.output),
            cache_read=chosen.get("cache_read", (self.cache_read or 0) * scale if self.cache_read is not None else None),
            cache_write=chosen.get("cache_write", (self.cache_write or 0) * scale if self.cache_write is not None else None),
        )


def cost(p: Price, input: int, output: int, cache_read: int, cache_write_5m: int, cache_write_1h: int) -> float:
    """cost = input·in + output·out + cache_read·cr + cw5m·cw + cw1h·(2·in), in dollars.

    A missing cache_read price falls back to the input price; a missing
    cache_write price falls back to 1.25 × input (Anthropic's 5-minute rate).
    """
    cr = p.cache_read if p.cache_read is not None else p.input
    cw = p.cache_write if p.cache_write is not None else p.input * 1.25
    return (input * p.input + output * p.output + cache_read * cr + cache_write_5m * cw + cache_write_1h * 2 * p.input) / 1e6


def normalize_model(model: str) -> list[str]:
    """Candidate model names, most specific first."""
    m = (model or "").strip().lower()
    m = _SUFFIX.sub("", m).strip()
    if "/" in m:
        m = m.rsplit("/", 1)[1]
    out = [m]
    base = _DATE.sub("", m)
    if base != m:
        out.append(base)
    # "claude-haiku-4-5" may be listed as "claude-haiku-4.5" by some providers.
    dotted = re.sub(r"(\d)-(\d)(?!\d)", r"\1.\2", base)
    if dotted not in out:
        out.append(dotted)
    return [c for c in out if c]


def parse_models_dev(data: dict[str, Any]) -> list[tuple]:
    """Rows (provider, model, input, output, cache_read, cache_write, tiers_json)."""
    rows = []
    if not isinstance(data, dict):
        return rows
    for pid, prov in data.items():
        models = prov.get("models") if isinstance(prov, dict) else None
        if not isinstance(models, dict):
            continue
        for mid, m in models.items():
            c = m.get("cost") if isinstance(m, dict) else None
            if not isinstance(c, dict) or not isinstance(c.get("input"), (int, float)) or not isinstance(c.get("output"), (int, float)):
                continue
            tiers = []
            for t in c.get("tiers") or []:
                size = (t.get("tier") or {}).get("size") if isinstance(t, dict) else None
                if isinstance(size, (int, float)):
                    tiers.append({k: t[k] for k in ("input", "output", "cache_read", "cache_write") if isinstance(t.get(k), (int, float))} | {"size": int(size)})
            if not tiers and isinstance(c.get("context_over_200k"), dict):
                t = c["context_over_200k"]
                tiers.append({k: t[k] for k in ("input", "output", "cache_read", "cache_write") if isinstance(t.get(k), (int, float))} | {"size": 200_000})
            tiers.sort(key=lambda t: t["size"])
            rows.append((
                str(pid).lower(), str(mid).lower(), float(c["input"]), float(c["output"]),
                float(c["cache_read"]) if isinstance(c.get("cache_read"), (int, float)) else None,
                float(c["cache_write"]) if isinstance(c.get("cache_write"), (int, float)) else None,
                json.dumps(tiers) if tiers else None,
            ))
    return rows


class Prices:
    def __init__(self, db: Database, url: str, provider_map: dict[str, str]):
        self.db = db
        self.url = url
        self.provider_map = provider_map
        self._cache: dict[tuple[str, str], Price | None] = {}
        self._table: dict[str, dict[str, Price]] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    # -- sync --
    def sync(self, fetch=None) -> int:
        """Fetch models.dev and replace the price table. Keeps the old table on failure."""
        try:
            if fetch is not None:
                data = fetch()
            else:
                req = urllib.request.Request(self.url, headers={"User-Agent": "usage-server"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read(64 * 1024 * 1024))
            rows = parse_models_dev(data)
            if not rows:
                raise ValueError("no prices in response")
        except Exception as e:  # keep last good copy
            self.last_error = f"{type(e).__name__}: {e}"[:300]
            log.warning("price sync failed: %s", self.last_error)
            return 0
        now = time.time()
        with self.db.write() as w:
            w.execute("DELETE FROM prices")
            w.executemany(
                "INSERT OR REPLACE INTO prices(provider, model, input, output, cache_read, cache_write, tiers, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [r + (now,) for r in rows],
            )
            self.db.kv_set("prices_synced_at", str(now), conn=w)
        self.last_error = None
        self.invalidate()
        log.info("synced %d prices", len(rows))
        return len(rows)

    def start(self, interval: float = 86400) -> None:
        self._thread = threading.Thread(target=self._loop, args=(interval,), name="price-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self, interval: float) -> None:
        last = float(self.db.kv_get("prices_synced_at", "0") or 0)
        with self.db.read() as r:
            empty = r.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 0
        if empty or time.time() - last > interval:
            self.sync()
        while not self._stop.wait(min(interval, 3600)):
            last = float(self.db.kv_get("prices_synced_at", "0") or 0)
            if time.time() - last > interval:
                self.sync()

    # -- lookup --
    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
            self._table = None

    def _load(self) -> dict[str, dict[str, Price]]:
        with self._lock:
            if self._table is not None:
                return self._table
        table: dict[str, dict[str, Price]] = {}
        with self.db.read() as r:
            for row in r.execute("SELECT * FROM prices"):
                tiers = json.loads(row["tiers"]) if row["tiers"] else []
                table.setdefault(row["provider"], {})[row["model"]] = Price(
                    row["provider"], row["model"], row["input"], row["output"], row["cache_read"], row["cache_write"], tiers
                )
        with self._lock:
            self._table = table
        return table

    def lookup(self, provider: str | None, model: str | None) -> Price | None:
        key = ((provider or "").lower(), (model or "").lower())
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        table = self._load()
        mapped = self.provider_map.get(key[0], key[0])
        cands = normalize_model(key[1])
        found = None
        if cands:
            order = [mapped] + [p for p in PREFERRED if p != mapped] + sorted(p for p in table if p != mapped and p not in PREFERRED)
            for prov in order:  # all name variants in one provider before the next
                models = table.get(prov, {})
                for cand in cands:
                    if cand in models:
                        found = models[cand]
                        break
                if found:
                    break
        with self._lock:
            self._cache[key] = found
        return found

    def status(self) -> dict[str, Any]:
        with self.db.read() as r:
            n = r.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        synced = self.db.kv_get("prices_synced_at")
        return {"count": n, "synced_at": float(synced) if synced else None, "last_error": self.last_error}
