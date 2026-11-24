"""Claude Code transcript adapter (SPEC §6).

Ported and adapted from internal reference parsers (see reference/, not
published). Rules kept from the reference:

- Claude Code writes one line per content block and every line repeats the
  message's `usage`: sum usage once per `message.id`.
- `cache_creation.ephemeral_{5m,1h}_input_tokens` split cache writes by TTL;
  without a split the whole write counts as 5-minute.
- Skills: loaded via `tool_use` name "Skill" (`input.skill`); token
  attribution via the assistant line's `attributionSkill`.
- Peak context of a turn = input + cache_read + cache_write.
- Cache rebuild = a turn writing >= 30k cache tokens after a gap longer than
  the cache TTL (5 min, or 1 h when `promptCacheRequested == "1h"`).

Privacy: no message text is kept. Titles, cwd, branch, subagent descriptions,
tool names and PR URLs only.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

HARNESS = "claude"
REBUILD_MIN_TOKENS = 30_000
SHORT_TTL_S = 300
LONG_TTL_S = 3600
# Entry types that never matter for a summary; skipped without a JSON parse.
SKIP_TYPES = (b'"type":"api-request', b'"type":"attachment"', b'"type":"queue-operation"',
              b'"type":"atis-latch"', b'"type":"last-prompt"', b'"type":"file-history-snapshot"',
              b'"type":"mode"', b'"type":"tools-used"', b'"type":"cost-state"', b'"type":"permission-mode"')
PR_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
PR_NUM = re.compile(r"\bgh\s+pr\s+merge\s+(?:[^\n]*?\s)?#?(\d+)\b")
REPO_FLAG = re.compile(r"(?:-R|--repo)[ =]([\w.-]+/[\w.-]+)")
MERGE_ERR = re.compile(r"(?i)\b(error|failed|not mergeable|could not|cannot|denied|refused|conflict)\b")
WORKTREE = [re.compile(r"/\.claude/worktrees/[^/]+/?.*$"), re.compile(r"/\.t3/worktrees/([^/]+)/[^/]+/?.*$")]
TS_FRACTION = re.compile(r"(\.\d{6})\d+")


@dataclass
class SessionFile:
    id: str
    path: Path
    mtime: float
    size: int


def parse_ts(v: Any) -> float | None:
    if not isinstance(v, str) or not v:
        return None
    s = TS_FRACTION.sub(r"\1", v.strip()).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def project_of(cwd: str) -> str:
    """Repository folder name from a cwd, with worktree suffixes removed.
    `/x/repo/.claude/worktrees/feat` -> repo; `/x/.t3/worktrees/repo/id` -> repo."""
    if not cwd:
        return ""
    c = cwd.rstrip("/")
    m = WORKTREE[1].search(c)
    if m:
        return m.group(1)
    c = WORKTREE[0].sub("", c)
    return os.path.basename(c.rstrip("/"))


def subagent_paths(session_path: Path) -> list[Path]:
    d = session_path.with_suffix("") / "subagents"
    return sorted(d.rglob("agent-*.jsonl")) if d.is_dir() else []


def read_meta(agent_path: Path) -> dict:
    try:
        data = json.loads(agent_path.with_suffix(".meta.json").read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def iter_entries(path: Path) -> Iterator[dict]:
    with open(path, "rb") as f:
        for line in f:
            head = line[:64]
            if any(t in head for t in SKIP_TYPES):
                continue
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                yield e


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _add_usage(target: dict, model: str, usage: dict) -> dict:
    cc = usage.get("cache_creation") or {}
    w1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    if not (w1 or w5) and written:
        w5 = written
    elif written > w1 + w5:
        w5 += written - w1 - w5
    u = target.setdefault(model, {"input": 0, "output": 0, "cache_read": 0, "cache_write_5m": 0, "cache_write_1h": 0})
    u["input"] += int(usage.get("input_tokens") or 0)
    u["output"] += int(usage.get("output_tokens") or 0)
    u["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
    u["cache_write_5m"] += w5
    u["cache_write_1h"] += w1
    return u


class FileStats:
    """Numbers from one transcript file (main or subagent)."""

    def __init__(self):
        self.title = ""
        self.ai_title = ""
        self.cwd = ""
        self.git_branch = ""
        self.first: float | None = None
        self.last: float | None = None
        self.turns = 0
        self.usage: dict[str, dict] = {}
        self.models: Counter = Counter()
        self.skills_loaded: Counter = Counter()
        self.skill_usage: dict[str, dict] = defaultdict(lambda: {"turns": 0, "usage": {}})
        self.tools: Counter = Counter()
        self.tool_errors = 0
        self.compactions = 0
        self.rebuilds = 0
        self.peak_context = 0
        self.prs: dict[tuple[str, str], dict] = {}  # (url, action) -> pr

    def add_pr(self, repo: str, number: int, action: str, ts: float | None):
        url = f"https://github.com/{repo}/pull/{number}"
        key = (url, action)
        if key not in self.prs:
            self.prs[key] = {"repo": repo, "number": number, "url": url, "action": action, "ts": ts}


def read_file(path: Path) -> FileStats:
    st = FileStats()
    seen: set[str] = set()
    pending: dict[str, tuple[str, dict, float | None]] = {}
    prev_turn_ts: float | None = None
    for e in iter_entries(path):
        et = e.get("type")
        if et == "custom-title":
            st.title = str(e.get("customTitle") or st.title)[:300]
            continue
        if et == "ai-title":
            st.ai_title = str(e.get("aiTitle") or st.ai_title)[:300]
            continue
        if et == "pr-link":
            m = PR_URL.search(str(e.get("prUrl") or ""))
            if m:
                st.add_pr(m.group(1), int(m.group(2)), "linked", parse_ts(e.get("timestamp")))
            continue
        if e.get("cwd") and not st.cwd:
            st.cwd = str(e["cwd"])[:500]
        if e.get("gitBranch"):
            st.git_branch = str(e["gitBranch"])[:200]
        ts = parse_ts(e.get("timestamp"))
        if ts is not None and et in ("user", "assistant", "system"):
            st.first = ts if st.first is None else min(st.first, ts)
            st.last = ts if st.last is None else max(st.last, ts)
        if et == "system" and e.get("subtype") == "compact_boundary":
            st.compactions += 1
        elif et == "assistant":
            msg = e.get("message") or {}
            mid = msg.get("id") or e.get("uuid")
            usage = msg.get("usage") or {}
            model = msg.get("model") or "?"
            if mid not in seen and model != "<synthetic>":
                seen.add(mid)
                st.turns += 1
                st.models[model] += 1
                _add_usage(st.usage, model, usage)
                skill = e.get("attributionSkill")
                if skill:
                    su = st.skill_usage[str(skill)[:200]]
                    su["turns"] += 1
                    _add_usage(su["usage"], model, usage)
                ctx = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                st.peak_context = max(st.peak_context, ctx)
                written = int(usage.get("cache_creation_input_tokens") or 0)
                if prev_turn_ts and ts and written >= REBUILD_MIN_TOKENS:
                    ttl = LONG_TTL_S if e.get("promptCacheRequested") == "1h" else SHORT_TTL_S
                    if ts - prev_turn_ts > ttl:
                        st.rebuilds += 1
                prev_turn_ts = ts or prev_turn_ts
            for b in msg.get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = str(b.get("name") or "?")[:200]
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                st.tools[name] += 1
                pending[b.get("id")] = (name, inp, ts)
                if name == "Skill" and inp.get("skill"):
                    st.skills_loaded[str(inp["skill"])[:200]] += 1
        elif et == "user":
            content = (e.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                name, inp, _ = pending.pop(b.get("tool_use_id"), ("?", {}, None))
                err = bool(b.get("is_error"))
                if err:
                    st.tool_errors += 1
                if name == "Bash":
                    _bash_prs(st, str(inp.get("command") or ""), _text(b.get("content")), err, ts)
    return st


def _bash_prs(st: FileStats, cmd: str, out: str, err: bool, ts: float | None):
    if "gh pr create" in cmd and not err:
        for m in PR_URL.finditer(out):
            st.add_pr(m.group(1), int(m.group(2)), "opened", ts)
            break
    if "gh pr merge" in cmd and not err and not MERGE_ERR.search(out):
        m = PR_URL.search(cmd) or PR_URL.search(out)
        if m:
            st.add_pr(m.group(1), int(m.group(2)), "merged", ts)
            return
        n = PR_NUM.search(cmd)
        repo = REPO_FLAG.search(cmd)
        if n and repo:
            st.add_pr(repo.group(1), int(n.group(1)), "merged", ts)
        elif n and (r := re.search(r"([\w.-]+/[\w.-]+)#" + n.group(1), out)):
            st.add_pr(r.group(1), int(n.group(1)), "merged", ts)


def _merge_usage(into: dict, other: dict):
    for model, u in other.items():
        t = into.setdefault(model, {k: 0 for k in u})
        for k, v in u.items():
            t[k] = t.get(k, 0) + v


def parse(path: Path) -> dict[str, Any]:
    """Summary for one session: the main transcript plus its subagents."""
    path = Path(path)
    main = read_file(path)
    agents = []
    total_usage = {k: dict(v) for k, v in main.usage.items()}
    tools = Counter(main.tools)
    skills: dict[str, dict] = {}

    def add_skills(fs: FileStats):
        for s, n in fs.skills_loaded.items():
            skills.setdefault(s, {"loads": 0, "turns": 0, "usage": {}})["loads"] += n
        for s, su in fs.skill_usage.items():
            d = skills.setdefault(s, {"loads": 0, "turns": 0, "usage": {}})
            d["turns"] += su["turns"]
            _merge_usage(d["usage"], su["usage"])

    add_skills(main)
    prs = dict(main.prs)
    tool_errors, compactions, rebuilds = main.tool_errors, main.compactions, main.rebuilds
    first, last = main.first, main.last
    for ap in subagent_paths(path):
        fs = read_file(ap)
        meta = read_meta(ap)
        agent_id = ap.stem[len("agent-"):] if ap.stem.startswith("agent-") else ap.stem
        agents.append({
            "agent_id": agent_id,
            "agent_type": str(meta.get("agentType") or "")[:200],
            "description": str(meta.get("description") or "")[:500],
            "model": str(meta.get("model") or (fs.models.most_common(1)[0][0] if fs.models else ""))[:200],
            "turns": fs.turns,
            "usage_by_model": fs.usage,
        })
        _merge_usage(total_usage, fs.usage)
        tools.update(fs.tools)
        add_skills(fs)
        prs.update({k: v for k, v in fs.prs.items() if k not in prs})
        tool_errors += fs.tool_errors
        compactions += fs.compactions
        rebuilds += fs.rebuilds
        if fs.first is not None:
            first = fs.first if first is None else min(first, fs.first)
        if fs.last is not None:
            last = fs.last if last is None else max(last, fs.last)
    # A PR opened or merged here is not also listed as merely linked.
    acted = {url for (url, action) in prs if action != "linked"}
    prs = {k: v for k, v in prs.items() if k[1] != "linked" or k[0] not in acted}
    return {
        "harness": HARNESS,
        "session_id": path.stem,
        "path": str(path),
        "title": main.title or main.ai_title,
        "cwd": main.cwd,
        "project": project_of(main.cwd),
        "git_branch": main.git_branch,
        "first_ts": first,
        "last_ts": last,
        "turns": main.turns,
        "peak_context": main.peak_context,
        "compactions": compactions,
        "cache_rebuilds": rebuilds,
        "tool_errors": tool_errors,
        "usage_by_model": total_usage,
        "tools": dict(tools),
        "agents": agents,
        "skills": [{"skill": s, "loads": d["loads"], "turns": d["turns"], "usage_by_model": d["usage"]} for s, d in sorted(skills.items())],
        "prs": sorted(prs.values(), key=lambda p: (p["ts"] or 0, p["url"], p["action"])),
    }


class ClaudeAdapter:
    harness = HARNESS

    def discover(self, root: Path, min_mtime: float = 0) -> Iterable[SessionFile]:
        """Session files under <root>/<project-slug>/<id>.jsonl. mtime and size
        include the session's subagent files, so a subagent change re-parses."""
        try:
            projects = [d for d in os.scandir(root) if d.is_dir()]
        except OSError:
            return
        for proj in projects:
            try:
                entries = list(os.scandir(proj.path))
            except OSError:
                continue
            for e in entries:
                if not e.name.endswith(".jsonl") or not e.is_file():
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                mtime, size = st.st_mtime, st.st_size
                sub = os.path.join(proj.path, e.name[:-6], "subagents")
                if os.path.isdir(sub):
                    for dirpath, _, files in os.walk(sub):
                        for f in files:
                            if f.startswith("agent-") and f.endswith(".jsonl"):
                                try:
                                    s2 = os.stat(os.path.join(dirpath, f))
                                except OSError:
                                    continue
                                mtime = max(mtime, s2.st_mtime)
                                size += s2.st_size
                if mtime < min_mtime:
                    continue
                yield SessionFile(e.name[:-6], Path(e.path), mtime, size)

    def parse(self, path: Path) -> dict[str, Any]:
        return parse(path)

    def session_key(self, proxy_session_id: str) -> str | None:
        """`claude:<uuid>[:agent:<id>]` -> `<uuid>`."""
        s = (proxy_session_id or "").strip()
        if not s.startswith("claude:"):
            return None
        s = s[len("claude:"):]
        return s.split(":agent:", 1)[0] or None
