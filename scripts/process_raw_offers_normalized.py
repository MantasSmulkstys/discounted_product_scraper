#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from uscraper.db import DBConfig
    from uscraper.env_loader import load_env
    from uscraper.normalization.ai_normalize import (
        build_ai_input,
        build_fallback_ai_result,
        load_allowed_categories,
        normalize_identity_text,
        normalize_with_ai,
        resolve_category_ids,
        validate_ai_output,
    )
    from uscraper.normalization.preprocess import preprocess_raw_offer
except ImportError:
    PROJECT_PARENT = Path(__file__).resolve().parents[2]
    if str(PROJECT_PARENT) not in sys.path:
        sys.path.insert(0, str(PROJECT_PARENT))
    from uscraper.db import DBConfig  # type: ignore[no-redef]
    from uscraper.env_loader import load_env  # type: ignore[no-redef]
    from uscraper.normalization.ai_normalize import (  # type: ignore[no-redef]
        build_ai_input,
        build_fallback_ai_result,
        load_allowed_categories,
        normalize_identity_text,
        normalize_with_ai,
        resolve_category_ids,
        validate_ai_output,
    )
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


def _fetch_candidates(cur, batch_size: int, offset: int = 0) -> list[dict[str, Any]]:
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
            raw_offers.scraped_at,
            raw_offers.normalization_status,
            raw_offers.normalized_product_id,
            raw_offers.product_variant_id
        FROM raw_offers
        LEFT JOIN stores ON stores.id = raw_offers.store_id
        WHERE
            raw_offers.normalization_status IS NULL
            OR raw_offers.normalization_status = %s
            OR (
                raw_offers.normalization_status = %s
                AND (
                    raw_offers.normalized_product_id IS NULL
                    OR raw_offers.product_variant_id IS NULL
                )
            )
        ORDER BY raw_offers.scraped_at ASC NULLS LAST, raw_offers.id ASC
        LIMIT %s
        OFFSET %s
        """,
        ("pending", "done", batch_size, offset),
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


def _get_openai_client(api_key: str | None):
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai package is not installed, falling back to deterministic mode.")
        return None
    return OpenAI(api_key=api_key)


def canonicalize_family_name(generic_name: str | None, canonical_name: str | None) -> str | None:
    source = generic_name if generic_name and generic_name.strip() else canonical_name
    if source is None:
        return None

    source_value = source.strip()
    if not source_value:
        return None

    normalized_source = normalize_identity_text(source_value)
    canonical_map = {
        normalize_identity_text("katės ėdalas"): "Kačių ėdalas",
        normalize_identity_text("kačių ėdalas"): "Kačių ėdalas",
        normalize_identity_text("kačių pašaras"): "Kačių ėdalas",
        normalize_identity_text("šunų ėdalas"): "Šunų ėdalas",
        normalize_identity_text("šunų pašaras"): "Šunų ėdalas",
        normalize_identity_text("gyvūnų ėdalas"): "Šunų ir kačių ėdalas",
        normalize_identity_text("šunų ir kačių ėdalas"): "Šunų ir kačių ėdalas",
        normalize_identity_text("šunų ir kačių pašaras"): "Šunų ir kačių ėdalas",
    }

    mapped_value = canonical_map.get(normalized_source)
    if mapped_value:
        return mapped_value

    if source_value[0].islower():
        return source_value[0].upper() + source_value[1:]
    return source_value


def _find_or_create_product_family(
    cur,
    product_data: dict[str, Any],
    category_id: int | None,
    subcategory_id: int | None,
    dry_run: bool,
) -> tuple[int | None, str]:
    family_name = canonicalize_family_name(
        product_data.get("generic_name"),
        product_data.get("canonical_name"),
    )
    normalized_family_name = normalize_identity_text(str(family_name or ""))
    if not normalized_family_name:
        raise ValueError("family_name is required for product family upsert")

    cur.execute(
        """
        SELECT id
        FROM product_families
        WHERE normalized_family_name = %s
        LIMIT 1
        """,
        (normalized_family_name,),
    )
    row = cur.fetchone()
    if row:
        return int(row[0]), "reused"

    if dry_run:
        return None, "would_insert"

    cur.execute(
        """
        INSERT INTO product_families (
            family_name,
            normalized_family_name,
            category_id,
            subcategory_id,
            generic_name,
            created_at,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
        RETURNING id
        """,
        (
            family_name,
            normalized_family_name,
            category_id,
            subcategory_id,
            product_data.get("generic_name"),
        ),
    )
    inserted = cur.fetchone()
    if not inserted:
        raise RuntimeError("Failed to insert product_families row")
    return int(inserted[0]), "inserted"


def _find_or_create_normalized_product(
    cur,
    product_data: dict[str, Any],
    category_id: int | None,
    subcategory_id: int | None,
    normalization_version: str,
    preprocessed: dict[str, Any],
    family_id: int | None,
    dry_run: bool,
) -> tuple[int | None, str]:
    normalized_name = product_data.get("normalized_name")
    brand = product_data.get("brand")
    if not normalized_name:
        raise ValueError("normalized_name is required for product upsert")

    cur.execute(
        """
        SELECT id
        FROM normalized_products
        WHERE normalized_name = %s
          AND brand IS NOT DISTINCT FROM %s
          AND category_id IS NOT DISTINCT FROM %s
          AND subcategory_id IS NOT DISTINCT FROM %s
        ORDER BY id ASC
        LIMIT 1
        """,
        (normalized_name, brand, category_id, subcategory_id),
    )
    row = cur.fetchone()
    if row:
        return int(row[0]), "reused"

    if dry_run:
        return None, "would_insert"

    cur.execute(
        """
        INSERT INTO normalized_products (
            canonical_name,
            normalized_name,
            brand,
            generic_name,
            category_id,
            subcategory_id,
            family_id,
            ai_confidence,
            needs_review,
            normalization_version,
            source_name,
            image_url,
            created_at,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        RETURNING id
        """,
        (
            product_data.get("canonical_name") or preprocessed.get("clean_name") or "",
            normalized_name,
            brand,
            product_data.get("generic_name"),
            category_id,
            subcategory_id,
            family_id,
            product_data.get("confidence"),
            bool(product_data.get("needs_review", True)),
            normalization_version,
            preprocessed.get("raw_name"),
            preprocessed.get("image"),
        ),
    )
    inserted = cur.fetchone()
    if not inserted:
        raise RuntimeError("Failed to insert normalized_products row")
    return int(inserted[0]), "inserted"


def _build_variant_name(preprocessed: dict[str, Any]) -> str | None:
    size_value = preprocessed.get("detected_size_value")
    size_unit = preprocessed.get("detected_size_unit")
    package_count = preprocessed.get("detected_package_count")
    package_unit = preprocessed.get("detected_package_unit")

    parts: list[str] = []
    if size_value is not None and size_unit:
        parts.append(f"{size_value} {size_unit}")
    if package_count is not None and package_unit:
        parts.append(f"{package_count} {package_unit}")

    if not parts:
        return None
    return " / ".join(parts)


def _find_or_create_product_variant(
    cur,
    normalized_product_id: int | None,
    variant_data: dict[str, Any],
    preprocessed: dict[str, Any],
    dry_run: bool,
) -> tuple[int | None, str]:
    size_value = variant_data.get("size_value")
    size_unit = variant_data.get("size_unit")
    package_count = variant_data.get("package_count")
    package_unit = variant_data.get("package_unit")

    if normalized_product_id is None:
        return None, "would_insert"

    cur.execute(
        """
        SELECT id
        FROM product_variants
        WHERE normalized_product_id = %s
          AND size_value IS NOT DISTINCT FROM %s
          AND size_unit IS NOT DISTINCT FROM %s
          AND package_count IS NOT DISTINCT FROM %s
          AND package_unit IS NOT DISTINCT FROM %s
        ORDER BY id ASC
        LIMIT 1
        """,
        (normalized_product_id, size_value, size_unit, package_count, package_unit),
    )
    row = cur.fetchone()
    if row:
        return int(row[0]), "reused"

    if dry_run:
        return None, "would_insert"

    cur.execute(
        """
        INSERT INTO product_variants (
            normalized_product_id,
            variant_name,
            size_value,
            size_unit,
            package_count,
            package_unit,
            net_quantity_value,
            net_quantity_unit,
            attributes,
            created_at,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW(), NOW())
        RETURNING id
        """,
        (
            normalized_product_id,
            variant_data.get("variant_name") or _build_variant_name(preprocessed),
            size_value,
            size_unit,
            package_count,
            package_unit,
            variant_data.get("net_quantity_value"),
            variant_data.get("net_quantity_unit"),
            json.dumps(variant_data.get("attributes") or {}),
        ),
    )
    inserted = cur.fetchone()
    if not inserted:
        raise RuntimeError("Failed to insert product_variants row")
    return int(inserted[0]), "inserted"


def _find_existing_snapshot_by_raw_offer_id(cur, raw_offer_id: int) -> int | None:
    cur.execute(
        """
        SELECT id
        FROM offer_snapshots
        WHERE raw_offer_id = %s
        ORDER BY id ASC
        LIMIT 1
        """,
        (raw_offer_id,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _insert_offer_snapshot(
    cur,
    row: dict[str, Any],
    preprocessed: dict[str, Any],
    product_variant_id: int | None,
    dry_run: bool,
) -> tuple[int | None, str]:
    price = preprocessed.get("price")
    if price is None:
        return (None, "skipped_no_price")

    existing_snapshot_id = _find_existing_snapshot_by_raw_offer_id(cur, int(row["id"]))
    if existing_snapshot_id is not None:
        return existing_snapshot_id, "existing"

    if dry_run:
        return None, "would_insert"

    discount_percent = preprocessed.get("discount")
    old_price = preprocessed.get("old_price")
    is_promotion = old_price is not None or discount_percent is not None

    cur.execute(
        """
        INSERT INTO offer_snapshots (
            raw_offer_id,
            product_variant_id,
            store_id,
            price,
            old_price,
            discount_percent,
            loyalty_card,
            unit_price_value,
            unit_price_unit,
            valid_from,
            valid_to,
            scraped_at,
            is_promotion,
            created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        RETURNING id
        """,
        (
            row["id"],
            product_variant_id,
            row.get("store_id"),
            price,
            old_price,
            discount_percent,
            preprocessed.get("loyalty_card"),
            preprocessed.get("unit_price_value"),
            preprocessed.get("unit_price_unit"),
            preprocessed.get("valid_from"),
            preprocessed.get("valid_to"),
            row.get("scraped_at"),
            is_promotion,
        ),
    )
    inserted = cur.fetchone()
    if not inserted:
        raise RuntimeError("Failed to insert offer_snapshots row")
    return int(inserted[0]), "inserted"


def _update_raw_offer_done(
    cur,
    raw_offer_id: int,
    preprocessed: dict[str, Any],
    normalized_product_id: int | None,
    product_variant_id: int | None,
) -> None:
    cur.execute(
        """
        UPDATE raw_offers
        SET valid_from = %s,
            valid_to = %s,
            unit_price_value = %s,
            unit_price_unit = %s,
            normalized_product_id = %s,
            product_variant_id = %s,
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
            normalized_product_id,
            product_variant_id,
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
        description=(
            "Process raw_offers with deterministic fallback normalization into "
            "normalized_products, product_variants, and offer_snapshots."
        )
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--use-ai", action="store_true")
    parser.add_argument("--openai-api-key", default=os.getenv("OPENAI_API_KEY"))
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
    batch_no = 0
    offset = 0
    openai_client = _get_openai_client(args.openai_api_key) if args.use_ai else None
    allowed_categories: list[dict[str, Any]] = []

    try:
        with _connect(db_config) as conn:
            with conn.cursor() as cur:
                if args.use_ai:
                    allowed_categories = load_allowed_categories(cur)
                    logger.info("Loaded %d allowed categories for AI normalization.", len(allowed_categories))
                    if openai_client is None:
                        logger.warning("AI mode requested but no client available; deterministic fallback will be used.")

                while True:
                    remaining = None if args.limit is None else args.limit - total_selected
                    if remaining is not None and remaining <= 0:
                        break

                    requested_batch = args.batch_size if remaining is None else min(args.batch_size, remaining)
                    rows = _fetch_candidates(
                        cur,
                        batch_size=requested_batch,
                        offset=offset if args.dry_run else 0,
                    )
                    if not rows:
                        break

                    batch_no += 1
                    total_selected += len(rows)
                    logger.info(
                        "Batch %d start: %d rows (dry_run=%s)",
                        batch_no,
                        len(rows),
                        args.dry_run,
                    )

                    for row in rows:
                        raw_offer_id = int(row["id"])
                        raw_name = row.get("name") or ""
                        logger.info("Processing raw_offer id=%s", raw_offer_id)
                        total_processed += 1

                        try:
                            if not args.dry_run:
                                _mark_processing(cur, raw_offer_id)
                                conn.commit()

                            preprocessed = preprocess_raw_offer(row)
                            ai_used = False
                            ai_succeeded = False
                            normalization_version = "deterministic_v1"
                            chosen_result = build_fallback_ai_result(preprocessed)

                            if args.use_ai and openai_client is not None:
                                ai_used = True
                                try:
                                    ai_input = build_ai_input(preprocessed, allowed_categories)

                                    import time
                                    time.sleep(0.3)
                                    
                                    ai_raw = normalize_with_ai(openai_client, ai_input)
                                    ai_validated = validate_ai_output(ai_raw)
                                    chosen_result = ai_validated
                                    ai_succeeded = True
                                    normalization_version = "ai_v1"
                                except Exception as ai_exc:
                                    logger.warning(
                                        "AI normalization failed for raw_offer id=%s: %s. Falling back to deterministic.",
                                        raw_offer_id,
                                        ai_exc,
                                    )

                            category_id, subcategory_id = resolve_category_ids(
                                chosen_result,
                                allowed_categories,
                            )
                            category_name = chosen_result.get("category_name")
                            subcategory_name = chosen_result.get("subcategory_name")

                            family_id, family_action = _find_or_create_product_family(
                                cur,
                                product_data=chosen_result,
                                category_id=category_id,
                                subcategory_id=subcategory_id,
                                dry_run=args.dry_run,
                            )
                            normalized_product_id, product_action = _find_or_create_normalized_product(
                                cur,
                                product_data=chosen_result,
                                category_id=category_id,
                                subcategory_id=subcategory_id,
                                normalization_version=normalization_version,
                                preprocessed=preprocessed,
                                family_id=family_id,
                                dry_run=args.dry_run,
                            )
                            product_variant_id, variant_action = _find_or_create_product_variant(
                                cur,
                                normalized_product_id=normalized_product_id,
                                variant_data=chosen_result,
                                preprocessed=preprocessed,
                                dry_run=args.dry_run,
                            )
                            snapshot_id, snapshot_action = _insert_offer_snapshot(
                                cur,
                                row,
                                preprocessed,
                                product_variant_id,
                                dry_run=args.dry_run,
                            )

                            if args.dry_run:
                                logger.info(
                                    (
                                        "[DRY-RUN] id=%s ai_used=%s ai_succeeded=%s "
                                        "canonical_name=%r normalized_name=%r "
                                        "category=%r (%s) subcategory=%r (%s) "
                                        "family=%s product=%s variant=%s snapshot=%s valid_from=%s valid_to=%s "
                                        "unit_price_value=%s unit_price_unit=%s"
                                    ),
                                    raw_offer_id,
                                    ai_used,
                                    ai_succeeded,
                                    chosen_result.get("canonical_name"),
                                    chosen_result.get("normalized_name"),
                                    category_name,
                                    category_id,
                                    subcategory_name,
                                    subcategory_id,
                                    family_action,
                                    product_action,
                                    variant_action,
                                    snapshot_action,
                                    preprocessed.get("valid_from"),
                                    preprocessed.get("valid_to"),
                                    preprocessed.get("unit_price_value"),
                                    preprocessed.get("unit_price_unit"),
                                )
                            else:
                                if normalized_product_id is None:
                                    raise RuntimeError("normalized_product_id unexpectedly None")
                                if product_variant_id is None:
                                    raise RuntimeError("product_variant_id unexpectedly None")

                                _update_raw_offer_done(
                                    cur,
                                    raw_offer_id=raw_offer_id,
                                    preprocessed=preprocessed,
                                    normalized_product_id=normalized_product_id,
                                    product_variant_id=product_variant_id,
                                )
                                conn.commit()
                                logger.info(
                                    (
                                        "Done id=%s name=%r valid_from=%s valid_to=%s "
                                        "unit_price_value=%s unit_price_unit=%s "
                                        "ai_used=%s ai_succeeded=%s canonical_name=%r normalized_name=%r "
                                        "category=%r (%s) subcategory=%r (%s) "
                                        "family=%s product=%s variant=%s snapshot=%s"
                                    ),
                                    raw_offer_id,
                                    raw_name,
                                    preprocessed.get("valid_from"),
                                    preprocessed.get("valid_to"),
                                    preprocessed.get("unit_price_value"),
                                    preprocessed.get("unit_price_unit"),
                                    ai_used,
                                    ai_succeeded,
                                    chosen_result.get("canonical_name"),
                                    chosen_result.get("normalized_name"),
                                    category_name,
                                    category_id,
                                    subcategory_name,
                                    subcategory_id,
                                    family_action,
                                    product_action,
                                    variant_action,
                                    snapshot_action,
                                )
                            _ = snapshot_id  # kept for explicit flow/readability
                            total_succeeded += 1
                        except Exception as exc:
                            total_failed += 1
                            message = _truncate_error(str(exc))
                            logger.exception("Failed raw_offer id=%s", raw_offer_id)

                            if args.dry_run:
                                logger.error(
                                    "[DRY-RUN] id=%s would be marked failed: %s",
                                    raw_offer_id,
                                    message,
                                )
                                continue

                            conn.rollback()
                            try:
                                _mark_failed(cur, raw_offer_id, message)
                                conn.commit()
                            except Exception:
                                conn.rollback()
                                total_skipped += 1
                                logger.exception("Could not mark failed for id=%s", raw_offer_id)

                    if args.dry_run:
                        offset += len(rows)

    except Exception:
        logger.exception("Fatal error during normalization run")
        return 1

    logger.info("Normalization run finished.")
    logger.info("total_selected=%d", total_selected)
    logger.info("total_processed=%d", total_processed)
    logger.info("total_succeeded=%d", total_succeeded)
    logger.info("total_failed=%d", total_failed)
    logger.info("total_skipped=%d", total_skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
