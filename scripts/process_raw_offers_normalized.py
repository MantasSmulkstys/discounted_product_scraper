#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

try:
    import psycopg
except ImportError:
    psycopg = None  # The lazy import inside _connect() will surface a friendly error.


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from db import DBConfig  # noqa: E402
from env_loader import load_env  # noqa: E402
from normalization.ai_normalize import (  # noqa: E402
    build_ai_input,
    build_fallback_ai_result,
    load_allowed_categories,
    normalize_identity_text,
    normalize_with_ai,
    resolve_category_ids,
    validate_ai_output,
)
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


def _fetch_candidates(cur, batch_size: int, offset: int = 0) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            raw_offers.id,
            raw_offers.store_id,
            stores.name AS store_name,
            raw_offers.scrape_batch_id,
            scrape_batches.week_starting,
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
            raw_offers.content_hash
        FROM raw_offers
        LEFT JOIN stores ON stores.id = raw_offers.store_id
        LEFT JOIN scrape_batches ON scrape_batches.id = raw_offers.scrape_batch_id
        WHERE
            raw_offers.normalization_status IS NULL
            OR raw_offers.normalization_status = %s
        ORDER BY raw_offers.scraped_at ASC NULLS LAST, raw_offers.id ASC
        LIMIT %s
        OFFSET %s
        """,
        ("pending", batch_size, offset),
    )
    rows = cur.fetchall()
    columns = [col.name for col in cur.description]
    return [dict(zip(columns, row)) for row in rows]


def _lookup_existing_normalization(
    cur,
    store_id: int | None,
    content_hash: str | None,
    raw_offer_id: int,
) -> tuple[int, int] | None:
    """Cross-batch dedup shortcut.

    `content_hash` is a SHA256 of the raw scraped tuple (store, name, url,
    unit_price, condition) — deterministic across rescrapes. If we previously
    AI-normalized a raw_offer with the same `(store_id, content_hash)` and
    persisted it via offer_snapshots, reuse that `(normalized_product_id,
    product_variant_id)` directly. Skips the AI call and guarantees identical
    raw input maps to the same identity, even when the AI's brand/name output
    drifts between runs.
    """
    if not content_hash or store_id is None:
        return None

    cur.execute(
        """
        SELECT pv.normalized_product_id, pv.id
        FROM raw_offers ro
        JOIN offer_snapshots os ON os.raw_offer_id = ro.id
        JOIN product_variants pv ON pv.id = os.product_variant_id
        WHERE ro.store_id = %s
          AND ro.content_hash = %s
          AND ro.id <> %s
          AND ro.normalization_status = 'done'
        ORDER BY ro.normalized_at DESC NULLS LAST, ro.id DESC
        LIMIT 1
        """,
        (store_id, content_hash, raw_offer_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return int(row[0]), int(row[1])


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


def _recover_stale_rows(cur, processing_age_minutes: int = 60, retry_failed: bool = False) -> dict[str, int]:
    """
    Reset rows left behind by previous crashed runs back to `pending`.

    - `processing` rows older than `processing_age_minutes` are always reset
      (a worker that took >1h on a single row almost certainly died).
    - `failed` rows are reset only when `retry_failed=True` is explicitly
      passed, since some failures are deterministic (bad data) and re-running
      them just wastes AI calls.
    """
    cur.execute(
        """
        UPDATE raw_offers
        SET normalization_status = 'pending',
            normalization_error = NULL,
            updated_at = NOW()
        WHERE normalization_status = 'processing'
          AND updated_at < NOW() - make_interval(mins => %s)
        """,
        (processing_age_minutes,),
    )
    recovered_processing = cur.rowcount

    recovered_failed = 0
    if retry_failed:
        cur.execute(
            """
            UPDATE raw_offers
            SET normalization_status = 'pending',
                normalization_error = NULL,
                updated_at = NOW()
            WHERE normalization_status = 'failed'
            """
        )
        recovered_failed = cur.rowcount

    return {"processing": recovered_processing, "failed": recovered_failed}


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

    if dry_run:
        cur.execute(
            """
            SELECT id FROM normalized_products
            WHERE identity_key = %s || '|' || COALESCE(%s,'')
                || '|' || COALESCE(%s::text,'')
                || '|' || COALESCE(%s::text,'')
            LIMIT 1
            """,
            (normalized_name, brand, category_id, subcategory_id),
        )
        row = cur.fetchone()
        return (int(row[0]), "reused") if row else (None, "would_insert")

    # ON CONFLICT (identity_key) gives us a single-shot upsert: insert if new,
    # update-noop and return the existing id otherwise. Replaces the prior
    # SELECT + savepoint + retry dance.
    cur.execute(
        """
        INSERT INTO normalized_products (
            canonical_name, normalized_name, brand, generic_name,
            category_id, subcategory_id, family_id,
            ai_confidence, needs_review, normalization_version,
            source_name, image_url, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (identity_key) DO UPDATE SET updated_at = NOW()
        RETURNING id, (xmax = 0) AS inserted
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
    result = cur.fetchone()
    if not result:
        raise RuntimeError("Failed to upsert normalized_products row")
    return int(result[0]), "inserted" if result[1] else "reused"


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
    if normalized_product_id is None:
        return None, "would_insert"

    size_value = variant_data.get("size_value")
    size_unit = variant_data.get("size_unit")
    package_count = variant_data.get("package_count")
    package_unit = variant_data.get("package_unit")

    if dry_run:
        cur.execute(
            """
            SELECT id FROM product_variants
            WHERE identity_key = %s::text || '|' || COALESCE(%s::text,'')
                || '|' || COALESCE(%s,'')
                || '|' || COALESCE(%s::text,'')
                || '|' || COALESCE(%s,'')
            LIMIT 1
            """,
            (normalized_product_id, size_value, size_unit, package_count, package_unit),
        )
        row = cur.fetchone()
        return (int(row[0]), "reused") if row else (None, "would_insert")

    cur.execute(
        """
        INSERT INTO product_variants (
            normalized_product_id, variant_name,
            size_value, size_unit, package_count, package_unit,
            net_quantity_value, net_quantity_unit,
            attributes, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, NOW(), NOW())
        ON CONFLICT (identity_key) DO UPDATE SET updated_at = NOW()
        RETURNING id, (xmax = 0) AS inserted
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
    result = cur.fetchone()
    if not result:
        raise RuntimeError("Failed to upsert product_variants row")
    return int(result[0]), "inserted" if result[1] else "reused"


def _week_starting_for(scraped_at, fallback_week_starting):
    """Monday (ISO) of the snapshot's scrape week. Falls back to the batch's
    week_starting if the row has no scraped_at."""
    from datetime import date, datetime, timedelta

    if scraped_at is None:
        return fallback_week_starting
    if isinstance(scraped_at, datetime):
        d = scraped_at.date()
    elif isinstance(scraped_at, date):
        d = scraped_at
    else:
        return fallback_week_starting
    return d - timedelta(days=d.weekday())


def _insert_offer_snapshot(
    cur,
    row: dict[str, Any],
    preprocessed: dict[str, Any],
    product_variant_id: int | None,
    dry_run: bool,
) -> tuple[int | None, str, dict[str, Any] | None]:
    """Insert a snapshot and return (id, action, snapshot_payload).

    The snapshot_payload (or None if dry-run/skipped) is what the caller passes
    to `_upsert_current_offer` so the "latest known price" table stays
    consistent without a second DB roundtrip.
    """
    if product_variant_id is None:
        return (None, "skipped_no_variant", None)

    price = preprocessed.get("price")
    discount_percent = preprocessed.get("discount")
    old_price = preprocessed.get("old_price")
    condition = preprocessed.get("condition")

    # Show priceless promos too (e.g. "1+1", "Galioja su pigintuvu") as long
    # as they carry SOME signal — a price, a discount, or a condition. Pure
    # blanks would be junk so we still drop them.
    if price is None and discount_percent is None and not condition:
        return (None, "skipped_empty", None)

    is_promotion = old_price is not None or discount_percent is not None or bool(condition)
    week_starting = _week_starting_for(row.get("scraped_at"), row.get("week_starting"))

    payload = {
        "raw_offer_id": int(row["id"]),
        "product_variant_id": product_variant_id,
        "store_id": row.get("store_id"),
        "scrape_batch_id": row.get("scrape_batch_id"),
        "week_starting": week_starting,
        "price": price,
        "old_price": old_price,
        "discount_percent": discount_percent,
        "condition": condition,
        "loyalty_card": preprocessed.get("loyalty_card"),
        "unit_price_value": preprocessed.get("unit_price_value"),
        "unit_price_unit": preprocessed.get("unit_price_unit"),
        "valid_from": preprocessed.get("valid_from"),
        "valid_to": preprocessed.get("valid_to"),
        "scraped_at": row.get("scraped_at"),
        "is_promotion": is_promotion,
    }

    if dry_run:
        return (None, "would_insert", payload)

    # ON CONFLICT (variant, store, week) — re-running the normalizer for the
    # same raw_offer (or another raw_offer that resolves to the same variant in
    # the same week) is a no-op rather than an error. We update last-seen-style
    # fields so the snapshot reflects the freshest scrape if the price moved
    # mid-week.
    cur.execute(
        """
        INSERT INTO offer_snapshots (
            raw_offer_id, product_variant_id, store_id, scrape_batch_id,
            week_starting, price, old_price, discount_percent, condition, loyalty_card,
            unit_price_value, unit_price_unit, valid_from, valid_to,
            scraped_at, is_promotion, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT ON CONSTRAINT offer_snapshots_pvs_week_unique DO UPDATE SET
            price = EXCLUDED.price,
            old_price = EXCLUDED.old_price,
            discount_percent = EXCLUDED.discount_percent,
            condition = EXCLUDED.condition,
            loyalty_card = EXCLUDED.loyalty_card,
            unit_price_value = EXCLUDED.unit_price_value,
            unit_price_unit = EXCLUDED.unit_price_unit,
            valid_from = EXCLUDED.valid_from,
            valid_to = EXCLUDED.valid_to,
            scraped_at = EXCLUDED.scraped_at,
            is_promotion = EXCLUDED.is_promotion
        RETURNING id, (xmax = 0) AS inserted
        """,
        (
            payload["raw_offer_id"], payload["product_variant_id"], payload["store_id"],
            payload["scrape_batch_id"], payload["week_starting"], payload["price"],
            payload["old_price"], payload["discount_percent"], payload["condition"],
            payload["loyalty_card"], payload["unit_price_value"], payload["unit_price_unit"],
            payload["valid_from"], payload["valid_to"],
            payload["scraped_at"], payload["is_promotion"],
        ),
    )
    result = cur.fetchone()
    if not result:
        raise RuntimeError("Failed to upsert offer_snapshots row")
    payload["id"] = int(result[0])
    return (int(result[0]), "inserted" if result[1] else "updated", payload)


def _upsert_current_offer(cur, snapshot: dict[str, Any]) -> None:
    """Refresh `current_offers` to reflect this snapshot, and write a
    `price_changes` row when the price actually moved.

    Called only for snapshots whose week is the latest known week for this
    (store, variant). Older backfill snapshots should not overwrite a fresher
    current price.
    """
    cur.execute(
        """
        SELECT week_starting, price FROM current_offers
        WHERE store_id = %s AND product_variant_id = %s
        FOR UPDATE
        """,
        (snapshot["store_id"], snapshot["product_variant_id"]),
    )
    existing = cur.fetchone()

    if existing is not None:
        existing_week, existing_price = existing
        if existing_week is not None and snapshot["week_starting"] is not None and existing_week > snapshot["week_starting"]:
            # Backfill of an older week — don't touch current_offers.
            return
        previous_price = existing_price
    else:
        previous_price = None

    cur.execute(
        """
        INSERT INTO current_offers (
            store_id, product_variant_id, price, old_price, discount_percent, condition,
            is_promotion, loyalty_card, unit_price_value, unit_price_unit,
            valid_from, valid_to, last_seen_at, week_starting, last_snapshot_id, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (store_id, product_variant_id) DO UPDATE SET
            price = EXCLUDED.price,
            old_price = EXCLUDED.old_price,
            discount_percent = EXCLUDED.discount_percent,
            condition = EXCLUDED.condition,
            is_promotion = EXCLUDED.is_promotion,
            loyalty_card = EXCLUDED.loyalty_card,
            unit_price_value = EXCLUDED.unit_price_value,
            unit_price_unit = EXCLUDED.unit_price_unit,
            valid_from = EXCLUDED.valid_from,
            valid_to = EXCLUDED.valid_to,
            last_seen_at = EXCLUDED.last_seen_at,
            week_starting = EXCLUDED.week_starting,
            last_snapshot_id = EXCLUDED.last_snapshot_id,
            updated_at = NOW()
        """,
        (
            snapshot["store_id"], snapshot["product_variant_id"], snapshot["price"],
            snapshot["old_price"], snapshot["discount_percent"], snapshot.get("condition"),
            snapshot["is_promotion"], snapshot["loyalty_card"], snapshot["unit_price_value"],
            snapshot["unit_price_unit"], snapshot["valid_from"], snapshot["valid_to"],
            snapshot["scraped_at"], snapshot["week_starting"], snapshot["id"],
        ),
    )

    # price_changes only makes sense when both sides are real prices.
    if (
        previous_price is None
        or snapshot["price"] is None
        or previous_price == snapshot["price"]
    ):
        return

    change_pct = None
    try:
        if previous_price and float(previous_price) != 0:
            change_pct = round((float(snapshot["price"]) - float(previous_price)) / float(previous_price) * 100, 2)
    except (TypeError, ValueError):
        change_pct = None

    cur.execute(
        """
        INSERT INTO price_changes (
            product_variant_id, store_id, previous_price, new_price,
            change_pct, week_starting, changed_at, snapshot_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s)
        """,
        (
            snapshot["product_variant_id"], snapshot["store_id"],
            previous_price, snapshot["price"], change_pct,
            snapshot["week_starting"], snapshot["id"],
        ),
    )


def _update_raw_offer_done(
    cur,
    raw_offer_id: int,
    preprocessed: dict[str, Any],
) -> None:
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


def run_normalization(
    *,
    db_config: DBConfig,
    limit: int | None = None,
    batch_size: int = 50,
    dry_run: bool = False,
    use_ai: bool = False,
    openai_api_key: str | None = None,
    retry_failed: bool = False,
    processing_age_minutes: int = 60,
) -> int:
    if batch_size <= 0:
        raise ValueError("--batch-size must be > 0")
    if limit is not None and limit < 0:
        raise ValueError("--limit must be >= 0")
    if processing_age_minutes < 0:
        raise ValueError("--processing-age-minutes must be >= 0")

    total_selected = 0
    total_processed = 0
    total_succeeded = 0
    total_failed = 0
    total_skipped = 0
    batch_no = 0
    offset = 0
    openai_client = _get_openai_client(openai_api_key) if use_ai else None
    allowed_categories: list[dict[str, Any]] = []

    try:
        with _connect(db_config) as conn:
            with conn.cursor() as cur:
                if not dry_run:
                    recovered = _recover_stale_rows(
                        cur,
                        processing_age_minutes=processing_age_minutes,
                        retry_failed=retry_failed,
                    )
                    if recovered["processing"] or recovered["failed"]:
                        conn.commit()
                        logger.info(
                            "Recovered stale rows: processing=%d, failed=%d (retry_failed=%s)",
                            recovered["processing"],
                            recovered["failed"],
                            retry_failed,
                        )

                if use_ai:
                    allowed_categories = load_allowed_categories(cur)
                    logger.info("Loaded %d allowed categories for AI normalization.", len(allowed_categories))
                    if openai_client is None:
                        logger.warning("AI mode requested but no client available; deterministic fallback will be used.")

                while True:
                    remaining = None if limit is None else limit - total_selected
                    if remaining is not None and remaining <= 0:
                        break

                    requested_batch = batch_size if remaining is None else min(batch_size, remaining)
                    rows = _fetch_candidates(
                        cur,
                        batch_size=requested_batch,
                        offset=offset if dry_run else 0,
                    )
                    if not rows:
                        break

                    batch_no += 1
                    total_selected += len(rows)
                    logger.info(
                        "Batch %d start: %d rows (dry_run=%s)",
                        batch_no,
                        len(rows),
                        dry_run,
                    )

                    for row in rows:
                        raw_offer_id = int(row["id"])
                        raw_name = row.get("name") or ""
                        logger.info("Processing raw_offer id=%s", raw_offer_id)
                        total_processed += 1

                        try:
                            if not dry_run:
                                _mark_processing(cur, raw_offer_id)
                                conn.commit()

                            preprocessed = preprocess_raw_offer(row)
                            ai_used = False
                            ai_succeeded = False
                            normalization_version = "deterministic_v1"
                            chosen_result = build_fallback_ai_result(preprocessed)

                            cached = _lookup_existing_normalization(
                                cur,
                                store_id=row.get("store_id"),
                                content_hash=row.get("content_hash"),
                                raw_offer_id=raw_offer_id,
                            )

                            if cached is not None:
                                normalized_product_id, product_variant_id = cached
                                category_id = None
                                subcategory_id = None
                                category_name = None
                                subcategory_name = None
                                family_id = None
                                family_action = "cached"
                                product_action = "reused_by_hash"
                                variant_action = "reused_by_hash"
                                normalization_version = "cached_by_content_hash"
                                logger.info(
                                    "raw_offer id=%s reused np=%s variant=%s via content_hash (no AI call)",
                                    raw_offer_id, normalized_product_id, product_variant_id,
                                )
                            else:
                                if use_ai and openai_client is not None:
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
                                    dry_run=dry_run,
                                )
                                normalized_product_id, product_action = _find_or_create_normalized_product(
                                    cur,
                                    product_data=chosen_result,
                                    category_id=category_id,
                                    subcategory_id=subcategory_id,
                                    normalization_version=normalization_version,
                                    preprocessed=preprocessed,
                                    family_id=family_id,
                                    dry_run=dry_run,
                                )
                                product_variant_id, variant_action = _find_or_create_product_variant(
                                    cur,
                                    normalized_product_id=normalized_product_id,
                                    variant_data=chosen_result,
                                    preprocessed=preprocessed,
                                    dry_run=dry_run,
                                )
                            snapshot_id, snapshot_action, snapshot_payload = _insert_offer_snapshot(
                                cur,
                                row,
                                preprocessed,
                                product_variant_id,
                                dry_run=dry_run,
                            )

                            if not dry_run and snapshot_payload is not None and snapshot_id is not None:
                                _upsert_current_offer(cur, snapshot_payload)

                            if dry_run:
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

                            if dry_run:
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

                    if dry_run:
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
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Reset rows currently marked as `failed` back to `pending` before processing.",
    )
    parser.add_argument(
        "--processing-age-minutes",
        type=int,
        default=60,
        help="Reset rows stuck in `processing` for longer than this many minutes (default: 60).",
    )
    parser.add_argument("--db-host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PGPORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PGDATABASE", "discount_tracker"))
    parser.add_argument("--db-user", default=os.getenv("PGUSER", "postgres"))
    parser.add_argument("--db-password", default=os.getenv("PGPASSWORD"))
    args = parser.parse_args()

    return run_normalization(
        db_config=DBConfig(
            host=args.db_host,
            port=args.db_port,
            dbname=args.db_name,
            user=args.db_user,
            password=args.db_password,
        ),
        limit=args.limit,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        use_ai=args.use_ai,
        openai_api_key=args.openai_api_key,
        retry_failed=args.retry_failed,
        processing_age_minutes=args.processing_age_minutes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
