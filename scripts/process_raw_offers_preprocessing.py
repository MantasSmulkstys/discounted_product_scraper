#!/usr/bin/env python3
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
from normalization.preprocess import preprocess_raw_offer  # noqa: E402


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


def _fetch_pending_raw_offers(cur, batch_size: int, offset: int = 0) -> list[dict[str, Any]]:
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
        WHERE raw_offers.normalization_status = %s
        ORDER BY raw_offers.scraped_at ASC NULLS LAST, raw_offers.id ASC
        LIMIT %s
        OFFSET %s
        """,
        ("pending", batch_size, offset),
    )
    rows = cur.fetchall()
    columns = [col.name for col in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def _mark_processing(cur, raw_offer_id: int) -> None:
    cur.execute(
        """
        UPDATE raw_offers
        SET normalization_status = %s,
            normalization_error = NULL,
            updated_at = NOW()
        WHERE id = %s
        """,
        ("processing", raw_offer_id),
    )


def _mark_done(cur, raw_offer_id: int, preprocessed: dict[str, Any]) -> None:
    cur.execute(
        """
        UPDATE raw_offers
        SET valid_from = %s,
            valid_to = %s,
            unit_price_value = %s,
            unit_price_unit = %s,
            normalization_status = %s,
            normalized_at = NOW(),
            normalization_error = NULL,
            updated_at = NOW()
        WHERE id = %s
        """,
        (
            preprocessed.get("valid_from"),
            preprocessed.get("valid_to"),
            preprocessed.get("unit_price_value"),
            preprocessed.get("unit_price_unit"),
            "done",
            raw_offer_id,
        ),
    )


def _mark_failed(cur, raw_offer_id: int, error_message: str) -> None:
    cur.execute(
        """
        UPDATE raw_offers
        SET normalization_status = %s,
            normalization_error = %s,
            updated_at = NOW()
        WHERE id = %s
        """,
        ("failed", error_message, raw_offer_id),
    )


def _truncate_error(error: str, max_len: int = 500) -> str:
    message = (error or "").strip()
    if len(message) <= max_len:
        return message
    return message[: max_len - 3] + "..."


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Process pending raw_offers with deterministic preprocessing only."
    )
    parser.add_argument("--batch-size", type=int, default=50, help="Rows fetched per batch.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of rows to process from pending queue.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write updates, only log what would be changed.",
    )
    parser.add_argument("--db-host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PGPORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PGDATABASE", "discount_tracker"))
    parser.add_argument("--db-user", default=os.getenv("PGUSER", "postgres"))
    parser.add_argument("--db-password", default=os.getenv("PGPASSWORD"))
    args = parser.parse_args()

    if args.batch_size <= 0:
        raise ValueError("--batch-size must be > 0")
    if args.limit is not None and args.limit < 0:
        raise ValueError("--limit must be >= 0")

    db_config = DBConfig(
        host=args.db_host,
        port=args.db_port,
        dbname=args.db_name,
        user=args.db_user,
        password=args.db_password,
    )

    total_selected = 0
    total_processed = 0
    total_succeeded = 0
    total_failed = 0
    total_skipped = 0

    try:
        with _connect(db_config) as conn:
            with conn.cursor() as cur:
                offset = 0
                batch_no = 0
                while True:
                    remaining = None if args.limit is None else args.limit - total_selected
                    if remaining is not None and remaining <= 0:
                        break

                    requested_batch = args.batch_size if remaining is None else min(args.batch_size, remaining)
                    batch = _fetch_pending_raw_offers(
                        cur,
                        batch_size=requested_batch,
                        offset=offset if args.dry_run else 0,
                    )
                    if not batch:
                        break

                    batch_no += 1
                    total_selected += len(batch)
                    logger.info(
                        "Starting batch %d with %d rows (dry_run=%s)",
                        batch_no,
                        len(batch),
                        args.dry_run,
                    )

                    for row in batch:
                        raw_offer_id = int(row["id"])
                        raw_name = row.get("name") or ""
                        logger.info("Processing raw_offer id=%s", raw_offer_id)
                        total_processed += 1

                        try:
                            if not args.dry_run:
                                _mark_processing(cur, raw_offer_id)
                                conn.commit()

                            preprocessed = preprocess_raw_offer(row)

                            if args.dry_run:
                                print(
                                    "[DRY-RUN] raw_offer_id={id} raw_name={name!r} "
                                    "valid_from={valid_from} valid_to={valid_to} "
                                    "unit_price_value={unit_price_value} unit_price_unit={unit_price_unit}".format(
                                        id=raw_offer_id,
                                        name=raw_name,
                                        valid_from=preprocessed.get("valid_from"),
                                        valid_to=preprocessed.get("valid_to"),
                                        unit_price_value=preprocessed.get("unit_price_value"),
                                        unit_price_unit=preprocessed.get("unit_price_unit"),
                                    )
                                )
                            else:
                                _mark_done(cur, raw_offer_id, preprocessed)
                                conn.commit()

                            logger.info(
                                "Done raw_offer id=%s name=%r valid_from=%s valid_to=%s unit_price_value=%s unit_price_unit=%s",
                                raw_offer_id,
                                raw_name,
                                preprocessed.get("valid_from"),
                                preprocessed.get("valid_to"),
                                preprocessed.get("unit_price_value"),
                                preprocessed.get("unit_price_unit"),
                            )
                            total_succeeded += 1
                        except Exception as exc:
                            total_failed += 1
                            error_message = _truncate_error(str(exc))
                            logger.exception("Failed raw_offer id=%s", raw_offer_id)

                            if args.dry_run:
                                print(
                                    f"[DRY-RUN] raw_offer_id={raw_offer_id} would be marked failed: {error_message}"
                                )
                                continue

                            conn.rollback()
                            try:
                                _mark_failed(cur, raw_offer_id, error_message)
                                conn.commit()
                            except Exception:
                                conn.rollback()
                                total_skipped += 1
                                logger.exception(
                                    "Could not mark failed for raw_offer id=%s", raw_offer_id
                                )

                    if args.dry_run:
                        offset += len(batch)

    except Exception:
        logger.exception("Fatal error during preprocessing run")
        return 1

    logger.info("Preprocessing run finished.")
    logger.info("total_selected=%d", total_selected)
    logger.info("total_processed=%d", total_processed)
    logger.info("total_succeeded=%d", total_succeeded)
    logger.info("total_failed=%d", total_failed)
    logger.info("total_skipped=%d", total_skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
