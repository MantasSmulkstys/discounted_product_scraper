#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any


try:
    from uscraper.env_loader import load_env
    from uscraper.db import DBConfig
    from uscraper.normalization.preprocess import preprocess_raw_offer
except ImportError:
    import sys

    PROJECT_PARENT = Path(__file__).resolve().parents[2]
    if str(PROJECT_PARENT) not in sys.path:
        sys.path.insert(0, str(PROJECT_PARENT))
    from uscraper.env_loader import load_env  # type: ignore[no-redef]
    from uscraper.db import DBConfig  # type: ignore[no-redef]
    from uscraper.normalization.preprocess import preprocess_raw_offer  # type: ignore[no-redef]


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

load_env()


def _connect(config: DBConfig):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "This script requires psycopg. Install it with: pip install psycopg[binary]"
        ) from exc

    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=config.password,
    )


def _fetch_recent_raw_offers(config: DBConfig, limit: int) -> list[dict[str, Any]]:
    with _connect(config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    raw_offers.id,
                    raw_offers.store_id,
                    stores.name AS store_name,
                    raw_offers.name,
                    raw_offers.price,
                    raw_offers.old_price,
                    raw_offers.discount,
                    raw_offers.condition,
                    raw_offers.unit_price,
                    raw_offers.image,
                    raw_offers.url,
                    raw_offers.valid_dates,
                    raw_offers.loyalty_card,
                    raw_offers.scraped_at
                FROM raw_offers
                LEFT JOIN stores ON stores.id = raw_offers.store_id
                ORDER BY raw_offers.scraped_at DESC NULLS LAST, raw_offers.id DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
            columns = [col.name for col in cur.description]
            return [dict(zip(columns, row)) for row in rows]


def _print_preprocessed(raw_offer: dict[str, Any], parsed: dict[str, Any]) -> None:
    print("-" * 90)
    print(
        "raw_offer_id: "
        f"{parsed['raw_offer_id']} | store_id: {parsed['store_id']} | store_name: {parsed['store_name']}"
    )
    print(f"raw name:     {parsed['raw_name']}")
    print(f"clean name:   {parsed['clean_name']}")
    print(
        "valid dates:  "
        f"{parsed['valid_dates_raw']!r} -> from={parsed['valid_from']} to={parsed['valid_to']}"
    )
    print(
        "unit price:   "
        f"{parsed['unit_price_raw']!r} -> value={parsed['unit_price_value']} unit={parsed['unit_price_unit']}"
    )
    print(
        "detected qty: "
        f"size=({parsed['detected_size_value']}, {parsed['detected_size_unit']}), "
        f"package=({parsed['detected_package_count']}, {parsed['detected_package_unit']})"
    )
    print(
        "prices:       "
        f"price={parsed['price']} old_price={parsed['old_price']} discount={parsed['discount']}"
    )
    print(f"scraped_at:   {parsed['scraped_at']}")
    if not raw_offer.get("name"):
        print("note:         raw name was empty")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run deterministic preprocessing on recent raw_offers rows."
    )
    parser.add_argument("--limit", type=int, default=10, help="How many rows to inspect.")
    parser.add_argument("--db-host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PGPORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PGDATABASE", "discount_tracker"))
    parser.add_argument("--db-user", default=os.getenv("PGUSER", "postgres"))
    parser.add_argument("--db-password", default=os.getenv("PGPASSWORD"))
    args = parser.parse_args()

    db_config = DBConfig(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
    )

    try:
        rows = _fetch_recent_raw_offers(db_config, limit=args.limit)
    except Exception:
        logger.exception("Failed to load rows from raw_offers")
        return 1

    if not rows:
        logger.info("No rows found in raw_offers.")
        return 0

    print(f"Loaded {len(rows)} rows from raw_offers.")
    for row in rows:
        try:
            parsed = preprocess_raw_offer(row)
            _print_preprocessed(row, parsed)
        except Exception as exc:
            logger.exception("Row-level preprocessing error for raw_offer id=%s", row.get("id"))
            print(f"row id={row.get('id')} failed: {exc}")
            continue

    print("-" * 90)
    print("Dry-run completed. No data was written to database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
