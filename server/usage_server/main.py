"""Process entry point: start the background threads, then serve HTTP."""

from __future__ import annotations

import logging
import threading

import uvicorn

from .api import App
from .config import Settings
from .db import Database
from .prices import Prices
from .spool import SpoolReader, prune

log = logging.getLogger("usage_server")


def build(settings: Settings, start_threads: bool = True):
    db = Database(settings.db_path)
    prices = Prices(db, settings.prices_url, settings.provider_map)
    spool = SpoolReader(db, settings.spool_dir, settings.spool_poll_seconds)
    scanner = None
    try:
        from .transcripts.scanner import Scanner
        scanner = Scanner(db, settings)
    except ImportError:
        pass
    if start_threads:
        spool.start()
        prices.start()
        if scanner:
            scanner.start()
        stop = threading.Event()

        def pruner():
            while not stop.wait(3600):
                try:
                    n = prune(db, settings.raw_retention_days)
                    if n:
                        log.info("pruned %d raw rows", n)
                except Exception:
                    log.exception("prune failed")
        threading.Thread(target=pruner, name="pruner", daemon=True).start()
    return App(settings, db, prices, spool, scanner)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if not settings.bridge_token and not settings.api_tokens and not settings.cpa_url:
        log.warning("no USAGE_BRIDGE_TOKEN, USAGE_API_TOKENS or USAGE_CPA_URL set: every /api call will be refused")
    app = build(settings)
    uvicorn.run(app.starlette(), host=settings.host, port=settings.port, log_level="info",
                access_log=False, proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
