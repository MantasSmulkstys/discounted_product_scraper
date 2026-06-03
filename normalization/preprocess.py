from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


_WS_RE = re.compile(r"\s+")
_DASH_RE = re.compile(r"\s*([\-–—])\s*")
_YEAR_RANGE_RE = re.compile(
    r"(?P<y1>\d{4})[-./](?P<m1>\d{1,2})[-./](?P<d1>\d{1,2})\s*[-–—]\s*"
    r"(?P<y2>\d{4})[-./](?P<m2>\d{1,2})[-./](?P<d2>\d{1,2})"
)
_MONTH_DAY_RANGE_RE = re.compile(
    r"(?P<m1>\d{1,2})[./\s](?P<d1>\d{1,2})\s*[-–—]\s*"
    r"(?P<m2>\d{1,2})[./\s](?P<d2>\d{1,2})"
)
_SINGLE_DATE_RE = re.compile(
    r"^(?:(?P<y>\d{4})[-./\s])?(?P<m>\d{1,2})[-./\s](?P<d>\d{1,2})\.?$"
)
_PREFIX_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^Iki\s+", re.IGNORECASE), "to"),
    (re.compile(r"^Nuo\s+", re.IGNORECASE), "from"),
    (re.compile(r"^Galioja:?\s+", re.IGNORECASE), "unknown"),
]
_UNIT_TOKEN_RE = re.compile(r"\b(kg|g|ml|l|vnt\.?)\b", re.IGNORECASE)
_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l)\b", re.IGNORECASE)
_PACKAGE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(vnt\.?)\b", re.IGNORECASE)
_PRICE_EUR_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*€", re.IGNORECASE)

# Tokens that identify a *specific* SKU within an otherwise identically-named
# product. These are deliberately discarded from the display name but must be
# kept as a variant discriminator, otherwise two distinct articles (e.g. two
# kettle models, or bouquets of 30 vs 9 stems) collapse into one product and
# share a single — meaningless — price history.
# The literal "art." dot is required so Lithuanian words like "artišokai" or
# "artistas" are never mistaken for an article code. The code runs until a
# comma/semicolon, another "art.", or the Lithuanian "arba" ("or") that joins
# alternative codes — so multi-token codes like "HTD 889 CB" are kept whole
# instead of being truncated to "HTD".
_ART_CODE_RE = re.compile(
    r"\bart\.\s*([A-Za-z0-9][^,;]*?)\s*(?=,|;|\barba\b|\bart\.|$)",
    re.IGNORECASE,
)
# A multipack count ("× 30 vnt", "– 9 vnt"). A lone "1 vnt" is just "a single
# item" and carries no discriminating signal, so it is ignored below.
_MULTIPACK_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*vnt\.?", re.IGNORECASE)
# Linear dimensions the size parser ignores (it only handles kg/g/ml/l), e.g.
# the "50 cm" stem length of a flower bouquet.
_DIMENSION_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(cm|mm)\b", re.IGNORECASE)


def _to_decimal(value: str | int | float | Decimal | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value

    text = str(value).strip()
    if not text:
        return None

    text = text.replace(",", ".")
    text = re.sub(r"[^\d.\-]", "", text)
    if not text:
        return None

    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _to_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 0:
            return False
        if value == 1:
            return True
        return bool(value)

    text = str(value).strip()
    if text in {"", "0", "false", "False", "no", "n"}:
        return False
    if text in {"1", "true", "True", "yes", "y"}:
        return True
    return bool(value)


def _normalize_unit(raw_unit: str | None) -> str | None:
    if not raw_unit:
        return None
    unit = raw_unit.lower().rstrip(".")
    if unit in {"kg", "g", "l", "ml", "vnt"}:
        return unit
    return None


def _coerce_date_ref(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()

    text = str(value).strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _build_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def clean_product_name(name: str) -> str:
    """
    Normalize raw product name text conservatively.

    The function keeps Lithuanian letters, percentages, and quantity markers,
    while applying minimal cleanup: trim, collapse whitespace, and normalize
    spacing around dashes.
    """
    if not name:
        return ""

    normalized = name.replace("\xa0", " ").strip()
    normalized = _DASH_RE.sub(r" \1 ", normalized)
    normalized = _WS_RE.sub(" ", normalized)
    return normalized.strip()


# Descriptor tokens stripped from the display name. Quantity detection runs on
# the conservatively-cleaned name BEFORE this, so removing sizes here is safe.
_DESCRIPTOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    # size / weight / volume / count, e.g. "1 kg", "350 g", "900 ml", "10 vnt."
    # The trailing lookahead means the unit must be its own token, so a word
    # like "gabaliukai" or "litras" is never clipped.
    re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:kg|g|ml|cl|l|vnt)\.?(?=\s|,|;|$)", re.IGNORECASE),
    # article/SKU codes up to the next separator, e.g. "art. 20240419",
    # "art. HTD 889 CB". The dot is required so words like "artišokai" are not
    # clipped. Kept as a variant discriminator via detect_variant_code() before
    # this strips it from the display name.
    re.compile(r"\bart\.\s*[A-Za-z0-9][^,;]*", re.IGNORECASE),
    # retail grade, e.g. "a. r.", "I r.", "II r."
    re.compile(r"\b(?:[ivx]+|a)\.?\s*r\.", re.IGNORECASE),
    # quality class, e.g. "1 kl.", "2 kl."
    re.compile(r"\b\d+\s*kl\.", re.IGNORECASE),
)


