"""Spool reader: tails the plugin's JSONL files into SQLite (SPEC §5.3.1).

Each file is read from its saved offset up to the last complete line. Rows are
inserted with INSERT OR IGNORE on request_id, and the new offset is saved in
the same transaction, so a crash never loses or double-counts a record.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import Database

log = logging.getLogger("usage_server.spool")

STATUS_FILE = "_plugin_status.json"
MAX_READ_BYTES = 8 * 1024 * 1024  # per file per pass, bounds memory
MAX_LINE_BYTES = 4 * 1024 * 1024  # a longer "line" without newline is garbage
STALE_TAIL_SECONDS = 60  # a partial last line in a closed, idle file will never complete

COLUMNS = [
    "request_id", "trace_id", "ts", "provider", "executor", "base_url", "model", "alias",
    "response_model", "api_key_hash", "api_key_hint", "access_provider", "session_id",
    "parent_session_id", "auth_id", "auth_index", "auth_type", "source", "source_hash",
    "source_hint", "reasoning_effort", "service_tier", "effective_service_tier", "stream",
    "failed", "status", "error", "latency_ms", "ttft_ms", "input", "output", "reasoning",
    "cache_read", "cache_write", "cache_write_5m", "cache_write_1h", "total", "ratelimit",
]
INSERT_SQL = (
    f"INSERT OR IGNORE INTO requests({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in COLUMNS)})"
)
ROLLUP_SQL = """
INSERT INTO daily_rollup(day, api_key_hash, auth_id, provider, model, session_id,
  requests, failed, input, output, cache_read, cache_write_5m, cache_write_1h)
VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
ON CONFLICT(day, api_key_hash, auth_id, provider, model, session_id) DO UPDATE SET
  requests = requests + 1, failed = failed + excluded.failed,
  input = input + excluded.input, output = output + excluded.output,
  cache_read = cache_read + excluded.cache_read,
  cache_write_5m = cache_write_5m + excluded.cache_write_5m,
  cache_write_1h = cache_write_1h + excluded.cache_write_1h
