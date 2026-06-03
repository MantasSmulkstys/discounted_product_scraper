#!/usr/bin/env python3
"""Re-split products that were merged before `variant_code` existed.

Two distinct articles that shared a normalized name + size (e.g. kettle models
"art. KT-S08" / "art. PK-3058B", or bouquets of 30 vs 9 stems) collapsed into
one product_variant and one price history — and a same-store/same-week collision
left the surviving snapshot with one offer's image and another's price.

The fix is `variant_code` in the variant identity. This script finds the
raw_offers whose deterministic `variant_code` no longer matches the variant they
currently sit on, and — only with --apply — resets just those rows'
`normalization_status` to 'pending'. It writes NO product data itself: the
normal, well-tested normalizer rebuilds them from `raw_offers` (the append-only
source of truth, which still holds both colliding offers), now splitting them by
code. Re-runs are cheap because the store_product_key cache reuses the existing
product/family for unchanged items (no AI), so only genuinely affected offers
are reconsidered.

Dry-run by default — it just reports what it would reset.

    python -m scripts.backfill_variant_codes                 # preview
    python -m scripts.backfill_variant_codes --apply         # reset affected rows to 'pending'
    # then rebuild:
    python -m scripts.process_raw_offers_normalized --use-ai
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db import DBConfig  # noqa: E402
from env_loader import load_env  # noqa: E402
from normalization.preprocess import detect_variant_code  # noqa: E402

load_env()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# Every normalized raw_offer with the variant_code of the variant its current
# snapshot points to (NULL when the offer lost a collision and has no snapshot).
QUERY = """
    SELECT ro.id            AS raw_offer_id,
           ro.name          AS name,
           pv.variant_code  AS current_code
    FROM raw_offers ro
    LEFT JOIN offer_snapshots os ON os.raw_offer_id = ro.id
    LEFT JOIN product_variants pv ON pv.id = os.product_variant_id
    WHERE ro.normalization_status = 'done'
"""


def _connect(config: DBConfig):
    import psycopg

    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=config.password,
    )


def find_affected(cur) -> list[dict[str, Any]]:
    """raw_offers whose deterministic code differs from where they currently
    sit. Only rows that SHOULD carry a code are touched (expected is not None),
    so plain groceries are never disturbed."""
    cur.execute(QUERY)
    columns = [col.name for col in cur.description]

    affected: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in cur.fetchall():
        record = dict(zip(columns, row))
        raw_offer_id = int(record["raw_offer_id"])
        if raw_offer_id in seen:
            continue

        expected = detect_variant_code(record.get("name") or "")
        current = record.get("current_code")
        if expected is not None and expected != current:
            seen.add(raw_offer_id)
            affected.append(
                {
                    "raw_offer_id": raw_offer_id,
                    "name": record.get("name"),
                    "current_code": current,
                    "expected_code": expected,
                }
            )

    return affected


def run(config: DBConfig, apply: bool, sample: int) -> int:
    conn = _connect(config)
    try:
        with conn.cursor() as cur:
            affected = find_affected(cur)

            logger.info("%d raw_offers need re-coding.", len(affected))
            for record in affected[:sample]:
                logger.info(
                    "  raw_offer=%s  %r  current=%r -> expected=%r",
                    record["raw_offer_id"],
                    (record["name"] or "")[:60],
                    record["current_code"],
                    record["expected_code"],
                )
            if len(affected) > sample:
                logger.info("  … and %d more.", len(affected) - sample)

            if not affected:
                logger.info("Nothing to do.")
                return 0

            if not apply:
                logger.info(
                    "Dry-run: database unchanged. Re-run with --apply to reset these "
                    "to 'pending', then run process_raw_offers_normalized --use-ai."
                )
                return 0

            ids = [record["raw_offer_id"] for record in affected]
            cur.execute(
                """
                UPDATE raw_offers
                SET normalization_status = 'pending', normalized_at = NULL
                WHERE id = ANY(%s)
                """,
                (ids,),
            )
            conn.commit()
            logger.info("Reset %d raw_offers to 'pending'. Now run:", cur.rowcount or 0)
            logger.info("  python -m scripts.process_raw_offers_normalized --use-ai")
    finally:
        conn.close()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-split mis-merged variants by resetting affected raw_offers.")
    parser.add_argument("--apply", action="store_true", help="Reset affected raw_offers to 'pending'.")
    parser.add_argument("--sample", type=int, default=20, help="How many affected rows to print.")
    parser.add_argument("--db-host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PGPORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PGDATABASE", "discount_tracker"))
    parser.add_argument("--db-user", default=os.getenv("PGUSER", "postgres"))
    parser.add_argument("--db-password", default=os.getenv("PGPASSWORD"))
    args = parser.parse_args()

    return run(
        DBConfig(
            host=args.db_host,
            port=args.db_port,
            dbname=args.db_name,
            user=args.db_user,
            password=args.db_password,
        ),
        apply=args.apply,
        sample=args.sample,
    )


if __name__ == "__main__":
    raise SystemExit(main())