def strip_descriptors(name: str) -> str:
    """Deterministic display name: drop quantities and retail noise, keep the
    actual product words (incl. Lithuanian diacritics, fat %, flavor, brand).

    Examples:
        "KREKENAVOS virtos dešrelės, a. r., 1 kg" -> "KREKENAVOS virtos dešrelės"
        "Pomidorai ROMANTICA RIMI 1 kl., 400 g"   -> "Pomidorai ROMANTICA RIMI"
        "Šaltkrepšis su kietu įdėklu, art. 20240419, 1 vnt." -> "Šaltkrepšis su kietu įdėklu"
    """
    if not name:
        return ""

    text = name
    for pattern in _DESCRIPTOR_PATTERNS:
        text = pattern.sub(" ", text)

    # Tidy separators left behind by the removals.
    text = re.sub(r"\s+([,;])", r"\1", text)        # " ," -> ","
    text = re.sub(r"([,;])\s*(?=[,;])", "", text)   # collapse runs of commas
    text = _WS_RE.sub(" ", text)
    return text.strip(" ,;-–—")


def parse_valid_dates(
    valid_dates: str | None,
    scraped_at: Any,
) -> tuple[date | None, date | None]:
    """
    Parse validity date ranges from common offer formats.

    Supported examples:
    - 04 20 - 04 26
    - 2026-04-20 - 2026-04-26
    - 04.20 - 04.26
    - 04/20 - 04/26
    - Iki 05.11             (single end date — Maxima style)
    - Nuo 05.11             (single start date)
    - 2026-05-11            (single explicit date — defaults to valid_to)

    If year is missing, it is inferred from scraped_at. Year crossing is handled
    for common New Year transitions (e.g. 12 29 - 01 05). For single dates with
    no year, if the inferred date is before scraped_at it is rolled to the next
    year.
    """
    if not valid_dates:
        return (None, None)

    text = _WS_RE.sub(" ", valid_dates.replace("\xa0", " ").strip())
    if not text:
        return (None, None)

    role = "to"
    for pattern, prefix_role in _PREFIX_PATTERNS:
        m = pattern.match(text)
        if m:
            role = prefix_role if prefix_role != "unknown" else role
            text = text[m.end():].strip()
            break

    match_with_year = _YEAR_RANGE_RE.search(text)
    if match_with_year:
        start = _build_date(
            int(match_with_year.group("y1")),
            int(match_with_year.group("m1")),
            int(match_with_year.group("d1")),
        )
        end = _build_date(
            int(match_with_year.group("y2")),
            int(match_with_year.group("m2")),
            int(match_with_year.group("d2")),
        )
        if start is None or end is None:
            return (None, None)
        if start > end and start.year == end.year:
            return (None, None)
        return (start, end)

    ref_date = _coerce_date_ref(scraped_at)

    if ref_date is not None:
        match_no_year = _MONTH_DAY_RANGE_RE.search(text)
        if match_no_year:
            m1 = int(match_no_year.group("m1"))
            d1 = int(match_no_year.group("d1"))
            m2 = int(match_no_year.group("m2"))
            d2 = int(match_no_year.group("d2"))

            start_year = ref_date.year
            end_year = ref_date.year

            year_crossing_applied = False
            if m1 > m2:
                year_crossing_applied = True
                if ref_date.month <= 2 and m1 >= 11 and m2 <= 2:
                    start_year = ref_date.year - 1
                    end_year = ref_date.year
                elif ref_date.month >= 11 and m1 >= 11 and m2 <= 2:
                    start_year = ref_date.year
                    end_year = ref_date.year + 1
                else:
                    end_year = ref_date.year + 1

            start = _build_date(start_year, m1, d1)
            end = _build_date(end_year, m2, d2)
            if start is None or end is None:
                return (None, None)
            if start > end:
                if not year_crossing_applied or start.year == end.year:
                    return (None, None)
            return (start, end)

    single = _SINGLE_DATE_RE.fullmatch(text)
    if not single:
        return (None, None)

    if single.group("y"):
        parsed = _build_date(
            int(single.group("y")),
            int(single.group("m")),
            int(single.group("d")),
        )
    else:
        if ref_date is None:
            return (None, None)
        m_val = int(single.group("m"))
        d_val = int(single.group("d"))
        parsed = _build_date(ref_date.year, m_val, d_val)
        if parsed is not None and parsed < ref_date:
            parsed = _build_date(ref_date.year + 1, m_val, d_val)

    if parsed is None:
        return (None, None)

    if role == "from":
        return (parsed, None)
    return (None, parsed)


