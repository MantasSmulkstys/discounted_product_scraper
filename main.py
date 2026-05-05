#!/usr/bin/env python3
"""
Single entrypoint: scrape all configured stores into PostgreSQL, then run
AI normalization on pending raw offers.

Configuration: `.env` in this directory (see `.env.example`).
No CLI flags — run:

    python main.py
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from db import DBConfig  # noqa: E402
from env_loader import load_env  # noqa: E402
from run import run_scrape_pipeline  # noqa: E402
from scripts.process_raw_offers_normalized import run_normalization  # noqa: E402

load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> int:
    db_config = DBConfig(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "discount_tracker"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD"),
    )

    batch_size = int(os.getenv("NORMALIZE_BATCH_SIZE", "50"))
    processing_age = int(os.getenv("PROCESSING_AGE_MINUTES", "60"))

    logger.info("Starting scrape + AI normalization (single command).")

    try:
        logger.info("Scraping all stores to database…")
        run_scrape_pipeline(db_config=db_config)
        logger.info("Scrape finished.")
    except Exception:
        logger.exception("Scrape failed.")
        return 1

    try:
        logger.info("Normalizing with AI…")
        rc = run_normalization(
            db_config=db_config,
            limit=None,
            batch_size=batch_size,
            dry_run=False,
            use_ai=True,
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            retry_failed=True,
            processing_age_minutes=processing_age,
        )
        if rc != 0:
            logger.error("Normalization exited with code %s.", rc)
            return rc
        logger.info("Normalization finished.")
    except Exception:
        logger.exception("Normalization failed.")
        return 1

    logger.info("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
