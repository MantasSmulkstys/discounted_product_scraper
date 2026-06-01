"""Flat JSON normalization pipeline (the `--o json` path).

Unlike the database path, this runs fully offline: scrape -> deterministic
preprocess -> AI normalize each offer -> one flat JSON array. There is no
cross-store dedup, no families/variants/snapshots — every scraped offer maps
to exactly one record that merges its raw price fields with the AI's
normalized identity fields.

Allowed categories are read from a static file (configs/categories.json)
instead of Postgres, so no database connection is needed.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from db import _parse_discount_value
from engine import Product
from normalization.ai_normalize import (
    build_ai_input,
    build_fallback_ai_result,
    normalize_with_ai,
    resolve_category_ids,
    validate_ai_output,
)
from normalization.preprocess import preprocess_raw_offer

logger = logging.getLogger(__name__)


def _product_to_raw_dict(product: Product, index: int) -> dict[str, Any]:
    """Adapt a scraped Product into the raw_offers-row shape preprocess expects.

    `discount` is run through the same `_parse_discount_value` the DB path uses
    so a "-40%" string and an old/new price pair both collapse to the numeric
    percentage, keeping JSON and DB output consistent.
    """
    return {
        "id": index,
        "store_id": None,
        "store_name": product.store,
        "name": product.name,
        "price": product.price,
        "old_price": product.old_price,
        "discount": _parse_discount_value(product),
        "condition": product.condition,
        "unit_price": product.unit_price,
        "image": product.image,
        "url": product.url,
        "valid_dates": product.valid_dates,
        "loyalty_card": product.loyalty_card,
        "scraped_at": product.scraped_at,
    }


def _flatten(
    preprocessed: dict[str, Any],
    ai: dict[str, Any],
    category_id: int | None,
    subcategory_id: int | None,
) -> dict[str, Any]:
    """Merge deterministic preprocess output with AI identity fields into one
    flat record. Preprocess wins for price/date/quantity facts; the AI wins for
    naming/categorization."""
    return {
        "store": preprocessed.get("store_name"),
        # identity (AI)
        "raw_name": preprocessed.get("raw_name"),
        "clean_name": preprocessed.get("clean_name"),
        "canonical_name": ai.get("canonical_name"),
        "normalized_name": ai.get("normalized_name"),
        "brand": ai.get("brand"),
        "generic_name": ai.get("generic_name"),
        "family_key": ai.get("family_key"),
        "category_name": ai.get("category_name"),
        "category_id": category_id,
        "subcategory_name": ai.get("subcategory_name"),
        "subcategory_id": subcategory_id,
        "variant_name": ai.get("variant_name"),
        # pricing (deterministic)
        "price": preprocessed.get("price"),
        "old_price": preprocessed.get("old_price"),
        "discount": preprocessed.get("discount"),
        "condition": preprocessed.get("condition"),
        "loyalty_card": preprocessed.get("loyalty_card"),
        "unit_price_value": preprocessed.get("unit_price_value"),
        "unit_price_unit": preprocessed.get("unit_price_unit"),
        # quantity (AI, structured)
        "size_value": ai.get("size_value"),
        "size_unit": ai.get("size_unit"),
        "package_count": ai.get("package_count"),
        "package_unit": ai.get("package_unit"),
        "net_quantity_value": ai.get("net_quantity_value"),
        "net_quantity_unit": ai.get("net_quantity_unit"),
        # validity + provenance
        "valid_from": preprocessed.get("valid_from"),
        "valid_to": preprocessed.get("valid_to"),
        "image": preprocessed.get("image"),
        "url": preprocessed.get("url"),
        "scraped_at": preprocessed.get("scraped_at"),
        "confidence": ai.get("confidence"),
        "needs_review": ai.get("needs_review"),
    }


def normalize_products_to_records(
    products: list[Product],
    allowed_categories: list[dict[str, Any]],
    *,
    openai_client: Any | None = None,
    throttle_seconds: float = 0.3,
) -> list[dict[str, Any]]:
    """Preprocess + AI-normalize every product into a flat record list.

    When `openai_client` is None (or a call fails) the deterministic fallback
    result is used for that offer, so the pipeline still produces output.
    """
    records: list[dict[str, Any]] = []
    total = len(products)

    for index, product in enumerate(products, start=1):
        raw = _product_to_raw_dict(product, index)
        preprocessed = preprocess_raw_offer(raw)
        chosen = build_fallback_ai_result(preprocessed)

        if openai_client is not None:
            try:
                ai_input = build_ai_input(preprocessed, allowed_categories)
                if throttle_seconds:
                    time.sleep(throttle_seconds)
                chosen = validate_ai_output(normalize_with_ai(openai_client, ai_input))
            except Exception as exc:
                logger.warning(
                    "AI normalization failed for %r (%d/%d): %s. Using fallback.",
                    product.name, index, total, exc,
                )

        category_id, subcategory_id = resolve_category_ids(chosen, allowed_categories)
        records.append(_flatten(preprocessed, chosen, category_id, subcategory_id))

        if index % 50 == 0 or index == total:
            logger.info("Normalized %d/%d offers", index, total)

    return records


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_records_json(records: list[dict[str, Any]], path: str | Path) -> Path:
    out_path = Path(path)
    out_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return out_path
