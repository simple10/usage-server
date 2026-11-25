"""Overview/Breakdown merge rule: per root session and hour, the proxy wins when
it has any record; otherwise the transcript counts."""
import pytest

from usage_server import queries
from usage_server.config import Settings
from usage_server.prices import Prices, cost
from usage_server.transcripts.claude import parse, prompt_band
from usage_server.transcripts.scanner import Scanner
from conftest import MODELS_DEV
from transcript_util import asst, write

S1 = "aaaaaaaa-0000-0000-0000-000000000001"
S2 = "bbbbbbbb-0000-0000-0000-000000000002"
HOUR_A = 1795593600.0  # 2026-11-25T08:00:00Z
HOUR_B = HOUR_A + 4 * 3600


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def env(tmp_path, db):
    prices = Prices(db, "x", {"claude": "anthropic", "codex": "openai"})
    prices.sync(fetch=lambda: MODELS_DEV)
    sc = Scanner(db, Settings(data_dir=tmp_path, transcripts_dir=tmp_path / "t"))
    m = "claude-haiku-4-5"
    p1 = write(tmp_path / "t" / "proj" / f"{S1}.jsonl", [
        asst("a1", iso(HOUR_A + 60), model=m, inp=100, out=10),
        asst("a1", iso(HOUR_A + 60), model=m, inp=100, out=10),  # same message: counts once
        asst("a2", iso(HOUR_A + 120), model=m, inp=100, out=10),
        asst("b1", iso(HOUR_B + 60), model=m, inp=7, out=7),       # hour B: proxy has data
    ])
    write(p1.with_suffix("") / "subagents" / "agent-x1.jsonl", [asst("s1", iso(HOUR_B + 600), model=m, inp=5, out=5)])
    p2 = write(tmp_path / "t" / "proj" / f"{S2}.jsonl", [
        # A sonnet turn with a 250k prompt: priced at the over-200k tier.
        asst("c1", iso(HOUR_A + 300), model="claude-sonnet-4-5", inp=1000, out=100, cr=249_000),
    ])
    for p in (p1, p2):
        sc.store("claude", str(p), 1.0, 1, {"ok": True, "summary": parse(p)})

    def proxy(ts, session, model=m, provider="claude", inp=1000, out=100, key="k" * 64):
        with db.write() as w:
            w.execute("INSERT INTO requests(request_id, ts, provider, model, api_key_hash, auth_id, session_id, failed, "
                      "input, output, cache_read, cache_write, cache_write_5m, cache_write_1h) VALUES (?,?,?,?,?,?,?,0,?,?,0,0,0,0)",
                      (f"r{ts}{session}", ts, provider, model, key, "auth1", session, inp, out))
    proxy(HOUR_B + 1800, f"claude:{S1}:agent:x1")          # one proxy record in hour B, from a subagent
    proxy(HOUR_A + 900, "codex:zzz", model="gpt-5", provider="codex", inp=50, out=5)
    return db, prices, (HOUR_A - 3600, HOUR_A + 86400)


def agg(db, prices, rng, group, **f):
    with db.read() as r:
        return {k[0] if k else None: a for k, a in queries.aggregate(r, prices, *rng, group, f).items()}


def test_prompt_band():
    assert prompt_band(1000) == 0 and prompt_band(200_000) == 128_001 and prompt_band(250_000) == 200_001


def test_proxy_wins_per_session_hour(env):
    db, prices, rng = env
    by_src = agg(db, prices, rng, "source")
    # Transcript: S1 hour A (2 messages; hour B dropped, main and subagent) + S2 (1 message).
    assert by_src["transcript"]["requests"] == 3
    assert by_src["transcript"]["input"] == 100 + 100 + 1000
    # Proxy: the S1 subagent record + the codex record.
    assert by_src["proxy"]["requests"] == 2 and by_src["proxy"]["input"] == 1000 + 50
    hours = agg(db, prices, rng, "hour", session=f"claude:{S1}")
    assert hours["2026-11-25T08:00"]["requests"] == 2
    assert hours["2026-11-25T12:00"]["requests"] == 1 and hours["2026-11-25T12:00"]["input"] == 1000


def test_source_filter_and_groups(env):
    db, prices, rng = env
    assert set(agg(db, prices, rng, "source", source="proxy")) == {"proxy"}
    assert set(agg(db, prices, rng, "source", source="transcript")) == {"transcript"}
    keys = agg(db, prices, rng, "api_key")
    assert set(keys) == {queries.TRANSCRIPT_GROUP, "k" * 64}
    with db.read() as r:
        assert queries.describe(r, "api_key", queries.TRANSCRIPT_GROUP, {})["label"] == "(transcript)"
    assert agg(db, prices, rng, "source", api_key=queries.TRANSCRIPT_GROUP).keys() == {"transcript"}
    proj = agg(db, prices, rng, "project")
    assert proj["myrepo"]["requests"] == 2 + 1 + 1  # S1 hour A + S1 proxy + S2


def test_transcript_tier_pricing(env):
    db, prices, rng = env
    a = agg(db, prices, rng, "model", source="transcript")["claude-sonnet-4-5"]
    tier = prices.lookup("claude", "claude-sonnet-4-5").for_prompt(250_000)
    assert a["cost"] == pytest.approx(cost(tier, 1000, 100, 249_000, 0, 0))


def test_rollup_blocks_transcript_before_raw_cutoff(env):
    db, prices, rng = env
    with db.write() as w:
        db.kv_set("raw_cutoff", str(HOUR_A + 86400), conn=w)
        w.execute("INSERT INTO daily_rollup(day, api_key_hash, auth_id, provider, model, session_id, requests) "
                  "VALUES ('2026-11-25', 'k', 'a', 'claude', 'm', ?, 1)", (f"claude:{S2}",))
    assert agg(db, prices, rng, "session", source="transcript").keys() == {f"claude:{S1}"}


def test_session_views_stay_proxy_only(env):
    db, prices, rng = env
    with db.read() as r:
        d = queries.session_detail(r, prices, f"claude:{S1}")
        assert d["proxy"]["totals"]["requests"] == 1
        rows = {x["id"]: x for x in queries.sessions_list(r, prices, *rng)}
    assert rows[f"claude:{S2}"]["proxy"] is None and rows[f"claude:{S2}"]["transcript"]
