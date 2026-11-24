"""Settings from environment variables (SPEC §5.2)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    return [p.strip() for p in os.environ.get(name, "").split(",") if p.strip()]


def _map(name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in _list(name):
        k, sep, v = item.partition("=")
        if sep and k.strip() and v.strip():
            out[k.strip().lower()] = v.strip().lower()
    return out


DEFAULT_PROVIDER_MAP = {"claude": "anthropic", "codex": "openai", "gemini": "google"}


@dataclass
class Settings:
    data_dir: Path = Path("/data")
    spool_dir: Path = Path("/spool")
    transcripts_dir: Path = Path("/transcripts")
    bridge_token: str = ""
    api_tokens: list[str] = field(default_factory=list)
    cpa_url: str = ""
    transcript_scan_seconds: int = 30
    transcript_backfill_days: int = 30
    raw_retention_days: int = 180
    prices_url: str = "https://models.dev/api.json"
    provider_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PROVIDER_MAP))
    spool_poll_seconds: float = 2.0
    parser_workers: int = 2
    parser_batch: int = 20
    parser_timeout: int = 120
    host: str = "0.0.0.0"
    port: int = 8080

    @property
    def db_path(self) -> Path:
        return self.data_dir / "usage.db"

    @classmethod
    def from_env(cls) -> "Settings":
        pm = dict(DEFAULT_PROVIDER_MAP)
        pm.update(_map("USAGE_PROVIDER_MAP"))
        return cls(
            data_dir=Path(os.environ.get("USAGE_DATA_DIR", "/data")),
            spool_dir=Path(os.environ.get("USAGE_SPOOL_DIR", "/spool")),
            transcripts_dir=Path(os.environ.get("USAGE_TRANSCRIPTS_DIR", "/transcripts")),
            bridge_token=os.environ.get("USAGE_BRIDGE_TOKEN", "").strip(),
            api_tokens=_list("USAGE_API_TOKENS"),
            cpa_url=os.environ.get("USAGE_CPA_URL", "").strip().rstrip("/"),
            transcript_scan_seconds=_int("USAGE_TRANSCRIPT_SCAN_SECONDS", 30),
            transcript_backfill_days=_int("USAGE_TRANSCRIPT_BACKFILL_DAYS", 30),
            raw_retention_days=_int("USAGE_RAW_RETENTION_DAYS", 180),
            prices_url=os.environ.get("USAGE_PRICES_URL", "https://models.dev/api.json").strip(),
            provider_map=pm,
            parser_workers=max(1, _int("USAGE_PARSER_WORKERS", 2)),
            parser_timeout=max(5, _int("USAGE_PARSER_TIMEOUT", 120)),
            host=os.environ.get("USAGE_HOST", "0.0.0.0"),
            port=_int("USAGE_PORT", 8080),
        )
