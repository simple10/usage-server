import json

import pytest
from starlette.testclient import TestClient

from usage_server.api import App, ManagementKeyCheck
from usage_server.config import Settings
from usage_server.prices import Prices
from usage_server.spool import SpoolReader
from conftest import MODELS_DEV, line

S1 = "claude:11111111-1111-1111-1111-111111111111"


@pytest.fixture
def env(db, tmp_path):
    sp = tmp_path / "spool"; sp.mkdir()
    lines = [
        line(api_key_hash="k1" * 32, auth_id="authA", input=1_000_000, output=0, session_id=S1),
        line(api_key_hash="k1" * 32, auth_id="authA", output=1_000_000, input=0, session_id=S1 + ":agent:abc", parent_session_id=S1),
        line(api_key_hash="k2" * 32, auth_id="authB", model="gpt-5", provider="codex", input=1_000_000, output=0, total=1_000_000, session_id=""),
        line(api_key_hash="k2" * 32, auth_id="authB", model="mystery", input=5, output=5, failed=True, status=401, session_id=""),
        # Tiered: prompt above 200k tokens on Sonnet.
        line(api_key_hash="k1" * 32, auth_id="authA", model="claude-sonnet-4-5", input=300_000, output=0, total=300_000, session_id=S1),
    ]
    (sp / "usage-20261124T100000-aaaaaaaa.jsonl").write_text("".join(lines))
    (sp / "_plugin_status.json").write_text(json.dumps({"version": "0.1.0", "updated_at": "2026-11-24T10:00:00Z", "written": 5}))
    spool = SpoolReader(db, sp)
    spool.run_once()
    prices = Prices(db, "x", {"claude": "anthropic", "codex": "openai"})
    prices.sync(fetch=lambda: MODELS_DEV)
    settings = Settings(data_dir=tmp_path, spool_dir=sp, bridge_token="bridge-secret", api_tokens=["tok1", "tok2"], cpa_url="http://cpa")

    class FakeResp:
        def __init__(self, status): self.status = status
        def __enter__(self): return self
        def __exit__(self, *a): return False
    calls = []
    def opener(req):
        calls.append(req.get_header("Authorization"))
        if req.get_header("Authorization") == "Bearer mgmt-good":
            return FakeResp(200)
        import urllib.error
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
    app = App(settings, db, prices, spool, mgmt_check=ManagementKeyCheck("http://cpa", opener=opener))
    client = TestClient(app.starlette())
    client.calls = calls
    return client


H = {"Authorization": "Bearer tok1"}
Q = {"from": "2026-11-01", "to": "2026-12-01"}


