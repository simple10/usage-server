import pytest

from usage_server.prices import Price, Prices, cost, normalize_model, parse_models_dev
from conftest import MODELS_DEV


@pytest.fixture
def prices(db):
    p = Prices(db, "http://unused", {"claude": "anthropic", "codex": "openai", "gemini": "google"})
    assert p.sync(fetch=lambda: MODELS_DEV) > 0
    return p


def test_parse_skips_bad_entries():
    rows = parse_models_dev(MODELS_DEV)
    names = {(r[0], r[1]) for r in rows}
    assert ("someone", "broken") not in names
    sonnet = [r for r in rows if r[1] == "claude-sonnet-4-5"][0]
    assert '"size": 200000' in sonnet[6]


def test_normalize_model():
    assert normalize_model("claude-opus-4-5-20251101[1m]")[:2] == ["claude-opus-4-5-20251101", "claude-opus-4-5"]
    assert normalize_model("anthropic/claude-haiku-4-5")[0] == "claude-haiku-4-5"
    assert normalize_model("") == []


def test_lookup(prices):
    assert prices.lookup("claude", "claude-haiku-4-5-20251001").model == "claude-haiku-4-5-20251001"
    assert prices.lookup("claude", "claude-sonnet-4-5-20250929[1m]").model == "claude-sonnet-4-5"
    assert prices.lookup("codex", "gpt-5").provider == "openai"
    assert prices.lookup("weird", "exotic-model").provider == "someone"  # search other providers
    assert prices.lookup("claude", "no-such-model") is None


def test_sync_failure_keeps_last_good(prices):
    def boom():
        raise OSError("network down")
    assert prices.sync(fetch=boom) == 0
    assert prices.last_error and "network down" in prices.last_error
    assert prices.lookup("claude", "claude-haiku-4-5") is not None
    assert prices.sync(fetch=lambda: {}) == 0  # empty response is a failure too
    assert prices.status()["count"] > 0


def test_cost_formula():
    p = Price("anthropic", "m", input=3, output=15, cache_read=0.3, cache_write=3.75)
    c = cost(p, input=1_000_000, output=1_000_000, cache_read=1_000_000, cache_write_5m=1_000_000, cache_write_1h=1_000_000)
    assert c == pytest.approx(3 + 15 + 0.3 + 3.75 + 6)
    # Missing cache prices fall back.
    q = Price("x", "m", input=2, output=4)
    assert cost(q, 0, 0, 1_000_000, 1_000_000, 0) == pytest.approx(2 + 2.5)


def test_tiers(prices):
    p = prices.lookup("codex", "gpt-5.5")
    assert p.for_prompt(100_000) is p
    t = p.for_prompt(300_000)
    assert (t.input, t.output, t.cache_read) == (10, 45, 1)
