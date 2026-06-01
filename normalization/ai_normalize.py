from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


_UNIT_ALIASES = {
    "kg": "kg",
    "g": "g",
    "l": "l",
    "ml": "ml",
    "vnt": "vnt",
    "vnt.": "vnt",
}


def normalize_identity_text(text: str) -> str:
    if not text:
        return ""

    value = text.strip().lower()
    value = value.translate(
        str.maketrans(
            {
                "ą": "a",
                "č": "c",
                "ę": "e",
                "ė": "e",
                "į": "i",
                "š": "s",
                "ų": "u",
                "ū": "u",
                "ž": "z",
            }
        )
    )
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"_+", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _normalize_unit(value: Any) -> str | None:
    if value is None:
        return None
    unit = str(value).strip().lower()
    if not unit:
        return None
    return _UNIT_ALIASES.get(unit)


def _null_if_empty(value: Any) -> Any:
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def load_allowed_categories(cur) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            c.id AS category_id,
            c.name_lt AS category_name,
            s.id AS subcategory_id,
            s.name_lt AS subcategory_name
        FROM categories c
        LEFT JOIN subcategories s ON s.category_id = c.id
        ORDER BY c.name_lt ASC, s.name_lt ASC NULLS LAST
        """
    )
    rows = cur.fetchall()
    if not rows:
        return []

    categories: dict[int, dict[str, Any]] = {}
    for category_id, category_name, subcategory_id, subcategory_name in rows:
        cid = int(category_id)
        if cid not in categories:
            categories[cid] = {
                "category_id": cid,
                "category_name": category_name,
                "subcategories": [],
            }
        if subcategory_id is not None:
            categories[cid]["subcategories"].append(
                {
                    "subcategory_id": int(subcategory_id),
                    "subcategory_name": subcategory_name,
                }
            )
    return list(categories.values())


def load_allowed_categories_from_file(path: str | Path) -> list[dict[str, Any]]:
    """Load the allowed-category list from a static JSON file.

    Used by the JSON output mode, which must run without a database. The file
    is expected to match the shape `load_allowed_categories` returns from
    Postgres: a list of {category_id, category_name, subcategories: [...]}.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path} must contain a JSON array of categories")
    return raw


def build_ai_input(preprocessed: dict[str, Any], allowed_categories: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "store_name": preprocessed.get("store_name"),
        "raw_name": preprocessed.get("raw_name"),
        "clean_name": preprocessed.get("clean_name"),
        "price": _to_float(preprocessed.get("price")),
        "old_price": _to_float(preprocessed.get("old_price")),
        "discount": _to_float(preprocessed.get("discount")),
        "condition": preprocessed.get("condition"),
        "loyalty_card": bool(preprocessed.get("loyalty_card")),
        "unit_price_raw": preprocessed.get("unit_price_raw"),
        "unit_price_value": _to_float(preprocessed.get("unit_price_value")),
        "unit_price_unit": preprocessed.get("unit_price_unit"),
        "valid_from": str(preprocessed.get("valid_from")) if preprocessed.get("valid_from") else None,
        "valid_to": str(preprocessed.get("valid_to")) if preprocessed.get("valid_to") else None,
        "detected_size_value": _to_float(preprocessed.get("detected_size_value")),
        "detected_size_unit": preprocessed.get("detected_size_unit"),
        "detected_package_count": _to_float(preprocessed.get("detected_package_count")),
        "detected_package_unit": preprocessed.get("detected_package_unit"),
        "allowed_categories": allowed_categories,
    }


