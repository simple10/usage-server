import json
import time
import uuid

import pytest

from usage_server.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "usage.db")
    yield d
    d.close()


def rec(**kw):
    r = {
        "v": 1, "request_id": str(uuid.uuid4()), "trace_id": "t1",
        "ts": "2026-11-24T10:00:00.123456789Z", "provider": "claude", "executor": "ClaudeExecutor",
        "model": "claude-haiku-4-5-20251001", "api_key_hash": "a" * 64, "api_key_hint": "…abcd",
        "session_id": "claude:11111111-1111-1111-1111-111111111111", "auth_id": "claude:apikey:x",
        "auth_type": "apikey", "generate": True, "latency_ms": 100,
        "input": 10, "output": 20, "cache_read": 0, "cache_write": 0, "cache_write_5m": 0, "cache_write_1h": 0, "total": 30,
    }
    r.update(kw)
    return r


def line(**kw):
    return json.dumps(rec(**kw)) + "\n"


MODELS_DEV = {
    "anthropic": {"models": {
        "claude-haiku-4-5": {"cost": {"input": 1, "output": 5, "cache_read": 0.1, "cache_write": 1.25}},
        "claude-haiku-4-5-20251001": {"cost": {"input": 1, "output": 5, "cache_read": 0.1, "cache_write": 1.25}},
        "claude-sonnet-4-5": {"cost": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75,
                                       "context_over_200k": {"input": 6, "output": 22.5, "cache_read": 0.6, "cache_write": 7.5}}},
    }},
    "openai": {"models": {
        "gpt-5": {"cost": {"input": 1.25, "output": 10, "cache_read": 0.125}},
        "gpt-5.5": {"cost": {"input": 5, "output": 30, "cache_read": 0.5,
                             "tiers": [{"input": 10, "output": 45, "cache_read": 1, "tier": {"type": "context", "size": 272000}}]}},
    }},
    "someone": {"models": {"exotic-model": {"cost": {"input": 2, "output": 4}}, "broken": {"cost": {}}}},
}
