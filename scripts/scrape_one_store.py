#!/usr/bin/env python3
"""Scrape a single store into raw_offers. Used by the Laravel admin trigger.

    python -m scripts.scrape_one_store --store iki
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db import DBConfig  # noqa: E402
from env_loader import load_env  # noqa: E402
from run import run_scrape_pipeline  # noqa: E402

load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape a single store.")
    parser.add_argument("--store", required=True, help="Store slug (e.g. iki, maxima).")
    args = parser.parse_args()

    db_config = DBConfig(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "discount_tracker"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD"),
    )

    try:
        run_scrape_pipeline(store_filter=[args.store], db_config=db_config)
    except Exception:
        logger.exception("Scrape for store %s failed.", args.store)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