def parse_unit_price(unit_price: str | None) -> tuple[Decimal | None, str | None]:
    """
    Parse unit price string into numeric value and normalized unit.

    Supported patterns include:
    - 1 kg - 3.69 €
    - 10 vnt. / 1 vnt. = 0,22 €
    - 1 l = 1,59 €
    - 1 kg = 4,99 €
    - 100 g = 0,89 €
    """
    if not unit_price:
        return (None, None)

    text = _WS_RE.sub(" ", unit_price.replace("\xa0", " ").strip())
    if not text:
        return (None, None)

    price_matches = list(_PRICE_EUR_RE.finditer(text))
    if not price_matches:
        return (None, None)
    price_value = _to_decimal(price_matches[-1].group(1))
    if price_value is None:
        return (None, None)

    unit_candidate: str | None = None
    left_side = text[: price_matches[-1].start()]
    quantity_matches = list(
        re.finditer(r"(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l|vnt\.?)\b", left_side, re.IGNORECASE)
    )
    if quantity_matches:
        unit_candidate = quantity_matches[-1].group(2)

    if unit_candidate is None:
        lhs = text.split("=", 1)[0]
        if lhs == text:
            lhs = text.split("-", 1)[0]
        unit_tokens = _UNIT_TOKEN_RE.findall(lhs)
        if unit_tokens:
            unit_candidate = unit_tokens[-1]

    unit = _normalize_unit(unit_candidate)
    if unit is None:
        return (None, None)
    return (price_value, unit)


def detect_quantity_from_name(name: str) -> dict[str, Decimal | str | None]:
    """
    Detect simple quantity hints from product name.

    Returns a dict with exactly these keys:
    - size_value
    - size_unit
    - package_count
    - package_unit
    """
    result: dict[str, Decimal | str | None] = {
        "size_value": None,
        "size_unit": None,
        "package_count": None,
        "package_unit": None,
    }
    if not name:
        return result

    text = clean_product_name(name)

    size_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l)\b", text, re.IGNORECASE)
    if size_match:
        size_value = _to_decimal(size_match.group(1))
        size_unit = _normalize_unit(size_match.group(2))
        if size_value is not None and size_unit in {"kg", "g", "l", "ml"}:
            result["size_value"] = size_value
            result["size_unit"] = size_unit

    package_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(vnt\.?)\b", text, re.IGNORECASE)
    if package_match:
        package_count = _to_decimal(package_match.group(1))
        package_unit = _normalize_unit(package_match.group(2))
        if package_count is not None and package_unit == "vnt":
            result["package_count"] = package_count
            result["package_unit"] = package_unit

    return result


