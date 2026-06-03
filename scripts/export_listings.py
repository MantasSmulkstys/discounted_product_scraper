#!/usr/bin/env python3
"""Export the current catalog listings to a JSON file.

Read-only: it never writes to the database. Use it as a point-in-time backup
before a backfill/recluster, or to inspect what the live catalog looks like.

    python -m scripts.export_listings                       # -> current_listings_<ts>.json
    python -m scripts.export_listings --output catalog.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db import DBConfig  # noqa: E402
from env_loader import load_env  # noqa: E402

load_env()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# Each current_offer (latest price per store + variant) joined to the product,
# variant and the raw offer behind its current snapshot.
QUERY = """
    SELECT
        s.name                         AS store,
        np.id                          AS normalized_product_id,
        np.canonical_name              AS canonical_name,
        np.brand                       AS brand,
        pf.family_name                 AS family_name,
        pf.normalized_family_name      AS family_key,
        pv.id                          AS variant_id,
        pv.variant_name                AS variant_name,
        pv.size_value                  AS size_value,
        pv.size_unit                   AS size_unit,
        pv.package_count               AS package_count,
        pv.package_unit                AS package_unit,
        pv.variant_code                AS variant_code,
        co.price                       AS price,
        co.old_price                   AS old_price,
        co.week_starting               AS week_starting,
        co.last_seen_at                AS last_seen_at,
        os.condition                   AS condition,
        os.discount_percent            AS discount_percent,
        ro.name                        AS raw_name,
        ro.store_product_key           AS store_product_key,
        ro.url                         AS url,
        COALESCE(ro.image, np.image_url) AS image_url
    FROM current_offers co
    JOIN product_variants pv     ON pv.id = co.product_variant_id
    JOIN normalized_products np  ON np.id = pv.normalized_product_id
    JOIN stores s                ON s.id = co.store_id
    LEFT JOIN product_families pf ON pf.id = np.family_id
    LEFT JOIN offer_snapshots os ON os.id = co.last_snapshot_id
    LEFT JOIN raw_offers ro      ON ro.id = os.raw_offer_id
    ORDER BY s.name, np.canonical_name, pv.id
"""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if hasattr(value, "isoformat"):  # date
        return value.isoformat()
    return str(value)


def _connect(config: DBConfig):
    import psycopg

    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=config.password,
    )


def export(config: DBConfig, output_path: Path) -> int:
    conn = _connect(config)
    try:
        with conn.cursor() as cur:
            cur.execute(QUERY)
            columns = [col.name for col in cur.description]
            listings = [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        conn.close()

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(listings),
        "listings": listings,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    logger.info("Wrote %d listings to %s", len(listings), output_path)
    return 0


def main() -> int:
    default_output = PROJECT_ROOT / f"current_listings_{datetime.now():%Y%m%d_%H%M%S}.json"
    parser = argparse.ArgumentParser(description="Export current catalog listings to JSON (read-only).")
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--db-host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PGPORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PGDATABASE", "discount_tracker"))
    parser.add_argument("--db-user", default=os.getenv("PGUSER", "postgres"))
    parser.add_argument("--db-password", default=os.getenv("PGPASSWORD"))
    args = parser.parse_args()

    return export(
        DBConfig(
            host=args.db_host,
            port=args.db_port,
            dbname=args.db_name,
            user=args.db_user,
            password=args.db_password,
        ),
        args.output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