def normalize_with_ai(client, ai_input: dict[str, Any]) -> dict[str, Any]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "canonical_name": {"type": "string"},
            "normalized_name": {"type": "string"},
            "brand": {"type": ["string", "null"]},
            "generic_name": {"type": ["string", "null"]},
            "family_key": {"type": ["string", "null"]},
            "category_name": {"type": ["string", "null"]},
            "subcategory_name": {"type": ["string", "null"]},
            "variant_name": {"type": ["string", "null"]},
            "size_value": {"type": ["number", "null"]},
            "size_unit": {"type": ["string", "null"]},
            "package_count": {"type": ["number", "null"]},
            "package_unit": {"type": ["string", "null"]},
            "net_quantity_value": {"type": ["number", "null"]},
            "net_quantity_unit": {"type": ["string", "null"]},
            "attributes": {
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
            "confidence": {"type": ["number", "null"]},
            "needs_review": {"type": "boolean"},
        },
        "required": [
            "canonical_name",
            "normalized_name",
            "brand",
            "generic_name",
            "family_key",
            "category_name",
            "subcategory_name",
            "variant_name",
            "size_value",
            "size_unit",
            "package_count",
            "package_unit",
            "net_quantity_value",
            "net_quantity_unit",
            "attributes",
            "confidence",
            "needs_review",
        ],
    }

    instruction = (
        "# ROLE\n"
        "You normalize Lithuanian grocery offer data into strict JSON for cross-store matching.\n"
        "\n"
        "# CORE PRINCIPLES\n"
        "1. Choose category/subcategory ONLY from `allowed_categories`. Never invent categories.\n"
        "2. Preserve Lithuanian meaning — do not translate or anglicize.\n"
        "3. When uncertain: use null, set `needs_review=true`, and lower `confidence`.\n"
        "4. Be deterministic: identical inputs must produce identical outputs.\n"
        "\n"
        "# FIELD RULES\n"
        "\n"
        "## canonical_name — product identity only\n"
        "CRITICAL: Must NOT contain package size, count, or volume (e.g. `1 kg`, `500 g`, `280 g`, `900 ml`, `10 vnt.`).\n"
        "Style (in this order): generic product name → important attributes → brand.\n"
        "- Brand goes at the END, not the beginning.\n"
        "- KEEP identity-defining attributes: fat %, preparation style, flavor, variety, color, lactose status, fresh/frozen.\n"
        "- REMOVE retail noise when not essential: `I r.`, `II r.`, `a. r.`, etc. Never remove meaningful food words.\n"
        "Example: `Pienas 2,5% Rokiškio` (NOT `Rokiškio pienas 2,5% 1 l`).\n"
        "\n"
        "## generic_name\n"
        "The plain generic product type, no attributes or brand (e.g. `pienas`, `sviestas`, `vynuogės`).\n"
        "\n"
        "## variant_name\n"
        "Compact quantity string, or null. Examples: `1 kg`, `10 vnt.`, `900 ml`. Never duplicates canonical_name content.\n"
        "\n"
        "## Quantity fields\n"
        "Put numeric quantities into the structured fields:\n"
        "- `size_value` / `size_unit`\n"
        "- `package_count` / `package_unit`\n"
        "- `net_quantity_value` / `net_quantity_unit`\n"
        "\n"
        "## family_key — THE CROSS-STORE MATCH KEY\n"
        "This groups the SAME ITEM sold under slightly different names in different stores. Get this right.\n"
        "\n"
        "Format requirements (ALL must hold):\n"
        "- 1–3 words, lowercase\n"
        "- Lithuanian, NO diacritics (ė→e, š→s, ą→a, ų→u, etc.)\n"
        "- Always nominative plural form (no declension variants)\n"
        "- NO numbers, percentages, weights, or volumes\n"
        "- NO brand names\n"
        "- NO packaging adjectives (e.g. `pakuotė`, `dėžutė`)\n"
        "\n"
        "What MUST stay in family_key (identity-defining splits):\n"
        "- Color when it signals variety (wine, grapes): `raudonas` vs `baltas`\n"
        "- Lactose status: `be laktozes` splits from regular\n"
        "- Fresh vs frozen: `saldytos` splits from fresh\n"
        "- Preparation style when it changes the product category\n"
        "\n"
        "What MUST be stripped (cosmetic/measurement differences):\n"
        "- Fat percentage (2,5%, 82%, 90% — all collapse together)\n"
        "- Weight, volume, count\n"
        "- Brand\n"
        "- Freshness adjectives like `šviežios` when fresh is the default state\n"
        "- Declension endings (always normalize to nominative plural)\n"
        "\n"
        "Worked examples (clean_name → family_key):\n"
        "  `Raudonosios besėklės vynuogės`      → `raudonos vynuoges`\n"
        "  `Raudonos besėklės vynuogės`         → `raudonos vynuoges`\n"
        "  `Šviežios raudonos vynuogės`         → `raudonos vynuoges`\n"
        "  `Baltos vynuogės`                    → `baltos vynuoges`\n"
        "  `Pienas Rokiškio 2,5% 1 l`           → `pienas`\n"
        "  `Pienas be laktozės 2,5%`            → `pienas be laktozes`\n"
        "  `Sviestas 82% riebumo`               → `sviestas`\n"
        "  `Sviestas 90% riebumo Dvaro`         → `sviestas`\n"
        "  `Raudonas vynas Cabernet`            → `raudonas vynas`\n"
        "  `Baltas vynas Chardonnay`            → `baltas vynas`\n"
        "  `Avietės šviežios 250 g`             → `avietes`\n"
        "  `Šaldytos avietės 400 g`             → `saldytos avietes`\n"
        "\n"
        "Key takeaways from the examples:\n"
        "- Lactose status splits; fat % does NOT.\n"
        "- Color splits for wine and grapes (signals variety).\n"
        "- Grape variety (Cabernet, Chardonnay) does NOT split — it's brand-like detail.\n"
        "- Frozen vs fresh splits; `šviežios` alone is dropped when fresh is the default.\n"
        "- Brand and packaging NEVER appear in family_key.\n"
        "\n"
        "# DECISION CHECKLIST (run for each item)\n"
        "1. Identify generic_name (the bare product type).\n"
        "2. Build canonical_name = generic + identity attributes + brand (no sizes).\n"
        "3. Extract quantities into variant_name + structured size fields.\n"
        "4. Build family_key: strip diacritics, brand, numbers, packaging; keep variety/lactose/frozen splits; nominative plural.\n"
        "5. Pick category/subcategory from allowed_categories only.\n"
        "6. If any step was uncertain → needs_review=true, lower confidence.\n"
    )

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(ai_input, ensure_ascii=False)},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "normalization_result",
                "schema": schema,
                "strict": True,
            }
        },
    )
    parsed = getattr(response, "output_parsed", None)
    if isinstance(parsed, dict):
        return parsed

    text_output = getattr(response, "output_text", "") or ""
    if not text_output:
        raise RuntimeError("AI returned empty response text")
    try:
        return json.loads(text_output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"AI returned invalid JSON: {exc}") from exc


