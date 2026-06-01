#!/usr/bin/env python3
"""Single entrypoint: scrape every configured store, then AI-normalize.

Output modes, chosen with `--o`:

    python main.py --o db     # scrape -> Postgres raw_offers -> AI normalize into the relational schema (default)
    python main.py --o json   # scrape -> AI normalize -> flat JSON array (no database)
    python main.py --o raw    # scrape only -> raw JSON for manual review (no AI, no database)

Use `--store` to limit to specific stores (repeatable), e.g. `--o raw --store iki`.

Configuration: `.env` in this directory (see `.env.example`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from db import DBConfig  # noqa: E402
from env_loader import load_env  # noqa: E402
from normalization.ai_normalize import load_allowed_categories_from_file  # noqa: E402
from normalization.json_normalize import (  # noqa: E402
    normalize_products_to_records,
    write_records_json,
)
from run import (  # noqa: E402
    _scrape_debug,
    _scrape_products,
    load_configs,
    print_debug_summary,
    print_summary,
    run_scrape_pipeline,
)
from scripts.process_raw_offers_normalized import (  # noqa: E402
    _get_openai_client,
    run_normalization,
)

load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CATEGORIES_FILE = _ROOT / "configs" / "categories.json"


def _db_config() -> DBConfig:
    return DBConfig(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "discount_tracker"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD"),
    )


def run_db(store_filter: list[str] | None = None) -> int:
    """Scrape into Postgres, then AI-normalize pending raw offers."""
    db_config = _db_config()
    batch_size = int(os.getenv("NORMALIZE_BATCH_SIZE", "50"))
    processing_age = int(os.getenv("PROCESSING_AGE_MINUTES", "60"))

    logger.info("Scraping stores to database…")
    run_scrape_pipeline(db_config=db_config, store_filter=store_filter)
    logger.info("Scrape finished. Normalizing with AI…")

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


def _load_and_scrape(store_filter: list[str] | None):
    configs = load_configs(store_filter)
    if not configs:
        raise ValueError("No configs found. Check the configs/ directory and --store filter.")
    logger.info("Loaded %d store configs: %s", len(configs), [c.store for c in configs])

    logger.info("Scraping stores…")
    products = _scrape_products(configs)
    print_summary(products)
    return products


def run_json(out_path: Path, store_filter: list[str] | None = None) -> int:
    """Scrape, AI-normalize each offer, and write a flat JSON array."""
    products = _load_and_scrape(store_filter)

    allowed_categories = load_allowed_categories_from_file(CATEGORIES_FILE)
    logger.info("Loaded %d allowed categories from %s", len(allowed_categories), CATEGORIES_FILE.name)

    openai_client = _get_openai_client(os.getenv("OPENAI_API_KEY"))
    if openai_client is None:
        logger.warning("No OpenAI client available; using deterministic fallback for every offer.")

    logger.info("Normalizing %d offers with AI…", len(products))
    records = normalize_products_to_records(products, allowed_categories, openai_client=openai_client)

    written = write_records_json(records, out_path)
    logger.info("Wrote %d normalized offers to %s", len(records), written)
    return 0


def run_raw(out_path: Path, store_filter: list[str] | None = None, debug: bool = False) -> int:
    """Scrape only — write raw scraped products to JSON for manual review.

    No AI, no database. Each item is a scraped Product exactly as the engine
    extracted it, so anomalies (bad prices, missing names, wrong dates, broken
    images) are visible before any normalization muddies them.

    With `debug=True`, write ONE entry per matched card — including cards that
    were skipped — each annotated with a `_skipped_reason` and HTML excerpt, so
    you can see products that never made it in (not just the ones that did).
    """
    configs = load_configs(store_filter)
    if not configs:
        raise ValueError("No configs found. Check the configs/ directory and --store filter.")
    logger.info("Loaded %d store configs: %s", len(configs), [c.store for c in configs])

    if debug:
        logger.info("Scraping stores (debug: including skipped cards)…")
        records = _scrape_debug(configs)
        print_debug_summary(records)
    else:
        logger.info("Scraping stores…")
        products = _scrape_products(configs)
        print_summary(products)
        records = [asdict(product) for product in products]

    out_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Wrote %d records to %s", len(records), out_path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape all stores, then AI-normalize.")
    parser.add_argument(
        "-o", "--o",
        dest="output",
        choices=["db", "json", "raw"],
        default="db",
        help="Output target: db (Postgres, default), json (normalized JSON), or raw (scrape only, no AI/DB).",
    )
    parser.add_argument(
        "--store",
        action="append",
        default=None,
        help="Limit to a store slug (e.g. iki). Repeat for several; omit for all.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output path for --o json/raw (default: normalized_offers.json / raw_scrape.json).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="With --o raw: include skipped cards + reasons (one entry per matched card).",
    )
    args = parser.parse_args()

    if args.debug and args.output != "raw":
        logger.warning("--debug only applies to --o raw; ignoring it.")

    logger.info("Starting (output=%s, stores=%s).", args.output, args.store or "all")
    try:
        if args.output == "json":
            return run_json(Path(args.out or "normalized_offers.json"), store_filter=args.store)
        if args.output == "raw":
            return run_raw(Path(args.out or "raw_scrape.json"), store_filter=args.store, debug=args.debug)
        return run_db(store_filter=args.store)
    except Exception:
        logger.exception("Run failed.")
        return 1
    finally:
        logger.info("Done.")


if __name__ == "__main__":
    raise SystemExit(main())
