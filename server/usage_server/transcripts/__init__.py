"""Transcript adapters, one per harness (SPEC §6, §8)."""

from .claude import ClaudeAdapter

ADAPTERS = {"claude": ClaudeAdapter}


def adapter(harness: str):
    cls = ADAPTERS.get(harness)
    if cls is None:
        raise KeyError(f"no adapter for harness {harness!r}")
    return cls()
