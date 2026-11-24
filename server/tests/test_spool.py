import json
import os

from usage_server.spool import SpoolReader, normalize, parse_ts, prune
from conftest import line, rec


def count(db, where="1=1"):
    with db.read() as r:
        return r.execute(f"SELECT COUNT(*) FROM requests WHERE {where}").fetchone()[0]


def test_reads_and_resumes(db, tmp_path):
    sp = tmp_path / "spool"; sp.mkdir()
    f = sp / "usage-20261124T100000-aaaaaaaa.jsonl"
    f.write_text(line() + line())
    r = SpoolReader(db, sp)
    assert r.run_once() == 2
    assert r.run_once() == 0
    with open(f, "a") as fh:
        fh.write(line())
    assert r.run_once() == 1
    assert count(db) == 3
    # A new reader (server restart) resumes from the saved offset.
    assert SpoolReader(db, sp).run_once() == 0


def test_partial_last_line_waits(db, tmp_path):
    sp = tmp_path / "spool"; sp.mkdir()
    f = sp / "usage-20261124T100000-aaaaaaaa.jsonl"
    full, part = line(), line(request_id="partial-1")
    f.write_text(full + part[:25])
    r = SpoolReader(db, sp)
    assert r.run_once() == 1
    assert r.stats["bad_lines"] == 0
    with open(f, "a") as fh:
        fh.write(part[25:])
    assert r.run_once() == 1
    assert count(db, "request_id = 'partial-1'") == 1


def test_bad_lines_skipped_and_counted(db, tmp_path):
    sp = tmp_path / "spool"; sp.mkdir()
    (sp / "usage-20261124T100000-aaaaaaaa.jsonl").write_text(
        line() + "{not json\n" + '{"v":2,"request_id":"x","ts":"2026-01-01T00:00:00Z"}\n'
        + '{"v":1,"ts":"2026-01-01T00:00:00Z"}\n' + '{"v":1,"request_id":"y","ts":"garbage"}\n'
        + "\xff\xfe\n" + "[]\n" + "\n" + line())
    r = SpoolReader(db, sp)
    assert r.run_once() == 2
    assert r.stats["bad_lines"] == 6
    assert db.kv_get("spool_bad_lines") == "6"
    assert SpoolReader(db, sp).stats["bad_lines"] == 6  # survives restart


def test_duplicate_request_id(db, tmp_path):
    sp = tmp_path / "spool"; sp.mkdir()
    l = line(request_id="dup")
    (sp / "usage-20261124T100000-aaaaaaaa.jsonl").write_text(l + l)
    (sp / "usage-20261124T110000-bbbbbbbb.jsonl").write_text(l)
    r = SpoolReader(db, sp)
    assert r.run_once() == 1
    assert r.stats["duplicates"] == 2
    with db.read() as c:
        assert c.execute("SELECT SUM(requests) FROM daily_rollup").fetchone()[0] == 1


def test_rotation_and_deleted_files(db, tmp_path):
    sp = tmp_path / "spool"; sp.mkdir()
    a = sp / "usage-20261124T100000-aaaaaaaa.jsonl"
    a.write_text(line())
    r = SpoolReader(db, sp)
    r.run_once()
    b = sp / "usage-20261124T110000-bbbbbbbb.jsonl"
    b.write_text(line() + line())
    assert r.run_once() == 2
    a.unlink()  # plugin retention deleted the old file
    r.run_once()
    with db.read() as c:
        files = [x[0] for x in c.execute("SELECT file FROM spool_offsets")]
    assert files == [b.name]
    assert count(db) == 3


def test_replaced_or_truncated_file_restarts(db, tmp_path):
    sp = tmp_path / "spool"; sp.mkdir()
    f = sp / "usage-20261124T100000-aaaaaaaa.jsonl"
    f.write_text(line(request_id="r1") + line(request_id="r2"))
    r = SpoolReader(db, sp)
    r.run_once()
    os.unlink(f)
    f.write_text(line(request_id="r3"))  # new inode, shorter
    assert r.run_once() == 1
    assert count(db) == 3