def _quantity_token(value: Decimal) -> str:
    """Stable string form of a quantity for use inside a variant code:
    `Decimal("30")` -> "30", `Decimal("0.50")` -> "0.5"."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def detect_variant_code(name: str) -> str | None:
    """A deterministic per-SKU discriminator derived from the raw name.

    The normalizer strips article codes and quantities from the display name,
    and the AI often flattens multipack counts — so two genuinely different
    articles can land on the same `normalized_name` + size, merging into one
    product. This code is folded into the product_variant identity so they stay
    separate. It only captures signals that are stable for a given SKU and do
    *not* vary by store wording (article codes, multipack counts, linear
    dimensions); weights/volumes are left to the size parser to avoid
    over-splitting the same product across stores.

    Returns None when nothing distinguishing is present (the common case for
    plain groceries like "Bananai"), so normal cross-store merging is untouched.

    Examples:
        "Elektrinis virdulys ONEX, art. KT-S08, 1 vnt." -> "art:KT-S08"
        "Kapų žvakė art.0508 arba art.0532, 1 vnt."      -> "art:0508-0532"
        "Gvazdikų puokštė, 50 cm, 1 puokštė – 30 vnt."   -> "d:50cm|x:30vnt"
        "Bananai"                                         -> None
    """
    if not name:
        return None

    parts: list[str] = []

    codes = []
    for match in _ART_CODE_RE.finditer(name):
        code = _WS_RE.sub("-", match.group(1).strip()).upper()
        if code:
            codes.append(code)
    if codes:
        parts.append("art:" + "-".join(codes))

    counts = []
    for match in _MULTIPACK_RE.finditer(name):
        value = _to_decimal(match.group(1))
        if value is not None and value != 1:
            counts.append(_quantity_token(value) + "vnt")
    if counts:
        parts.append("x:" + "-".join(sorted(counts)))

    dimensions = []
    for match in _DIMENSION_RE.finditer(name):
        value = _to_decimal(match.group(1))
        if value is not None:
            dimensions.append(_quantity_token(value) + match.group(2).lower())
    if dimensions:
        parts.append("d:" + "-".join(sorted(dimensions)))

    return "|".join(parts) if parts else None


def preprocess_raw_offer(raw_offer: dict[str, Any]) -> dict[str, Any]:
    """
    Build deterministic preprocessed payload from one raw_offers row.

    This function is side-effect free: no AI calls and no database writes.
    """
    raw_name = raw_offer.get("name") or ""
    # Conservatively-cleaned name still contains sizes, so quantity detection
    # works; `clean_name` is the stripped display name derived from it.
    conservative_name = clean_product_name(str(raw_name))
    clean_name = strip_descriptors(conservative_name)

    valid_dates_raw = raw_offer.get("valid_dates")
    valid_from, valid_to = parse_valid_dates(valid_dates_raw, raw_offer.get("scraped_at"))

    unit_price_raw = raw_offer.get("unit_price")
    unit_price_value, unit_price_unit = parse_unit_price(unit_price_raw)

    quantity = detect_quantity_from_name(conservative_name)
    variant_code = detect_variant_code(conservative_name)

    return {
        "raw_offer_id": raw_offer.get("id"),
        "store_id": raw_offer.get("store_id"),
        "store_name": raw_offer.get("store_name"),
        "raw_name": raw_name,
        "clean_name": clean_name,
        "price": _to_decimal(raw_offer.get("price")),
        "old_price": _to_decimal(raw_offer.get("old_price")),
        "discount": _to_decimal(raw_offer.get("discount")),
        "condition": raw_offer.get("condition"),
        "unit_price_raw": unit_price_raw,
        "unit_price_value": unit_price_value,
        "unit_price_unit": unit_price_unit,
        "valid_dates_raw": valid_dates_raw,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "loyalty_card": _to_bool(raw_offer.get("loyalty_card")),
        "image": raw_offer.get("image"),
        "url": raw_offer.get("url"),
        "scraped_at": raw_offer.get("scraped_at"),
        "detected_size_value": quantity["size_value"],
        "detected_size_unit": quantity["size_unit"],
        "detected_package_count": quantity["package_count"],
        "detected_package_unit": quantity["package_unit"],
        "detected_variant_code": variant_code,
    }