def test_auth(env):
    assert env.get("/api/summary").status_code == 401
    assert env.get("/api/summary", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert env.get("/api/summary", headers=H, params=Q).status_code == 200
    assert env.get("/api/summary", headers={"Authorization": "Bearer tok2"}, params=Q).status_code == 200
    assert env.get("/api/summary", headers={"X-Usage-Bridge-Token": "bridge-secret"}, params=Q).status_code == 200
    assert env.get("/api/summary", headers={"X-Usage-Bridge-Token": "wrong"}, params=Q).status_code == 401
    # A wrong bridge token is not rescued by a valid bearer.
    assert env.get("/api/summary", headers={"X-Usage-Bridge-Token": "wrong", **H}, params=Q).status_code == 401
    # Management key via the proxy, cached.
    assert env.get("/api/summary", headers={"Authorization": "Bearer mgmt-good"}, params=Q).status_code == 200
    assert env.get("/api/summary", headers={"Authorization": "Bearer mgmt-good"}, params=Q).status_code == 200
    assert env.calls.count("Bearer mgmt-good") == 1
    # UI needs no auth and has no data.
    r = env.get("/ui/")
    assert r.status_code == 200 and "<html" in r.text and "Content-Security-Policy" in r.headers


def test_summary_costs(env):
    d = env.get("/api/summary", headers=H, params=Q).json()
    t = d["totals"]
    assert t["requests"] == 5 and t["failed"] == 1
    # haiku: 1M in ($1) + 1M out ($5); gpt-5: 1M in ($1.25); sonnet tier: 300k in at $6 = $1.8
    assert t["cost"] == pytest.approx(1 + 5 + 1.25 + 1.8)
    assert t["unpriced_requests"] == 1
    assert {r["key"] for r in d["top"]["api_key"]} == {"k1" * 32, "k2" * 32}


def test_breakdown_groups(env):
    rows = env.get("/api/breakdown", headers=H, params={**Q, "group": "session"}).json()["rows"]
    s = [r for r in rows if r["key"] == S1][0]
    assert s["requests"] == 3  # subagent rolls up into the root session
    rows = env.get("/api/breakdown", headers=H, params={**Q, "group": "auth"}).json()["rows"]
    assert {r["key"]: r["requests"] for r in rows} == {"authA": 3, "authB": 2}
    rows = env.get("/api/breakdown", headers=H, params={**Q, "group": "model", "api_key": "k2" * 32}).json()["rows"]
    assert {r["key"] for r in rows} == {"gpt-5", "mystery"}
    assert env.get("/api/breakdown", headers=H, params={**Q, "group": "bogus"}).status_code == 400
    assert env.get("/api/breakdown", headers=H, params={**Q, "group": "project"}).status_code == 200


def test_timeseries(env):
    d = env.get("/api/timeseries", headers=H, params={**Q, "bucket": "day", "group": "model", "top": 2}).json()
    assert len(d["buckets"]) == 30 and "2026-11-24" in d["buckets"]  # empty days included
    i = d["buckets"].index("2026-11-24")
    assert len(d["series"]) == 3 and d["series"][-1]["key"] == "__other__"
    assert all(v == 0 for s in d["series"] for j, v in enumerate(s["values"]) if j != i)
    total = sum(s["values"][i] for s in d["series"])
    assert total == pytest.approx(1 + 5 + 1.25 + 1.8)
    assert env.get("/api/timeseries", headers=H, params={**Q, "bucket": "week"}).status_code == 400


def test_sessions_and_detail(env):
    rows = env.get("/api/sessions", headers=H, params=Q).json()["rows"]
    assert [r["id"] for r in rows] == [S1]
    d = env.get("/api/session", headers=H, params={"id": S1}).json()
    assert d["proxy"]["totals"]["requests"] == 3
    assert {a["agent_id"] for a in d["proxy"]["by_agent"]} == {"main", "abc"}
    assert env.get("/api/session", headers=H, params={"id": "claude:nope"}).status_code == 404
    assert env.post("/api/session/reparse", headers=H, params={"id": S1}).status_code == 503


def test_labels(env):
    r = env.post("/api/labels", headers=H, content=json.dumps({"kind": "api_key", "key": "k1" * 32, "label": "laptop"}))
    assert r.status_code == 200 and r.json()["labels"][0]["label"] == "laptop"
    rows = env.get("/api/breakdown", headers=H, params={**Q, "group": "api_key"}).json()["rows"]
    assert any(x["label"] == "laptop" for x in rows)
    assert env.post("/api/labels", headers=H, content=json.dumps({"kind": "evil", "key": "x", "label": "y"})).status_code == 400
    assert env.post("/api/labels", headers=H, content="nope").status_code == 400
    env.post("/api/labels", headers=H, content=json.dumps({"kind": "api_key", "key": "k1" * 32, "label": ""}))
    assert env.get("/api/labels", headers=H).json()["labels"] == []


def test_prices_and_health(env):
    d = env.get("/api/prices", headers=H).json()
    assert [u["model"] for u in d["unknown"]] == ["mystery"]
    h = env.get("/api/health", headers=H).json()
    assert h["plugin"]["written"] == 5 and h["requests"]["count"] == 5 and h["prices"]["count"] > 0
