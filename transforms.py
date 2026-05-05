from __future__ import annotations

import re
from typing import Callable

def trim(value: str) -> str:
    return value.strip()

def collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()

def strip_currency(value: str) -> str:
    return re.sub(r"[€$]|EUR", "", value, flags=re.IGNORECASE).strip()

def comma_to_decimal(value: str) -> str:
    return value.replace(",", ".")

def strip_non_numeric(value: str) -> str:
    return re.sub(r"[^\d]", "", value)

def first_line(value: str) -> str:
    return re.split(r"[\n\r]|<br\s*/?>", value)[0].strip()

def extract_date(value: str) -> str:
    match = re.search(r"(\d{2}[.\-/\s]+\d{2}(?:\s*[-–]\s*\d{2}[.\-/\s]+\d{2})?)", value)
    return match.group(1).strip() if match else value.strip()

def strip_prefix(value: str) -> str:
    return re.sub(r"^(Galioja:?\s*|Iki\s+|Nuo\s+)", "", value, flags=re.IGNORECASE).strip()

def lowercase(value: str) -> str:
    return value.lower()

def uppercase(value: str) -> str:
    return value.upper()

def normalize_lt_price_suffix(value: str) -> str:
    """Tidy Lithuanian price-label strings whose digits got split across
    sibling elements by the markup.

    "Label 0 79"   -> "Label 0,79 €"  (integer + cents pair)
    "Label -40 %"  -> "Label -40%"    (percent value with spurious space)

    Leaves other strings untouched.
    """
    cents_match = re.search(r"^(.+?)\s+(-?\d{1,3})\s+(\d{1,2})\s*$", value)
    if cents_match:
        prefix = cents_match.group(1).strip()
        euros = cents_match.group(2)
        cents = cents_match.group(3).rjust(2, "0")
        return f"{prefix} {euros},{cents} \u20ac"
    percent_match = re.search(r"^(.+?)\s+(-?\d+)\s+%\s*$", value)
    if percent_match:
        prefix = percent_match.group(1).strip()
        number = percent_match.group(2)
        return f"{prefix} {number}%"
    return value

TRANSFORMS: dict[str, Callable[[str], str]] = {
    "trim": trim,
    "collapseWhitespace": collapse_whitespace,
    "stripCurrency": strip_currency,
    "commaToDecimal": comma_to_decimal,
    "stripNonNumeric": strip_non_numeric,
    "firstLine": first_line,
    "extractDate": extract_date,
    "stripPrefix": strip_prefix,
    "lowercase": lowercase,
    "uppercase": uppercase,
    "normalizeLtPriceSuffix": normalize_lt_price_suffix,
}