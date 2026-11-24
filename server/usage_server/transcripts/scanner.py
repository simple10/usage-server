"""Transcript scanner and parser pool (SPEC §5.3.3, §5.3.4).

Every scan interval each adapter lists its session files. Files whose
(mtime, size) changed are queued, newest first, within the backfill window.
Workers run the parser in a subprocess on batches of about 20 files with a
timeout. On a timeout or crash the batch is retried one file at a time; a file
that fails alone is marked parse_error and is not retried until it changes.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import Database
from . import ADAPTERS, adapter

log = logging.getLogger("usage_server.transcripts")


class Scanner:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.s = settings
        self.root = Path(settings.transcripts_dir)
        self.harnesses = [h for h in ADAPTERS if (self.root / h).is_dir()]
        self._queue: list[tuple[float, str, str, float, int]] = []  # (mtime, harness, path, mtime, size)
        self._queued: set[str] = set()
        self._inflight: set[str] = set()
        self._forced: set[str] = set()
        self._cv = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        # The parser command; tests replace it to simulate crashes and hangs.
        self.parser_cmd = [sys.executable, "-m", "usage_server.transcripts.parse"]
        self.stats = {"last_scan": None, "scan_seconds": None, "parsed": 0, "errors": 0, "timeouts": 0, "crashes": 0}

    # -- lifecycle --
    def start(self) -> None:
        t = threading.Thread(target=self._scan_loop, name="transcript-scan", daemon=True)
        t.start()
        self._threads.append(t)
        for i in range(self.s.parser_workers):
            w = threading.Thread(target=self._worker, name=f"parser-{i}", daemon=True)
            w.start()
            self._threads.append(w)

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()

    # -- scanning --
    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                log.exception("transcript scan failed")
            self._stop.wait(self.s.transcript_scan_seconds)

    def scan_once(self) -> int:
        t0 = time.time()
        min_mtime = t0 - self.s.transcript_backfill_days * 86400
        with self.db.read() as r:
            known = {(row["harness"], row["path"]): (row["mtime"], row["size"])
                     for row in r.execute("SELECT harness, path, mtime, size FROM sessions")}
        added = 0
        for h in self.harnesses:
            ad = adapter(h)
            for sf in ad.discover(self.root / h, min_mtime=min_mtime):
                p = str(sf.path)
                prev = known.get((h, p))
                if prev and prev[0] == sf.mtime and prev[1] == sf.size and p not in self._forced:
                    continue
                if self._enqueue(h, p, sf.mtime, sf.size):
                    added += 1
        self.stats["last_scan"] = time.time()
        self.stats["scan_seconds"] = round(time.time() - t0, 3)
        return added

    def _enqueue(self, harness: str, path: str, mtime: float, size: int) -> bool:
        with self._cv:
            if path in self._queued or path in self._inflight:
                return False
            self._queue.append((mtime, harness, path, mtime, size))
            self._queued.add(path)
            self._queue.sort(key=lambda x: x[0])  # newest at the end; pop() takes newest
            self._cv.notify()
            return True

    def request_reparse(self, sid: str) -> bool:
        """Queue a re-parse of one session by proxy or transcript ID. Returns at once."""
        harness, _, tid = sid.partition(":") if ":" in sid else ("claude", "", sid)
        tid = tid.split(":agent:", 1)[0]
        with self.db.read() as r:
            row = r.execute("SELECT path FROM sessions WHERE harness = ? AND session_id = ?", (harness, tid)).fetchone()
        path = row["path"] if row else None
        if not path and harness in self.harnesses:
            hits = list((self.root / harness).glob(f"*/{tid}.jsonl"))
            path = str(hits[0]) if hits else None
        if not path or not os.path.exists(path):
            return False
        st = os.stat(path)
        self._forced.add(path)
        self._enqueue(harness, path, time.time(), st.st_size)
        return True

    # -- workers --
    def _take(self, n: int) -> list[tuple]:
        with self._cv:
            while not self._queue and not self._stop.is_set():
                self._cv.wait(timeout=5)
            if self._stop.is_set():
                return []
            batch, harness = [], None
            while self._queue and len(batch) < n:
                item = self._queue[-1]
                if harness and item[1] != harness:
                    break
                harness = item[1]
                self._queue.pop()
                self._queued.discard(item[2])
                self._inflight.add(item[2])
                batch.append(item)
            return batch

    def _worker(self) -> None:
        while not self._stop.is_set():
            batch = self._take(self.s.parser_batch)
            if not batch:
                continue
            try:
                self.process_batch(batch)
            except Exception:
                log.exception("parser batch failed")

    def _run(self, harness: str, paths: list[str], timeout: float) -> tuple[dict[str, dict], str | None]:
        """Run the parser subprocess. Returns results by path and a failure reason."""
        cmd = [*self.parser_cmd, "--harness", harness, *paths]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False,
                                  env={**os.environ, "PYTHONHASHSEED": "0"})
        except subprocess.TimeoutExpired as e:
            self.stats["timeouts"] += 1
            return self._results(e.stdout or b""), "timeout"
        res = self._results(proc.stdout)
        if proc.returncode != 0:
            self.stats["crashes"] += 1
            return res, f"exit {proc.returncode}: {proc.stderr.decode(errors='replace')[-300:]}"
        return res, None

    @staticmethod
    def _results(out: bytes) -> dict[str, dict]:
        res = {}
        for line in out.splitlines():
            try:
                r = json.loads(line)
                res[r["path"]] = r
            except (ValueError, KeyError, TypeError):
                continue
        return res

    def process_batch(self, batch: list[tuple]) -> None:
        try:
            self._process(batch)
        finally:
            with self._cv:
                for item in batch:
                    self._inflight.discard(item[2])
                    self._forced.discard(item[2])

    def _process(self, batch: list[tuple]) -> None:
        harness = batch[0][1]
        paths = [b[2] for b in batch]
        meta = {b[2]: (b[3], b[4]) for b in batch}
        res, failure = self._run(harness, paths, self.s.parser_timeout)
        missing = [p for p in paths if p not in res]
        if failure and missing:
            log.warning("parser batch of %d failed (%s); retrying %d file(s) one at a time", len(paths), failure, len(missing))
            for p in missing:
                one, why = self._run(harness, [p], self.s.parser_timeout)
                if p in one:
                    res[p] = one[p]
                else:
                    res[p] = {"path": p, "ok": False, "error": why or "no output"}
        for p in paths:
            r = res.get(p) or {"path": p, "ok": False, "error": "no output"}
            mtime, size = meta[p]
            if p in self._forced or mtime > time.time() - 5:
                # Forced re-parse used a fake mtime; store the real one.
                try:
                    st = os.stat(p)
                    mtime, size = st.st_mtime, self._size_with_subagents(p, st.st_size)
                except OSError:
                    pass
            self.store(harness, p, mtime, size, r)

    @staticmethod
    def _size_with_subagents(path: str, size: int) -> int:
        sub = os.path.join(path[:-6], "subagents")
        for dirpath, _, files in os.walk(sub):
            for f in files:
                if f.startswith("agent-") and f.endswith(".jsonl"):
                    try:
                        size += os.stat(os.path.join(dirpath, f)).st_size
                    except OSError:
                        pass
        return size

    def store(self, harness: str, path: str, mtime: float, size: int, r: dict[str, Any]) -> None:
        now = time.time()
        sid = Path(path).stem
        with self.db.write() as w:
            if not r.get("ok"):
                self.stats["errors"] += 1
                w.execute(
                    "INSERT INTO sessions(harness, session_id, path, mtime, size, parsed_at, parse_error) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(harness, session_id) DO UPDATE SET path=excluded.path, mtime=excluded.mtime, size=excluded.size, "
                    "parsed_at=excluded.parsed_at, parse_error=excluded.parse_error",
                    (harness, sid, path, mtime, size, now, str(r.get("error") or "parse failed")[:500]))
                return
            s = r["summary"]
            sid = s["session_id"]
            w.execute(
                "INSERT OR REPLACE INTO sessions(harness, session_id, path, mtime, size, parsed_at, parse_error, title, cwd, project, "
                "git_branch, first_ts, last_ts, turns, peak_context, compactions, cache_rebuilds, tool_errors, usage_by_model, tools) "
                "VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (harness, sid, path, mtime, size, now, s["title"], s["cwd"], s["project"], s["git_branch"], s["first_ts"],
                 s["last_ts"], s["turns"], s["peak_context"], s["compactions"], s["cache_rebuilds"], s["tool_errors"],
                 json.dumps(s["usage_by_model"]), json.dumps(s["tools"])))
            for table in ("session_agents", "session_skills", "session_prs"):
                w.execute(f"DELETE FROM {table} WHERE harness = ? AND session_id = ?", (harness, sid))
            w.executemany(
                "INSERT OR REPLACE INTO session_agents(harness, session_id, agent_id, agent_type, description, model, turns, usage_by_model) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [(harness, sid, a["agent_id"], a["agent_type"], a["description"], a["model"], a["turns"], json.dumps(a["usage_by_model"]))
                 for a in s["agents"]])
            w.executemany(
                "INSERT OR REPLACE INTO session_skills(harness, session_id, skill, loads, turns, usage_by_model) VALUES (?,?,?,?,?,?)",
                [(harness, sid, k["skill"], k["loads"], k["turns"], json.dumps(k["usage_by_model"])) for k in s["skills"]])
            w.executemany(
                "INSERT OR REPLACE INTO session_prs(harness, session_id, repo, number, url, action, ts) VALUES (?,?,?,?,?,?,?)",
                [(harness, sid, p["repo"], p["number"], p["url"], p["action"], p["ts"]) for p in s["prs"]])
        self.stats["parsed"] += 1

    def status(self) -> dict[str, Any]:
        with self.db.read() as r:
            n, errs = r.execute("SELECT COUNT(*), SUM(parse_error IS NOT NULL) FROM sessions").fetchone()
        with self._cv:
            q, inflight = len(self._queue), len(self._inflight)
        return {**self.stats, "harnesses": self.harnesses, "sessions": n, "errors": errs or 0,
                "queued": q, "in_flight": inflight}
