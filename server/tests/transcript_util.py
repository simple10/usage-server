import json
from pathlib import Path


def asst(mid, ts, model="claude-opus-4-5", inp=10, out=20, cr=0, cw=0, w5=None, w1=None, skill=None, tools=(), text="secret words", **kw):
    usage = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}
    if w5 is not None or w1 is not None:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": w5 or 0, "ephemeral_1h_input_tokens": w1 or 0}
    content = [{"type": "text", "text": text}] + [{"type": "tool_use", "id": tid, "name": n, "input": i} for tid, n, i in tools]
    e = {"type": "assistant", "timestamp": ts, "cwd": "/work/myrepo/.claude/worktrees/feat-x", "gitBranch": "feat-x",
         "message": {"id": mid, "model": model, "usage": usage, "content": content}}
    if skill:
        e["attributionSkill"] = skill
    e.update(kw)
    return e


def result(tid, ts, out="ok", err=False):
    return {"type": "user", "timestamp": ts, "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": err}]}}


def write(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return path