def test_missing_dir_and_ignored_names(db, tmp_path):
    r = SpoolReader(db, tmp_path / "nope")
    assert r.run_once() == 0
    sp = tmp_path / "spool"; sp.mkdir()
    (sp / "_plugin_status.json").write_text('{"written": 3, "version": "0.1.0"}')
    (sp / "_plugin_status.json.tmp").write_text("{")
    (sp / "other.jsonl").write_text(line())
    r = SpoolReader(db, sp)
    assert r.run_once() == 0
    assert r.plugin_status()["written"] == 3
    (sp / "_plugin_status.json").write_text("{partial")
    assert r.plugin_status() is None


def test_huge_line_without_newline(db, tmp_path, monkeypatch):
    import usage_server.spool as s
    monkeypatch.setattr(s, "MAX_LINE_BYTES", 1000)
    monkeypatch.setattr(s, "MAX_READ_BYTES", 1000)
    sp = tmp_path / "spool"; sp.mkdir()
    (sp / "usage-20261124T100000-aaaaaaaa.jsonl").write_text("x" * 3000 + "\n" + line())
    r = SpoolReader(db, sp)
    for _ in range(5):
        r.run_once()
    assert count(db) == 1
    assert r.stats["bad_lines"] >= 1


def test_lag(db, tmp_path):
    sp = tmp_path / "spool"; sp.mkdir()
    (sp / "usage-20261124T100000-aaaaaaaa.jsonl").write_text(line())
    r = SpoolReader(db, sp)
    assert r.lag()["pending_bytes"] > 0
    r.run_once()
    assert r.lag() == {"files": 1, "pending_bytes": 0}


def test_parse_ts():
    assert parse_ts("2026-11-24T10:00:00Z") == parse_ts("2026-11-24T10:00:00+00:00")
    assert abs(parse_ts("2026-11-24T10:00:00.123456789Z") - parse_ts("2026-11-24T10:00:00Z") - 0.123456) < 1e-6
    assert parse_ts("2026-11-24T11:00:00+01:00") == parse_ts("2026-11-24T10:00:00Z")
    assert parse_ts("nope") is None and parse_ts(None) is None


def test_normalize_tokens():
    # Anthropic: input excludes cache; keep as is. Unknown split -> all 5m.
    r = normalize(rec(input=10, output=5, cache_read=100, cache_write=50, total=165, cache_write_5m=None, cache_write_1h=None))
    assert (r["input"], r["cache_read"], r["cache_write_5m"], r["cache_write_1h"]) == (10, 100, 50, 0)
    # OpenAI-style: prompt includes cached tokens (total = input + output).
    r = normalize(rec(provider="codex", input=1000, output=50, cache_read=800, total=1050))
    assert r["input"] == 200
    # No total: provider decides.
    assert normalize(rec(provider="codex", input=1000, cache_read=800, total=0))["input"] == 200
    assert normalize(rec(provider="claude", input=1000, cache_read=800, total=0))["input"] == 1000
    # Split given.
    r = normalize(rec(cache_write=30, cache_write_5m=10, cache_write_1h=20))
    assert (r["cache_write_5m"], r["cache_write_1h"]) == (10, 20)
    r = normalize(rec(cache_write=30, cache_write_5m=None, cache_write_1h=20))
    assert r["cache_write_5m"] == 10
    # `cached` is ignored.
    assert normalize(rec(cached=999))["cache_read"] == 0
    assert normalize(rec(ratelimit={"anthropic-ratelimit-unified-5h-utilization": "0.2"}))["ratelimit"]


def test_prune_keeps_rollup(db, tmp_path):
    sp = tmp_path / "spool"; sp.mkdir()
    (sp / "usage-20261124T100000-aaaaaaaa.jsonl").write_text(line(ts="2020-01-01T00:00:00Z") + line())
    SpoolReader(db, sp).run_once()
    assert prune(db, 180) == 1
    assert count(db) == 1
    with db.read() as c:
        assert c.execute("SELECT SUM(requests) FROM daily_rollup").fetchone()[0] == 2