def validate_ai_output(data: dict[str, Any]) -> dict[str, Any]:
    required = {
        "canonical_name",
        "normalized_name",
        "brand",
        "generic_name",
        "category_name",
        "subcategory_name",
        "variant_name",
        "size_value",
        "size_unit",
        "package_count",
        "package_unit",
        "net_quantity_value",
        "net_quantity_unit",
        "attributes",
        "confidence",
        "needs_review",
    }
    missing = sorted(required - set(data.keys()))
    if missing:
        raise ValueError(f"Missing keys in AI output: {missing}")

    cleaned: dict[str, Any] = dict(data)
    if "family_key" not in cleaned:
        cleaned["family_key"] = None
    for key in [
        "canonical_name",
        "normalized_name",
        "brand",
        "generic_name",
        "family_key",
        "category_name",
        "subcategory_name",
        "variant_name",
    ]:
        cleaned[key] = _null_if_empty(cleaned.get(key))

    family_key = cleaned.get("family_key")
    if isinstance(family_key, str):
        cleaned["family_key"] = normalize_identity_text(family_key)
        if not cleaned["family_key"]:
            cleaned["family_key"] = None

    if not cleaned.get("canonical_name"):
        raise ValueError("AI output canonical_name is empty")
    canonical_name = str(cleaned["canonical_name"]).strip()
    canonical_name = re.sub(r"\s+", " ", canonical_name)
    if canonical_name and canonical_name[0].isalpha():
        canonical_name = canonical_name[0].upper() + canonical_name[1:]
    cleaned["canonical_name"] = canonical_name
    cleaned["normalized_name"] = normalize_identity_text(cleaned["canonical_name"])

    for key in ["size_value", "package_count", "net_quantity_value", "confidence"]:
        cleaned[key] = _to_decimal(cleaned.get(key))
    confidence = cleaned.get("confidence")
    if confidence is not None:
        if confidence < Decimal("0") or confidence > Decimal("1"):
            cleaned["confidence"] = None

    for key in ["size_unit", "package_unit", "net_quantity_unit"]:
        cleaned[key] = _normalize_unit(cleaned.get(key))

    attributes = cleaned.get("attributes")
    if not isinstance(attributes, dict):
        raise ValueError("AI output attributes must be an object")
    cleaned["attributes"] = attributes

    needs_review = cleaned.get("needs_review")
    if not isinstance(needs_review, bool):
        raise ValueError("AI output needs_review must be boolean")

    return cleaned


def resolve_category_ids(
    ai_output: dict[str, Any],
    allowed_categories: list[dict[str, Any]],
) -> tuple[int | None, int | None]:
    category_name = ai_output.get("category_name")
    subcategory_name = ai_output.get("subcategory_name")
    if not category_name:
        return (None, None)

    for cat in allowed_categories:
        if cat.get("category_name") != category_name:
            continue
        category_id = cat.get("category_id")
        if not subcategory_name:
            return (category_id, None)
        for sub in cat.get("subcategories", []):
            if sub.get("subcategory_name") == subcategory_name:
                return (category_id, sub.get("subcategory_id"))
        return (category_id, None)
    return (None, None)


def _build_variant_name_from_preprocessed(preprocessed: dict[str, Any]) -> str | None:
    parts: list[str] = []
    if preprocessed.get("detected_size_value") is not None and preprocessed.get("detected_size_unit"):
        parts.append(f"{preprocessed['detected_size_value']} {preprocessed['detected_size_unit']}")
    if preprocessed.get("detected_package_count") is not None and preprocessed.get("detected_package_unit"):
        parts.append(f"{preprocessed['detected_package_count']} {preprocessed['detected_package_unit']}")
    if not parts:
        return None
    return " / ".join(parts)


def build_fallback_ai_result(preprocessed: dict[str, Any]) -> dict[str, Any]:
    clean_name = preprocessed.get("clean_name") or ""
    return {
        "canonical_name": clean_name,
        "normalized_name": normalize_identity_text(clean_name),
        "brand": None,
        "generic_name": None,
        "family_key": None,
        "category_name": None,
        "subcategory_name": None,
        "variant_name": _build_variant_name_from_preprocessed(preprocessed),
        "size_value": _to_decimal(preprocessed.get("detected_size_value")),
        "size_unit": _normalize_unit(preprocessed.get("detected_size_unit")),
        "package_count": _to_decimal(preprocessed.get("detected_package_count")),
        "package_unit": _normalize_unit(preprocessed.get("detected_package_unit")),
        "net_quantity_value": _to_decimal(preprocessed.get("detected_size_value")),
        "net_quantity_unit": _normalize_unit(preprocessed.get("detected_size_unit")),
        "attributes": {},
        "confidence": None,
        "needs_review": True,
    }
