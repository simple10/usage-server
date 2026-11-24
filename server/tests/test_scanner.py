import os
import sys
import time
from pathlib import Path

from usage_server.config import Settings
from usage_server.transcripts.scanner import Scanner
from transcript_util import asst, write


def make(tmp_path, db, **kw):
    root = tmp_path / "transcripts"
    (root / "claude").mkdir(parents=True, exist_ok=True)
    s = Settings(data_dir=tmp_path, transcripts_dir=root, parser_timeout=5, parser_batch=20, **kw)
    sc = Scanner(db, s)
    sc.parser_cmd = [sys.executable, str(Path(__file__).parent / "fake_parser.py")]
    return sc, root / "claude"


def drain(sc):
    while sc._queue:
        sc.process_batch(sc._take(sc.s.parser_batch))


def rows(db):
    with db.read() as r:
        return {x["session_id"]: dict(x) for x in r.execute("SELECT * FROM sessions")}


def test_scan_parse_and_change_detection(tmp_path, db):
    sc, root = make(tmp_path, db)
    write(root / "p" / "s1.jsonl", [asst("m1", "2026-11-20T10:00:00Z")])
    write(root / "p" / "s2.jsonl", [asst("m1", "2026-11-20T10:00:00Z", out=7)])
    assert sc.scan_once() == 2
    drain(sc)
    r = rows(db)
    assert r["s1"]["turns"] == 1 and r["s2"]["parse_error"] is None
    assert sc.scan_once() == 0  # unchanged
    with open(root / "p" / "s1.jsonl", "a") as f:
        f.write('{"type":"assistant","timestamp":"2026-11-20T10:01:00Z","message":{"id":"m2","model":"x","usage":{"output_tokens":1}}}\n')
    assert sc.scan_once() == 1
    drain(sc)
    assert rows(db)["s1"]["turns"] == 2


def test_crash_and_hang_are_isolated(tmp_path, db):
    sc, root = make(tmp_path, db)
    for name in ("good1", "crash1", "hang1", "good2"):
        write(root / "p" / f"{name}.jsonl", [asst("m1", "2026-11-20T10:00:00Z")])
    sc.scan_once()
    t = time.time()
    drain(sc)
    el = time.time() - t
    r = rows(db)
    assert r["good1"]["turns"] == 1 and r["good2"]["turns"] == 1
    assert r["crash1"]["parse_error"] and "exit" in r["crash1"]["parse_error"]
    assert r["hang1"]["parse_error"] == "timeout"
    assert sc.stats["timeouts"] >= 1 and sc.stats["crashes"] >= 1
    assert el < 30
    # A failed file is not retried until it changes.
    assert sc.scan_once() == 0
    os.utime(root / "p" / "crash1.jsonl", (time.time() + 5, time.time() + 5))
    assert sc.scan_once() == 1


def test_backfill_window_and_order(tmp_path, db):
    sc, root = make(tmp_path, db, transcript_backfill_days=1)
    old = write(root / "p" / "old.jsonl", [asst("m1", "2026-01-01T00:00:00Z")])
    os.utime(old, (time.time() - 3 * 86400,) * 2)
    a = write(root / "p" / "a.jsonl", [asst("m1", "2026-11-20T10:00:00Z")])
    b = write(root / "p" / "b.jsonl", [asst("m1", "2026-11-20T10:00:00Z")])
    os.utime(a, (time.time() - 100,) * 2)
    assert sc.scan_once() == 2
    batch = sc._take(1)
    assert Path(batch[0][2]).stem == "b"  # newest first


def test_reparse(tmp_path, db):
    sc, root = make(tmp_path, db)
    write(root / "p" / "s1.jsonl", [asst("m1", "2026-11-20T10:00:00Z")])
    sc.scan_once(); drain(sc)
    assert sc.request_reparse("claude:s1") is True
    assert sc.status()["queued"] == 1
    drain(sc)
    assert sc.request_reparse("claude:nope") is False
    # After a forced re-parse the stored mtime is the real one: no endless re-queue.
    assert sc.scan_once() == 0