"""

_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_ts(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    s = _FRACTION.sub(r"\1", value.strip())
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _int(v: Any) -> int:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    return 0


def _opt_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def _str(v: Any, limit: int = 1024) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s[:limit] if s else None


def normalize(rec: dict[str, Any]) -> dict[str, Any] | None:
    """Turn one spool record (v1) into a requests row. None for bad records.

    Token rules:
    - `input` is stored as *uncached* input. Anthropic reports it that way.
      OpenAI-style usage reports prompt tokens including cached tokens; that is
      detected from `total` (or, without a total, from the provider) and the
      cached part is subtracted.
    - `cached` is ignored: for Claude the host fills it with the cache *write*
      count when there are no cache reads.
    - An unknown 5m/1h cache-write split counts the whole write as 5-minute.
    """
    if not isinstance(rec, dict) or rec.get("v") != 1:
        return None
    rid = rec.get("request_id")
    ts = parse_ts(rec.get("ts"))
    if not isinstance(rid, str) or not rid or ts is None:
        return None
    inp, out = _int(rec.get("input")), _int(rec.get("output"))
    cr, cw = _int(rec.get("cache_read")), _int(rec.get("cache_write"))
    total = _int(rec.get("total"))
    if cr > 0 and inp >= cr:
        independent = inp + out + cr + cw
        inclusive = inp + out
        if total > 0:
            if abs(total - inclusive) < abs(total - independent):
                inp -= cr
        elif (rec.get("provider") or "").lower() not in ("claude", "anthropic"):
            inp -= cr
    w5, w1 = _opt_int(rec.get("cache_write_5m")), _opt_int(rec.get("cache_write_1h"))
    if w5 is None and w1 is None:
        w5, w1 = cw, 0
    elif w5 is None:
        w5 = max(cw - (w1 or 0), 0)
    elif w1 is None:
        w1 = max(cw - w5, 0)
    if cw == 0 and (w5 or w1):
        cw = (w5 or 0) + (w1 or 0)
    rl = rec.get("ratelimit")
    row = {
        "request_id": rid[:128],
        "trace_id": _str(rec.get("trace_id"), 128),
        "ts": ts,
        "provider": _str(rec.get("provider"), 64),
        "executor": _str(rec.get("executor"), 64),
        "base_url": _str(rec.get("base_url"), 512),
        "model": _str(rec.get("model"), 256),
        "alias": _str(rec.get("alias"), 256),
        "response_model": _str(rec.get("response_model"), 256),
        "api_key_hash": _str(rec.get("api_key_hash"), 64),
        "api_key_hint": _str(rec.get("api_key_hint"), 16),
        "access_provider": _str(rec.get("access_provider"), 128),
        "session_id": _str(rec.get("session_id"), 256),
        "parent_session_id": _str(rec.get("parent_session_id"), 256),
        "auth_id": _str(rec.get("auth_id"), 256),
        "auth_index": _str(rec.get("auth_index"), 64),
        "auth_type": _str(rec.get("auth_type"), 64),
        "source": _str(rec.get("source"), 256),
        "source_hash": _str(rec.get("source_hash"), 64),
        "source_hint": _str(rec.get("source_hint"), 16),
        "reasoning_effort": _str(rec.get("reasoning_effort"), 32),
        "service_tier": _str(rec.get("service_tier"), 32),
        "effective_service_tier": _str(rec.get("effective_service_tier"), 32),
        "stream": 1 if rec.get("stream") else 0,
        "failed": 1 if rec.get("failed") else 0,
        "status": _opt_int(rec.get("status")),
        "error": _str(rec.get("error"), 1024),
        "latency_ms": _opt_int(rec.get("latency_ms")),
        "ttft_ms": _opt_int(rec.get("ttft_ms")),
        "input": max(inp, 0),
        "output": out,
        "reasoning": _int(rec.get("reasoning")),
        "cache_read": cr,
        "cache_write": cw,
        "cache_write_5m": w5,
        "cache_write_1h": w1,
        "total": total,
        "ratelimit": json.dumps(rl, sort_keys=True) if isinstance(rl, dict) and rl else None,
    }
    return row


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


class SpoolReader:
    def __init__(self, db: Database, spool_dir: Path, poll_seconds: float = 2.0):
        self.db = db
        self.dir = Path(spool_dir)
        self.poll = poll_seconds
        self.stats = {"bad_lines": 0, "inserted": 0, "duplicates": 0, "files": 0, "last_pass": 0.0, "errors": 0}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        saved = db.kv_get("spool_bad_lines")
        if saved and saved.isdigit():
            self.stats["bad_lines"] = int(saved)

    # -- lifecycle --
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="spool-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # never let the thread die
                self.stats["errors"] += 1
                log.exception("spool pass failed")
            self._stop.wait(self.poll)

    # -- work --
    def list_files(self) -> list[Path]:
        try:
            names = sorted(n for n in os.listdir(self.dir) if n.startswith("usage-") and n.endswith(".jsonl"))
        except FileNotFoundError:
            return []
        return [self.dir / n for n in names]

    def run_once(self) -> int:
        """One pass over all spool files. Returns rows inserted."""
        with self._lock:
            files = self.list_files()
            with self.db.read() as r:
                offsets = {row["file"]: (row["inode"], row["offset"]) for row in r.execute("SELECT * FROM spool_offsets")}
            inserted = 0
            present = set()
            current = self._current_file()
            for path in files:
                present.add(path.name)
                try:
                    inserted += self._read_file(path, offsets.get(path.name), current)
                except FileNotFoundError:
                    present.discard(path.name)  # deleted between list and open
                except OSError:
                    self.stats["errors"] += 1
                    log.exception("reading %s", path.name)
            gone = [name for name in offsets if name not in present]
            if gone:
                with self.db.write() as w:
                    w.executemany("DELETE FROM spool_offsets WHERE file = ?", [(g,) for g in gone])
            self.stats["files"] = len(files)
            self.stats["last_pass"] = time.time()
            return inserted

    def _current_file(self) -> str | None:
        st = self.plugin_status()
        return st.get("current_file") if st else None

    def _read_file(self, path: Path, saved: tuple[int, int] | None, current: str | None = None) -> int:
        inserted = 0
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            offset = 0
            if saved is not None and saved[0] == st.st_ino and saved[1] <= st.st_size:
                offset = saved[1]
            # Otherwise: new file, a different file under the same name, or a
            # truncated file. Start from the beginning; request_id dedups.
            while offset < st.st_size:
                fh.seek(offset)
                chunk = fh.read(min(MAX_READ_BYTES, st.st_size - offset))
                end = chunk.rfind(b"\n")
                if end < 0:
                    if len(chunk) >= MAX_LINE_BYTES:
                        # No newline in a huge run of bytes: skip it as one bad line.
                        self._commit(path, st.st_ino, offset + len(chunk), [], bad=1)
                        offset += len(chunk)
                        continue
                    # Partial last line: wait for the rest, unless the plugin has
                    # moved on (e.g. the disk filled mid-line) and the file is idle.
                    if path.name != current and time.time() - st.st_mtime > STALE_TAIL_SECONDS:
                        self._commit(path, st.st_ino, offset + len(chunk), [], bad=1)
                        offset += len(chunk)
                        continue
                    break
                body = chunk[: end + 1]
                rows, bad = [], 0
                for raw in body.split(b"\n"):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        row = normalize(json.loads(raw))
                    except (ValueError, UnicodeDecodeError):
                        row = None
                    if row is None:
                        bad += 1
                    else:
                        rows.append(row)
                offset += len(body)
                inserted += self._commit(path, st.st_ino, offset, rows, bad)
            if saved is None and st.st_size == 0:
                self._commit(path, st.st_ino, 0, [], 0)
        return inserted

    def _commit(self, path: Path, inode: int, offset: int, rows: list[dict], bad: int) -> int:
        n = 0
        with self.db.write() as w:
            for row in rows:
                cur = w.execute(INSERT_SQL, [row[c] for c in COLUMNS])
                if cur.rowcount == 1:
                    n += 1
                    w.execute(ROLLUP_SQL, (
                        _day(row["ts"]), row["api_key_hash"] or "", row["auth_id"] or "",
                        row["provider"] or "", row["model"] or "", row["session_id"] or "",
                        row["failed"], row["input"], row["output"], row["cache_read"],
                        row["cache_write_5m"] or 0, row["cache_write_1h"] or 0,
                    ))
            w.execute(
                "INSERT INTO spool_offsets(file, inode, offset, updated) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(file) DO UPDATE SET inode = excluded.inode, offset = excluded.offset, updated = excluded.updated",
                (path.name, inode, offset, time.time()),
            )
            if bad:
                self.stats["bad_lines"] += bad
                self.db.kv_set("spool_bad_lines", str(self.stats["bad_lines"]), conn=w)
        if bad:
            log.warning("skipped %d bad line(s) in %s", bad, path.name)
        self.stats["inserted"] += n
        self.stats["duplicates"] += len(rows) - n
        return n

    # -- health --
    def plugin_status(self) -> dict[str, Any] | None:
        try:
            raw = (self.dir / STATUS_FILE).read_text()
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def lag(self) -> dict[str, Any]:
        """Bytes in spool files not yet read."""
        files = self.list_files()
        with self.db.read() as r:
            offsets = {row["file"]: row["offset"] for row in r.execute("SELECT file, offset FROM spool_offsets")}
        pending = 0
        for p in files:
            try:
                pending += max(p.stat().st_size - offsets.get(p.name, 0), 0)
            except OSError:
                pass
        return {"files": len(files), "pending_bytes": pending}


def prune(db: Database, retention_days: int) -> int:
    """Delete raw request rows older than the retention. Daily rollups stay."""
    cutoff = time.time() - retention_days * 86400
    with db.write() as w:
        cur = w.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
        n = cur.rowcount
        db.kv_set("raw_cutoff", str(cutoff), conn=w)
    return n
