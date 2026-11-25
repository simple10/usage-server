"""SQLite access: one writer connection behind a lock, readers on their own
connections, WAL mode, and ordered migrations (SPEC §5.3, §5.4)."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE requests(
      id INTEGER PRIMARY KEY,
      request_id TEXT UNIQUE NOT NULL,
      trace_id TEXT,
      ts REAL NOT NULL,
      provider TEXT, executor TEXT, base_url TEXT, model TEXT, alias TEXT, response_model TEXT,
      api_key_hash TEXT, api_key_hint TEXT, access_provider TEXT,
      session_id TEXT, parent_session_id TEXT,
      auth_id TEXT, auth_index TEXT, auth_type TEXT,
      source TEXT, source_hash TEXT, source_hint TEXT,
      reasoning_effort TEXT, service_tier TEXT, effective_service_tier TEXT,
      stream INT, failed INT, status INT, error TEXT, latency_ms INT, ttft_ms INT,
      input INT, output INT, reasoning INT, cache_read INT, cache_write INT,
      cache_write_5m INT, cache_write_1h INT, total INT,
      ratelimit TEXT
    );
    CREATE INDEX requests_ts ON requests(ts);
    CREATE INDEX requests_session ON requests(session_id, ts);
    CREATE INDEX requests_key ON requests(api_key_hash, ts);
    CREATE INDEX requests_auth ON requests(auth_id, ts);
    CREATE INDEX requests_model ON requests(model, ts);

    CREATE TABLE daily_rollup(
      day TEXT NOT NULL, api_key_hash TEXT NOT NULL, auth_id TEXT NOT NULL,
      provider TEXT NOT NULL, model TEXT NOT NULL, session_id TEXT NOT NULL,
      requests INT NOT NULL DEFAULT 0, failed INT NOT NULL DEFAULT 0,
      input INT NOT NULL DEFAULT 0, output INT NOT NULL DEFAULT 0,
      cache_read INT NOT NULL DEFAULT 0, cache_write_5m INT NOT NULL DEFAULT 0,
      cache_write_1h INT NOT NULL DEFAULT 0,
      PRIMARY KEY(day, api_key_hash, auth_id, provider, model, session_id)
    );

    CREATE TABLE spool_offsets(file TEXT PRIMARY KEY, inode INT, offset INT, updated REAL);

    CREATE TABLE prices(
      provider TEXT, model TEXT, input REAL, output REAL, cache_read REAL, cache_write REAL,
      tiers TEXT, fetched_at REAL, PRIMARY KEY(provider, model)
    );

    CREATE TABLE labels(kind TEXT, key TEXT, label TEXT, PRIMARY KEY(kind, key));

    CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT);

    CREATE TABLE sessions(
      harness TEXT NOT NULL, session_id TEXT NOT NULL, path TEXT, mtime REAL, size INT,
      parsed_at REAL, parse_error TEXT,
      title TEXT, cwd TEXT, project TEXT, git_branch TEXT, first_ts REAL, last_ts REAL,
      turns INT, peak_context INT, compactions INT, cache_rebuilds INT, tool_errors INT,
      usage_by_model TEXT, tools TEXT,
      PRIMARY KEY(harness, session_id)
    );
    CREATE INDEX sessions_last ON sessions(last_ts);
    CREATE TABLE session_agents(
      harness TEXT, session_id TEXT, agent_id TEXT, agent_type TEXT, description TEXT, model TEXT,
      turns INT, usage_by_model TEXT, PRIMARY KEY(harness, session_id, agent_id)
    );
    CREATE TABLE session_skills(
      harness TEXT, session_id TEXT, skill TEXT, loads INT, turns INT, usage_by_model TEXT,
      PRIMARY KEY(harness, session_id, skill)
    );
    CREATE TABLE session_prs(
      harness TEXT, session_id TEXT, repo TEXT, number INT, url TEXT, action TEXT, ts REAL,
      PRIMARY KEY(harness, session_id, url, action)
    );
    """,
    # v2: transcript usage per hour, so Overview and Breakdown can use transcripts
    # where the proxy has no data. Clearing mtime makes the scanner re-parse all.
    """
    CREATE TABLE transcript_usage(
      harness TEXT NOT NULL, session_id TEXT NOT NULL,
      usage_session TEXT NOT NULL, root_session TEXT NOT NULL,
      hour REAL NOT NULL, model TEXT NOT NULL, prompt INT NOT NULL,
      messages INT NOT NULL, input INT NOT NULL, output INT NOT NULL, cache_read INT NOT NULL,
      cache_write_5m INT NOT NULL, cache_write_1h INT NOT NULL,
      PRIMARY KEY(harness, session_id, usage_session, hour, model, prompt)
    );
    CREATE INDEX transcript_usage_hour ON transcript_usage(hour);
    UPDATE sessions SET mtime = NULL;
    """,
]


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")


class Database:
    """The writer connection is shared behind a lock. Readers open their own."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._writer = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        _configure(self._writer)
        if self.path != ":memory:":
            self._writer.execute("PRAGMA journal_mode = WAL")
            self._writer.execute("PRAGMA synchronous = NORMAL")
        self.migrate()

    def migrate(self) -> int:
        with self._lock:
            w = self._writer
            w.execute("CREATE TABLE IF NOT EXISTS schema_version(version INT)")
            row = w.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] or 0
            for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
                w.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _split(sql):
                        w.execute(stmt)
                    w.execute("INSERT INTO schema_version(version) VALUES (?)", (i,))
                    w.execute("COMMIT")
                except Exception:
                    w.execute("ROLLBACK")
                    raise
            return len(MIGRATIONS)

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A write transaction on the shared writer connection."""
        with self._lock:
            w = self._writer
            w.execute("BEGIN IMMEDIATE")
            try:
                yield w
                w.execute("COMMIT")
            except BaseException:
                w.execute("ROLLBACK")
                raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A short-lived read connection. In-memory databases share the writer."""
        if self.path == ":memory:":
            with self._lock:
                yield self._writer
            return
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None)
        try:
            _configure(conn)
            yield conn
        finally:
            conn.close()

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self.read() as r:
            row = r.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
        sql = "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        if conn is not None:
            conn.execute(sql, (key, value))
            return
        with self.write() as w:
            w.execute(sql, (key, value))

    def close(self) -> None:
        with self._lock:
            self._writer.close()


def _split(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]
