#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os

from db import DBConfig
from run import run_scrape_pipeline
from scripts.process_raw_offers_normalized import run_normalization


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run weekly scraper pipeline: scrape to DB, then normalize pending raw offers."
    )
    parser.add_argument("--db-host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PGPORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PGDATABASE", "discount_tracker"))
    parser.add_argument("--db-user", default=os.getenv("PGUSER", "postgres"))
    parser.add_argument("--db-password", default=os.getenv("PGPASSWORD"))

    parser.add_argument("--dry-run-normalization", action="store_true")
    parser.add_argument("--normalize-limit", type=int, default=None)
    parser.add_argument("--normalize-batch-size", type=int, default=50)
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--openai-api-key", default=os.getenv("OPENAI_API_KEY"))
    args = parser.parse_args()

    db_config = DBConfig(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
    )

    logger.info("Starting full pipeline.")

    try:
        logger.info("Scraping stage started.")
        run_scrape_pipeline(
            output="db",
            db_config=db_config,
            dry_run=False,
        )
        logger.info("Scraping stage finished.")
    except Exception:
        logger.exception("Scraping stage failed.")
        return 1

    try:
        logger.info("Normalization stage started.")
        normalize_rc = run_normalization(
            db_config=db_config,
            limit=args.normalize_limit,
            batch_size=args.normalize_batch_size,
            dry_run=args.dry_run_normalization,
            use_ai=not args.no_ai,
            openai_api_key=args.openai_api_key,
        )
        if normalize_rc != 0:
            logger.error("Normalization stage failed with exit code %d.", normalize_rc)
            return normalize_rc
        logger.info("Normalization stage finished.")
    except Exception:
        logger.exception("Normalization stage failed.")
        return 1

    logger.info("Pipeline finished successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
