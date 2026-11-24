import json

from usage_server.transcripts.claude import ClaudeAdapter, parse, project_of
from transcript_util import asst, result, write

SID = "aaaaaaaa-0000-0000-0000-000000000001"


def session(tmp_path):
    p = tmp_path / "proj" / f"{SID}.jsonl"
    entries = [
        {"type": "custom-title", "customTitle": "My session"},
        {"type": "user", "timestamp": "2026-11-20T10:00:00Z", "message": {"content": "do a thing"}, "cwd": "/work/myrepo"},
        # One message written as two lines (text + tool_use): usage must count once.
        asst("m1", "2026-11-20T10:00:01Z", inp=100, out=50, cr=1000, cw=500, w5=100, w1=400, skill="pr-review"),
        asst("m1", "2026-11-20T10:00:01Z", inp=100, out=50, cr=1000, cw=500, w5=100, w1=400, skill="pr-review",
             tools=[("t1", "Skill", {"skill": "pr-review"}), ("t2", "Bash", {"command": "gh pr create --title x"})]),
        result("t1", "2026-11-20T10:00:02Z"),
        result("t2", "2026-11-20T10:00:03Z", out="https://github.com/acme/widgets/pull/42\n"),
        {"type": "api-request", "timestamp": "2026-11-20T10:00:03Z", "params": {"huge": "x" * 1000}},
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-11-20T10:05:00Z", "compactMetadata": {"preTokens": 150000}},
        # 20 minutes later, a 40k cache write: a cache rebuild (5m TTL).
        asst("m2", "2026-11-20T10:25:00Z", inp=5, out=5, cw=40000, tools=[("t3", "Bash", {"command": "gh pr merge 42 -R acme/widgets --squash"})]),
        result("t3", "2026-11-20T10:25:10Z", out="✓ Squashed and merged pull request acme/widgets#42"),
        asst("m3", "2026-11-20T10:26:00Z", tools=[("t4", "Bash", {"command": "gh pr merge 43 -R acme/widgets"})]),
        result("t4", "2026-11-20T10:26:05Z", out="X Pull request acme/widgets#43 is not mergeable", err=True),
        asst("m4", "2026-11-20T10:27:00Z", tools=[("t5", "Read", {"file_path": "/x"})]),
        result("t5", "2026-11-20T10:27:01Z", out="boom", err=True),
        {"type": "pr-link", "prUrl": "https://github.com/acme/widgets/pull/42", "prRepository": "acme/widgets", "prNumber": 42},
        {"type": "pr-link", "prUrl": "https://github.com/acme/other/pull/7", "prRepository": "acme/other", "prNumber": 7, "timestamp": "2026-11-20T10:28:00Z"},
        asst("synthetic", "2026-11-20T10:28:00Z", model="<synthetic>", inp=999),
        "not json",
    ]
    p.parent.mkdir(parents=True)
    p.write_text("".join((json.dumps(e) if isinstance(e, dict) else e) + "\n" for e in entries))
    sub = p.with_suffix("") / "subagents" / "agent-abc123.jsonl"
    write(sub, [asst("s1", "2026-11-20T10:01:00Z", model="claude-haiku-4-5", inp=1, out=2, cw=3, skill="pr-review")])
    sub.with_suffix(".meta.json").write_text(json.dumps({"agentType": "Explore", "description": "Find files", "model": "claude-haiku-4-5"}))
    return p


def test_parse_session(tmp_path):
    s = parse(session(tmp_path))
    assert s["session_id"] == SID and s["title"] == "My session"
    assert s["project"] == "myrepo" and s["git_branch"] == "feat-x"
    assert s["turns"] == 4  # m1..m4, synthetic excluded
    opus = s["usage_by_model"]["claude-opus-4-5"]
    assert opus == {"input": 100 + 5 + 10 + 10, "output": 50 + 5 + 20 + 20, "cache_read": 1000,
                    "cache_write_5m": 100 + 40000, "cache_write_1h": 400}
    assert s["usage_by_model"]["claude-haiku-4-5"]["cache_write_5m"] == 3  # subagent merged in
    assert s["peak_context"] == 100 + 1000 + 500 if s["peak_context"] < 40005 else s["peak_context"] == 40005
    assert s["compactions"] == 1 and s["cache_rebuilds"] == 1 and s["tool_errors"] == 2
    assert s["tools"] == {"Skill": 1, "Bash": 3, "Read": 1}
    assert s["agents"] == [{"agent_id": "abc123", "agent_type": "Explore", "description": "Find files", "model": "claude-haiku-4-5",
                            "turns": 1, "usage_by_model": {"claude-haiku-4-5": {"input": 1, "output": 2, "cache_read": 0, "cache_write_5m": 3, "cache_write_1h": 0}}}]
    sk = {k["skill"]: k for k in s["skills"]}
    assert sk["pr-review"]["loads"] == 1 and sk["pr-review"]["turns"] == 2  # main m1 + subagent s1
    prs = {(p["url"], p["action"]) for p in s["prs"]}
    assert prs == {("https://github.com/acme/widgets/pull/42", "opened"), ("https://github.com/acme/widgets/pull/42", "merged"),
                   ("https://github.com/acme/other/pull/7", "linked")}
    assert s["first_ts"] < s["last_ts"]


def test_no_message_text(tmp_path):
    s = parse(session(tmp_path))
    blob = json.dumps(s)
    assert "secret words" not in blob and "do a thing" not in blob and "boom" not in blob


def test_ai_title_fallback(tmp_path):
    p = write(tmp_path / "p" / "x.jsonl", [{"type": "ai-title", "aiTitle": "Auto"}, asst("m", "2026-11-20T10:00:00Z")])
    assert parse(p)["title"] == "Auto"


def test_project_of():
    assert project_of("/home/u/code/repo") == "repo"
    assert project_of("/home/u/code/repo/.claude/worktrees/feature") == "repo"
    assert project_of("/home/u/.t3/worktrees/repo/abc123") == "repo"
    assert project_of("/home/u/.t3/worktrees/repo/abc123/sub") == "repo"
    assert project_of("") == ""


def test_discover_and_key(tmp_path):
    p = session(tmp_path)
    ad = ClaudeAdapter()
    found = list(ad.discover(tmp_path))
    assert [f.id for f in found] == [SID]
    main_size = p.stat().st_size
    assert found[0].size > main_size  # includes the subagent file
    assert list(ad.discover(tmp_path, min_mtime=4e9)) == []
    assert ad.session_key(f"claude:{SID}") == SID
    assert ad.session_key(f"claude:{SID}:agent:abc123") == SID
    assert ad.session_key("codex:xyz") is None
