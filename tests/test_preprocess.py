"""Tests for the deterministic preprocessor that runs before the AI normalizer.

These functions parse free-form Lithuanian price/date/quantity text into
structured fields. They have no network or DB side effects.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from normalization.preprocess import (
    clean_product_name,
    detect_quantity_from_name,
    parse_unit_price,
    parse_valid_dates,
    preprocess_raw_offer,
)


class TestCleanProductName:
    def test_collapses_whitespace_and_trims(self) -> None:
        assert clean_product_name("  Pienas   1  L  ") == "Pienas 1 L"

    def test_normalizes_dash_spacing(self) -> None:
        assert clean_product_name("Pienas-1L") == "Pienas - 1L"

    def test_handles_nbsp(self) -> None:
        assert clean_product_name("Pienas\xa01\xa0L") == "Pienas 1 L"

    def test_empty_input(self) -> None:
        assert clean_product_name("") == ""


class TestParseUnitPrice:
    @pytest.mark.parametrize(
        "raw, value, unit",
        [
            ("1 kg = 4,99 €", Decimal("4.99"), "kg"),
            ("100 g = 0,89 €", Decimal("0.89"), "g"),
            ("10 vnt. / 1 vnt. = 0,22 €", Decimal("0.22"), "vnt"),
            ("1 l = 1,59 €", Decimal("1.59"), "l"),
            ("1 kg - 8.56 €", Decimal("8.56"), "kg"),
        ],
    )
    def test_parses_canonical_forms(self, raw: str, value: Decimal, unit: str) -> None:
        assert parse_unit_price(raw) == (value, unit)

    @pytest.mark.parametrize("raw", [None, "", "no price here", "123"])
    def test_returns_pair_of_none_when_unparseable(self, raw: str | None) -> None:
        assert parse_unit_price(raw) == (None, None)


class TestParseValidDates:
    REF = "2026-05-01T00:00:00Z"

    @pytest.mark.parametrize(
        "raw, expected_from, expected_to",
        [
            ("04 20 - 04 26", date(2026, 4, 20), date(2026, 4, 26)),
            ("2026-04-20 - 2026-04-26", date(2026, 4, 20), date(2026, 4, 26)),
            ("04.20 - 04.26", date(2026, 4, 20), date(2026, 4, 26)),
            ("04/20 - 04/26", date(2026, 4, 20), date(2026, 4, 26)),
        ],
    )
    def test_parses_in_year_range(self, raw: str, expected_from: date, expected_to: date) -> None:
        assert parse_valid_dates(raw, self.REF) == (expected_from, expected_to)

    def test_handles_year_crossing(self) -> None:
        result = parse_valid_dates("12 29 - 01 05", self.REF)
        assert result[0].year == 2026 and result[0].month == 12
        assert result[1].year == 2027 and result[1].month == 1

    @pytest.mark.parametrize("raw", [None, "", "no dates here"])
    def test_returns_pair_of_none_when_unparseable(self, raw: str | None) -> None:
        assert parse_valid_dates(raw, self.REF) == (None, None)

    def test_returns_none_when_no_ref_date_for_relative_dates(self) -> None:
        assert parse_valid_dates("04 20 - 04 26", None) == (None, None)

    @pytest.mark.parametrize(
        "raw, expected_to",
        [
            ("Iki 05.11", date(2026, 5, 11)),
            ("iki 05.11", date(2026, 5, 11)),
            ("Iki 5.11", date(2026, 5, 11)),
            ("Iki 05-11", date(2026, 5, 11)),
            ("Iki 2026-05-11", date(2026, 5, 11)),
        ],
    )
    def test_parses_iki_single_end_date(self, raw: str, expected_to: date) -> None:
        assert parse_valid_dates(raw, self.REF) == (None, expected_to)

    def test_iki_rolls_year_when_inferred_date_is_in_the_past(self) -> None:
        # Scraped late December, leaflet says "Iki 01.05" → next January.
        assert parse_valid_dates("Iki 01.05", "2026-12-20T00:00:00Z") == (
            None,
            date(2027, 1, 5),
        )

    def test_parses_nuo_single_start_date(self) -> None:
        assert parse_valid_dates("Nuo 05.11", self.REF) == (date(2026, 5, 11), None)

    def test_bare_single_date_defaults_to_valid_to(self) -> None:
        # Maxima's transform used to strip the "Iki" prefix; if any store does
        # the same, fall back to the most-likely interpretation (end date).
        assert parse_valid_dates("05.11", self.REF) == (None, date(2026, 5, 11))


class TestDetectQuantityFromName:
    def test_extracts_size_with_liter(self) -> None:
        result = detect_quantity_from_name("Pienas 1 L")
        assert result["size_value"] == Decimal("1")
        assert result["size_unit"] == "l"
        assert result["package_count"] is None
        assert result["package_unit"] is None

    def test_extracts_size_with_grams(self) -> None:
        result = detect_quantity_from_name("Sausainiai 250 g")
        assert result["size_value"] == Decimal("250")
        assert result["size_unit"] == "g"

    def test_extracts_package_count(self) -> None:
        result = detect_quantity_from_name("Kiausiniai 10 vnt")
        assert result["package_count"] == Decimal("10")
        assert result["package_unit"] == "vnt"
        assert result["size_value"] is None

    def test_handles_decimal_size(self) -> None:
        result = detect_quantity_from_name("Vanduo 1.5 l")
        assert result["size_value"] == Decimal("1.5")
        assert result["size_unit"] == "l"

    def test_returns_all_none_for_empty_name(self) -> None:
        result = detect_quantity_from_name("")
        assert result == {
            "size_value": None,
            "size_unit": None,
            "package_count": None,
            "package_unit": None,
        }


class TestPreprocessRawOffer:
    """End-to-end test of the full preprocessor pipeline on a realistic row."""

    def test_assembles_full_payload(self) -> None:
        row = {
            "id": 42,
            "store_id": 1,
            "store_name": "maxima",
            "name": "Šviežias pienas Rokiškio 1 L",
            "price": "1.49",
            "old_price": "2.09",
            "discount": "29",
            "condition": None,
            "unit_price": "1 l = 1,49 €",
            "image": None,
            "url": None,
            "valid_dates": "04 20 - 04 26",
            "loyalty_card": True,
            "scraped_at": "2026-04-21T10:00:00Z",
        }
        out = preprocess_raw_offer(row)

        assert out["raw_offer_id"] == 42
        assert out["clean_name"] == "Šviežias pienas Rokiškio 1 L"
        assert out["price"] == Decimal("1.49")
        assert out["old_price"] == Decimal("2.09")
        assert out["unit_price_value"] == Decimal("1.49")
        assert out["unit_price_unit"] == "l"
        assert out["valid_from"] == date(2026, 4, 20)
        assert out["valid_to"] == date(2026, 4, 26)
        assert out["loyalty_card"] is True
        assert out["detected_size_value"] == Decimal("1")
        assert out["detected_size_unit"] == "l"
