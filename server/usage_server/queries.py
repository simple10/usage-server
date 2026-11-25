"""Read-side analytics: aggregation by dimension, with cost computed at read time."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from .prices import Price, Prices, cost

TOKEN_COLS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")

def root_session(col: str = "requests.session_id") -> str:
    """SQL for the root session: "<root>:agent:<id>" collapses to <root>."""
    return (f"CASE WHEN instr({col}, ':agent:') > 0 "
            f"THEN substr({col}, 1, instr({col}, ':agent:') - 1) ELSE {col} END")


ROOT_SESSION = root_session()
# Transcript key: the root session without its "<harness>:" prefix.
TRANSCRIPT_KEY = (
    f"CASE WHEN instr({ROOT_SESSION}, ':') > 0 "
    f"THEN substr({ROOT_SESSION}, instr({ROOT_SESSION}, ':') + 1) ELSE {ROOT_SESSION} END"
)
HARNESS = (
    f"CASE WHEN instr({ROOT_SESSION}, ':') > 0 "
    f"THEN substr({ROOT_SESSION}, 1, instr({ROOT_SESSION}, ':') - 1) ELSE NULL END"
)

TRANSCRIPT_GROUP = "~transcript"  # API key / credential group for transcript usage

GROUPS: dict[str, str] = {
    "api_key": f"CASE WHEN src = 'transcript' THEN '{TRANSCRIPT_GROUP}' ELSE COALESCE(api_key_hash, '') END",
    "auth": f"CASE WHEN src = 'transcript' THEN '{TRANSCRIPT_GROUP}' ELSE COALESCE(requests.auth_id, '') END",
    "source": "src",
    "provider": "COALESCE(provider, '')",
    "model": "COALESCE(model, '')",
    "session": f"COALESCE({ROOT_SESSION}, '')",
    "project": "COALESCE((SELECT s.project FROM sessions s WHERE s.harness = " + HARNESS +
               " AND s.session_id = " + TRANSCRIPT_KEY + "), '')",
    "day": "strftime('%Y-%m-%d', ts, 'unixepoch')",
    "hour": "strftime('%Y-%m-%dT%H:00', ts, 'unixepoch')",
}
FILTERS: dict[str, str] = {
    "api_key": GROUPS["api_key"] + " = ?",
    "auth": GROUPS["auth"] + " = ?",
    "provider": "provider = ?",
    "model": "model = ?",
    "session": f"{ROOT_SESSION} = ?",
    "project": GROUPS["project"] + " = ?",
}


def parse_time(v: str | None, default: float) -> float:
    if v is None or str(v).strip() == "":
        return default
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        pass
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def time_range(params: dict[str, str]) -> tuple[float, float]:
    now = time.time()
    to = parse_time(params.get("to"), now)
    frm = parse_time(params.get("from"), to - 7 * 86400)
    return frm, to


SOURCES = ("all", "proxy", "transcript")

# One row per proxy request, plus one row per transcript (session, hour, model,
# prompt band) where the proxy has no record for that root session in that hour.
# The proxy wins when both exist: it also sees side calls and retries.
# Before raw_cutoff (raw proxy rows pruned) the check uses the daily rollup.
_PROXY_ROWS = (
    "SELECT ts, 'proxy' AS src, 1 AS n, provider, model, api_key_hash, auth_id, session_id, "
    "COALESCE(failed, 0) AS failed, input, output, cache_read, cache_write_5m, cache_write_1h, "
    "cache_write, COALESCE(input,0) + COALESCE(cache_read,0) + COALESCE(cache_write,0) AS prompt "
    "FROM requests WHERE ts >= ? AND ts < ?"
)
_TRANSCRIPT_ROWS = (
    "SELECT t.hour AS ts, 'transcript' AS src, t.messages AS n, t.harness AS provider, t.model, "
    "NULL AS api_key_hash, NULL AS auth_id, t.usage_session AS session_id, 0 AS failed, "
    "t.input, t.output, t.cache_read, t.cache_write_5m, t.cache_write_1h, "
    "t.cache_write_5m + t.cache_write_1h AS cache_write, t.prompt "
    "FROM transcript_usage t WHERE t.hour >= ? AND t.hour < ? "
    "AND NOT EXISTS (SELECT 1 FROM requests r WHERE r.session_id >= t.root_session "
    "AND r.session_id < t.root_session || ';' AND r.ts >= t.hour AND r.ts < t.hour + 3600) "
    "AND (t.hour >= ? OR NOT EXISTS (SELECT 1 FROM daily_rollup d "
    "WHERE d.day = strftime('%Y-%m-%d', t.hour, 'unixepoch') "
    "AND d.session_id >= t.root_session AND d.session_id < t.root_session || ';'))"
)


def usage_source(conn, frm: float, to: float, source: str = "all") -> tuple[str, list[Any]]:
    """SQL (aliased `requests`, so the GROUPS expressions apply) and its args."""
    source = source if source in SOURCES else "all"
    parts, args = [], []
    if source in ("all", "proxy"):
        parts.append(_PROXY_ROWS)
        args += [frm, to]
    if source in ("all", "transcript"):
        parts.append(_TRANSCRIPT_ROWS)
        args += [frm, to, _raw_cutoff(conn) or 0]
    return "(" + " UNION ALL ".join(parts) + ") AS requests", args


def where_clause(frm: float, to: float, filters: dict[str, str]) -> tuple[str, list[Any]]:
    parts, args = ["ts >= ?", "ts < ?"], [frm, to]
    for k, sql in FILTERS.items():
        v = filters.get(k)
        if v is not None and v != "":
            parts.append(sql)
            args.append(v)
    return " AND ".join(parts), args


class Agg(dict):
    """Totals for one group."""

    def __init__(self):
        super().__init__(requests=0, failed=0, input=0, output=0, cache_read=0,
                         cache_write_5m=0, cache_write_1h=0, cost=0.0, unpriced_requests=0)
        self._models: set[str] = set()


def aggregate(conn, prices: Prices, frm: float, to: float, group: str | list[str] | None,
              filters: dict[str, str] | None = None) -> dict[tuple, Agg]:
    """Totals per group key (a tuple, one item per group dimension)."""
    groups = [group] if isinstance(group, str) else list(group or [])
    for g in groups:
        if g not in GROUPS:
            raise ValueError(f"unknown group {g!r}")
    gexpr = [GROUPS[g] for g in groups]
    filters = filters or {}
    source = filters.get("source") or "all"
    src_sql, src_args = usage_source(conn, frm, to, source)
    where, wargs = where_clause(frm, to, filters)
    args = src_args + wargs
    sel = ", ".join(f"{e} AS g{i}" for i, e in enumerate(gexpr))
    gcols = ", ".join(f"g{i}" for i in range(len(gexpr)))
    sql = (
        f"SELECT {sel + ', ' if sel else ''}COALESCE(provider,'') AS p, COALESCE(model,'') AS m, "
        "SUM(n) AS requests, SUM(failed) AS failed, "
        "SUM(input) AS input, SUM(output) AS output, SUM(cache_read) AS cache_read, "
        "SUM(cache_write_5m) AS cache_write_5m, SUM(cache_write_1h) AS cache_write_1h "
        f"FROM {src_sql} WHERE {where} GROUP BY {gcols + ', ' if gcols else ''}p, m"
    )
    out: dict[tuple, Agg] = {}
    tiered: dict[tuple[str, str], Price] = {}
    for row in conn.execute(sql, args):
        key = tuple(row[f"g{i}"] for i in range(len(gexpr)))
        a = out.setdefault(key, Agg())
        toks = {c: row[c] or 0 for c in TOKEN_COLS}
        a["requests"] += row["requests"]
        a["failed"] += row["failed"] or 0
        for c in TOKEN_COLS:
            a[c] += toks[c]
        price = prices.lookup(row["p"], row["m"])
        if price is None:
            if any(toks.values()):
                a["unpriced_requests"] += row["requests"]
        else:
            a["cost"] += cost(price, **toks)
            if price.tiers:
                tiered[(row["p"], row["m"])] = price
    # Context-size tiers: re-price the requests whose prompt is above a tier size.
    for (p, m), price in tiered.items():
        sizes = [t["size"] for t in price.tiers]
        prompt = "prompt"
        band = "CASE " + " ".join(f"WHEN {prompt} > {s} THEN {i}" for i, s in reversed(list(enumerate(sizes)))) + " END"
        tsql = (
            f"SELECT {sel + ', ' if sel else ''}{band} AS band, "
            "SUM(input) AS input, SUM(output) AS output, SUM(cache_read) AS cache_read, "
            "SUM(cache_write_5m) AS cache_write_5m, SUM(cache_write_1h) AS cache_write_1h "
            f"FROM {src_sql} WHERE {where} AND COALESCE(provider,'') = ? AND COALESCE(model,'') = ? "
            f"AND {prompt} > ? GROUP BY {gcols + ', ' if gcols else ''}band"
        )
        for row in conn.execute(tsql, args + [p, m, sizes[0]]):
            key = tuple(row[f"g{i}"] for i in range(len(gexpr)))
            toks = {c: row[c] or 0 for c in TOKEN_COLS}
            tier_price = price.for_prompt(sizes[row["band"]] + 1)
            out[key]["cost"] += cost(tier_price, **toks) - cost(price, **toks)
    # Days older than the raw retention come from the daily rollup.
    raw_cutoff = _raw_cutoff(conn)
    if raw_cutoff and frm < raw_cutoff and source != "transcript" and set(groups) <= {"api_key", "auth", "provider", "model", "session", "day", "source"}:
        _add_rollup(conn, prices, out, groups, frm, min(to, raw_cutoff), filters)
    return out


def _raw_cutoff(conn) -> float | None:
    row = conn.execute("SELECT value FROM kv WHERE key = 'raw_cutoff'").fetchone()
    return float(row[0]) if row else None


def _add_rollup(conn, prices, out, groups, frm, to, filters):
    rs = root_session("daily_rollup.session_id")
    col = {"api_key": "api_key_hash", "auth": "auth_id", "provider": "provider", "model": "model",
           "session": rs, "day": "day", "source": "'proxy'"}
    parts, args = ["day >= ?", "day < ?"], [
        datetime.fromtimestamp(frm, timezone.utc).strftime("%Y-%m-%d"),
        datetime.fromtimestamp(to, timezone.utc).strftime("%Y-%m-%d"),
    ]
    fcol = {"api_key": "api_key_hash", "auth": "auth_id", "provider": "provider", "model": "model", "session": rs}
    for k, c in fcol.items():
        if filters.get(k):
            parts.append(f"{c} = ?")
            args.append(filters[k])
    sel = ", ".join(f"{col[g]} AS g{i}" for i, g in enumerate(groups))
    sql = (f"SELECT {sel + ', ' if sel else ''}provider AS p, model AS m, SUM(requests) AS requests, SUM(failed) AS failed, "
           "SUM(input) AS input, SUM(output) AS output, SUM(cache_read) AS cache_read, "
           "SUM(cache_write_5m) AS cache_write_5m, SUM(cache_write_1h) AS cache_write_1h "
           f"FROM daily_rollup WHERE {' AND '.join(parts)} GROUP BY {', '.join([f'g{i}' for i in range(len(groups))] + ['p', 'm'])}")
    for row in conn.execute(sql, args):
        key = tuple(row[f"g{i}"] for i in range(len(groups)))
        a = out.setdefault(key, Agg())
        toks = {c: row[c] or 0 for c in TOKEN_COLS}
        a["requests"] += row["requests"]
        a["failed"] += row["failed"] or 0
        for c in TOKEN_COLS:
            a[c] += toks[c]
        price = prices.lookup(row["p"], row["m"])
        if price:
            a["cost"] += cost(price, **toks)
        elif any(toks.values()):
            a["unpriced_requests"] += row["requests"]


def labels(conn) -> dict[tuple[str, str], str]:
    return {(r["kind"], r["key"]): r["label"] for r in conn.execute("SELECT kind, key, label FROM labels")}


def describe(conn, group: str, key: str, lab: dict) -> dict[str, Any]:
    """Display fields for one group key."""
    d: dict[str, Any] = {"key": key}
    if key == TRANSCRIPT_GROUP and group in ("api_key", "auth"):
        d["label"] = "(transcript)"
    elif group == "api_key":
        row = conn.execute("SELECT api_key_hint FROM requests WHERE api_key_hash = ? AND api_key_hint IS NOT NULL LIMIT 1", (key,)).fetchone()
        d["hint"] = row[0] if row else None
        d["label"] = lab.get(("api_key", key)) or (row[0] if row else None) or (key[:10] if key else "(none)")
    elif group == "auth":
        row = conn.execute("SELECT auth_type, provider, base_url FROM requests WHERE auth_id = ? LIMIT 1", (key,)).fetchone()
        if row:
            d.update(auth_type=row["auth_type"], provider=row["provider"], base_url=row["base_url"])
        d["label"] = lab.get(("auth", key)) or key or "(none)"
    elif group == "session":
        row = conn.execute(
            "SELECT title, project FROM sessions WHERE session_id = ?",
            (key.split(":", 1)[1] if ":" in key else key,),
        ).fetchone()
        d["title"] = row["title"] if row else None
        d["project"] = row["project"] if row else None
        d["label"] = (row["title"] if row and row["title"] else None) or key or "(none)"
    else:
        d["label"] = key or "(none)"
    return d


def rows_for(conn, prices, group: str, frm, to, filters, limit: int | None = None, sort: str = "cost") -> list[dict]:
    agg = aggregate(conn, prices, frm, to, group, filters)
    lab = labels(conn)
    items = sorted(agg.items(), key=lambda kv: (kv[1].get(sort, 0) or 0), reverse=True)
    if limit:
        items = items[:limit]
    out = []
    for (key,), a in items:
        d = describe(conn, group, key, lab)
        d.update({k: v for k, v in a.items()})
        d["cost"] = round(a["cost"], 6)
        d["tokens"] = sum(a[c] for c in TOKEN_COLS)
        out.append(d)
    return out


def summary(conn, prices, frm, to, filters) -> dict[str, Any]:
    tot = aggregate(conn, prices, frm, to, None, filters).get((), Agg())
    res = {
        "from": frm, "to": to,
        "totals": {**{k: v for k, v in tot.items()}, "cost": round(tot["cost"], 6),
                   "tokens": sum(tot[c] for c in TOKEN_COLS)},
        "top": {},
        "by_source": {k[0]: {**a, "cost": round(a["cost"], 6)}
                      for k, a in aggregate(conn, prices, frm, to, "source", filters).items()},
    }
    for g in ("api_key", "auth", "provider", "model", "session", "project"):
        res["top"][g] = rows_for(conn, prices, g, frm, to, filters, limit=5)
    return res


def all_buckets(frm: float, to: float, bucket: str, seen) -> list[str]:
    """Every bucket in the range (so empty days show), plus any seen ones."""
    step = 3600 if bucket == "hour" else 86400
    fmt = "%Y-%m-%dT%H:00" if bucket == "hour" else "%Y-%m-%d"
    out = set(seen)
    if (to - frm) / step <= 800:
        t = frm - (frm % step)
        while t < to:
            out.add(datetime.fromtimestamp(t, timezone.utc).strftime(fmt))
            t += step
    return sorted(out)


def timeseries(conn, prices, frm, to, bucket: str, group: str | None, top: int, filters, metric: str = "cost") -> dict[str, Any]:
    if bucket not in ("hour", "day"):
        raise ValueError("bucket must be hour or day")
    if not group:
        agg = aggregate(conn, prices, frm, to, [bucket], filters)
        buckets = all_buckets(frm, to, bucket, (k[0] for k in agg))
        z = Agg()
        return {"bucket": bucket, "buckets": buckets, "series": [{
            "key": "total", "label": "total",
            "values": [round(agg.get((b,), z)["cost"], 6) if metric == "cost" else sum(agg.get((b,), z)[c] for c in TOKEN_COLS) for b in buckets],
        }]}
    agg = aggregate(conn, prices, frm, to, [bucket, group], filters)
    totals: dict[str, float] = {}
    for (b, g), a in agg.items():
        totals[g] = totals.get(g, 0) + a["cost"]
    keep = [g for g, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:top]]
    buckets = all_buckets(frm, to, bucket, {b for b, _ in agg})
    lab = labels(conn)
    series = []
    for g in keep + (["__other__"] if len(totals) > len(keep) else []):
        vals = []
        for b in buckets:
            if g == "__other__":
                items = [a for (bb, gg), a in agg.items() if bb == b and gg not in keep]
            else:
                items = [agg[(b, g)]] if (b, g) in agg else []
            if metric == "cost":
                vals.append(round(sum(a["cost"] for a in items), 6))
            else:
                vals.append(sum(a[c] for a in items for c in TOKEN_COLS))
        label = "other" if g == "__other__" else describe(conn, group, g, lab)["label"]
        series.append({"key": g, "label": label, "values": vals})
    return {"bucket": bucket, "group": group, "buckets": buckets, "series": series}


def _usage_cost(prices: Prices, usage_by_model: dict, provider: str = "claude") -> tuple[float, bool]:
    total, known = 0.0, True
    for model, u in (usage_by_model or {}).items():
        p = prices.lookup(provider, model)
        if p is None:
            known = False
            continue
        total += cost(p, int(u.get("input", 0)), int(u.get("output", 0)), int(u.get("cache_read", 0)),
                      int(u.get("cache_write_5m", 0)), int(u.get("cache_write_1h", 0)))
    return round(total, 6), known


def sessions_list(conn, prices, frm, to, q: str = "", harness: str = "", has_transcript: str = "", limit: int = 200) -> list[dict]:
    proxy = aggregate(conn, prices, frm, to, "session", {"source": "proxy"})
    rows: dict[str, dict] = {}
    for (sid,), a in proxy.items():
        if not sid:
            continue
        h, _, tid = sid.partition(":") if ":" in sid else ("", "", sid)
        rows[sid] = {"id": sid, "harness": h or None, "transcript_id": tid, "proxy": {**a, "cost": round(a["cost"], 6)}}
    tsql = "SELECT * FROM sessions WHERE (last_ts >= ? AND first_ts < ?)"
    for t in conn.execute(tsql, (frm, to)):
        sid = f"{t['harness']}:{t['session_id']}"
        r = rows.setdefault(sid, {"id": sid, "harness": t["harness"], "transcript_id": t["session_id"], "proxy": None})
        r["transcript"] = _session_brief(t, prices)
    # Proxy sessions whose transcript is outside the window.
    for sid, r in rows.items():
        if "transcript" not in r and r["harness"]:
            t = conn.execute("SELECT * FROM sessions WHERE harness = ? AND session_id = ?", (r["harness"], r["transcript_id"])).fetchone()
            r["transcript"] = _session_brief(t, prices) if t else None
    out = list(rows.values())
    if harness:
        out = [r for r in out if r["harness"] == harness]
    if has_transcript in ("1", "true", "yes"):
        out = [r for r in out if r.get("transcript")]
    elif has_transcript in ("0", "false", "no"):
        out = [r for r in out if not r.get("transcript")]
    if q:
        ql = q.lower()
        out = [r for r in out if ql in r["id"].lower() or any(
            ql in str((r.get("transcript") or {}).get(f) or "").lower() for f in ("title", "project", "cwd", "git_branch"))]

    def sort_key(r):
        t = r.get("transcript") or {}
        return t.get("last_ts") or 0, (r.get("proxy") or {}).get("cost", 0)
    out.sort(key=sort_key, reverse=True)
    return out[:limit]


def _session_brief(t, prices) -> dict:
    usage = json.loads(t["usage_by_model"]) if t["usage_by_model"] else {}
    c, known = _usage_cost(prices, usage, t["harness"])
    return {
        "title": t["title"], "cwd": t["cwd"], "project": t["project"], "git_branch": t["git_branch"],
        "first_ts": t["first_ts"], "last_ts": t["last_ts"], "turns": t["turns"], "peak_context": t["peak_context"],
        "compactions": t["compactions"], "cache_rebuilds": t["cache_rebuilds"], "tool_errors": t["tool_errors"],
        "parse_error": t["parse_error"], "cost": c, "cost_complete": known,
        "tokens": sum(sum(int(v) for v in u.values()) for u in usage.values()),
    }


def session_detail(conn, prices, sid: str) -> dict[str, Any] | None:
    if ":" in sid:
        harness, tid = sid.split(":", 1)
    else:
        harness, tid = "claude", sid
        sid = f"claude:{sid}"
    far = (0, time.time() + 86400)
    f = {"session": sid, "source": "proxy"}
    proxy = {
        "totals": None,
        "by_model": rows_for(conn, prices, "model", *far, f),
        "by_auth": rows_for(conn, prices, "auth", *far, f),
        "by_api_key": rows_for(conn, prices, "api_key", *far, f),
    }
    tot = aggregate(conn, prices, *far, None, f).get(())
    if tot:
        proxy["totals"] = {**tot, "cost": round(tot["cost"], 6)}
    # Per proxy session ID (main + each subagent), keyed by agent ID.
    per_agent: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT session_id, COALESCE(provider,'') p, COALESCE(model,'') m, COUNT(*) n, SUM(input) input, SUM(output) output, "
        "SUM(cache_read) cache_read, SUM(cache_write_5m) cache_write_5m, SUM(cache_write_1h) cache_write_1h, "
        f"MIN(ts) first_ts, MAX(ts) last_ts FROM requests WHERE {ROOT_SESSION} = ? GROUP BY session_id, p, m", (sid,)
    ):
        agent = r["session_id"].split(":agent:", 1)[1] if ":agent:" in r["session_id"] else "main"
        a = per_agent.setdefault(agent, {"agent_id": agent, "requests": 0, "cost": 0.0, "tokens": 0, "first_ts": r["first_ts"], "last_ts": r["last_ts"]})
        toks = {c: r[c] or 0 for c in TOKEN_COLS}
        a["requests"] += r["n"]
        a["tokens"] += sum(toks.values())
        p = prices.lookup(r["p"], r["m"])
        if p:
            a["cost"] = round(a["cost"] + cost(p, **toks), 6)
        a["first_ts"] = min(a["first_ts"], r["first_ts"])
        a["last_ts"] = max(a["last_ts"], r["last_ts"])
    proxy["by_agent"] = sorted(per_agent.values(), key=lambda a: a["first_ts"] or 0)
    timeline = [dict(r) for r in conn.execute(
        "SELECT ts, session_id, model, failed, status, input, output, cache_read, cache_write, latency_ms "
        f"FROM requests WHERE {ROOT_SESSION} = ? ORDER BY ts LIMIT 2000", (sid,))]
    for t in timeline:
        t["agent_id"] = t.pop("session_id").split(":agent:", 1)[1] if ":agent:" in (t.get("session_id") or "") else "main"
    t = conn.execute("SELECT * FROM sessions WHERE harness = ? AND session_id = ?", (harness, tid)).fetchone()
    transcript = None
    if t:
        transcript = _session_brief(t, prices)
        transcript["usage_by_model"] = json.loads(t["usage_by_model"]) if t["usage_by_model"] else {}
        transcript["tools"] = json.loads(t["tools"]) if t["tools"] else {}
        transcript["path"] = t["path"]
        agents = []
        for a in conn.execute("SELECT * FROM session_agents WHERE harness = ? AND session_id = ? ORDER BY agent_id", (harness, tid)):
            u = json.loads(a["usage_by_model"]) if a["usage_by_model"] else {}
            c, known = _usage_cost(prices, u, harness)
            agents.append({"agent_id": a["agent_id"], "agent_type": a["agent_type"], "description": a["description"],
                           "model": a["model"], "turns": a["turns"], "usage_by_model": u, "cost": c, "cost_complete": known,
                           "proxy": per_agent.get(a["agent_id"])})
        transcript["agents"] = agents
        skills = []
        for s in conn.execute("SELECT * FROM session_skills WHERE harness = ? AND session_id = ? ORDER BY skill", (harness, tid)):
            u = json.loads(s["usage_by_model"]) if s["usage_by_model"] else {}
            c, known = _usage_cost(prices, u, harness)
            skills.append({"skill": s["skill"], "loads": s["loads"], "turns": s["turns"], "usage_by_model": u, "cost": c})
        transcript["skills"] = skills
        transcript["prs"] = [dict(r) for r in conn.execute(
            "SELECT repo, number, url, action, ts FROM session_prs WHERE harness = ? AND session_id = ? ORDER BY ts", (harness, tid))]
    if not tot and not t:
        return None
    return {"id": sid, "harness": harness, "transcript_id": tid, "proxy": proxy, "transcript": transcript, "timeline": timeline}


def unknown_models(conn, prices, frm: float = 0) -> list[dict]:
    out = []
    for r in conn.execute("SELECT COALESCE(provider,'') p, COALESCE(model,'') m, COUNT(*) n, MAX(ts) last FROM requests "
                          "WHERE ts >= ? GROUP BY p, m", (frm,)):
        if prices.lookup(r["p"], r["m"]) is None:
            out.append({"provider": r["p"], "model": r["m"], "requests": r["n"], "last_ts": r["last"]})
    return out
