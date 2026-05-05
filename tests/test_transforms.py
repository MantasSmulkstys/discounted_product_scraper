"""Tests every transform function registered in transforms.TRANSFORMS.

These transforms are the substrate of every store config — a regression here
silently corrupts every scraped row, so coverage is dense on purpose.
"""

from __future__ import annotations

import pytest

from transforms import TRANSFORMS, normalize_lt_price_suffix


def t(name: str, value: str) -> str:
    return TRANSFORMS[name](value)


class TestTrim:
    def test_strips_outer_whitespace(self) -> None:
        assert t("trim", "  hello  ") == "hello"

    def test_no_change_for_clean_input(self) -> None:
        assert t("trim", "hello") == "hello"

    def test_empty_string(self) -> None:
        assert t("trim", "") == ""


class TestCollapseWhitespace:
    def test_collapses_multiple_spaces(self) -> None:
        assert t("collapseWhitespace", "a   b\tc\nd") == "a b c d"

    def test_strips_outer_whitespace(self) -> None:
        assert t("collapseWhitespace", "   a  b   ") == "a b"


class TestStripCurrency:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("12,99 €", "12,99"),
            ("12.99$", "12.99"),
            ("12.99 EUR", "12.99"),
            ("12.99 eur", "12.99"),
            ("12.99", "12.99"),
        ],
    )
    def test_strips_currency_markers(self, raw: str, expected: str) -> None:
        assert t("stripCurrency", raw) == expected


class TestCommaToDecimal:
    def test_replaces_comma(self) -> None:
        assert t("commaToDecimal", "12,99") == "12.99"

    def test_replaces_all_commas(self) -> None:
        assert t("commaToDecimal", "1,234,567") == "1.234.567"


class TestStripNonNumeric:
    def test_keeps_only_digits(self) -> None:
        assert t("stripNonNumeric", "abc-12.34xyz") == "1234"


class TestFirstLine:
    def test_returns_first_line_on_newline(self) -> None:
        assert t("firstLine", "first\nsecond") == "first"

    def test_returns_first_line_on_br_tag(self) -> None:
        assert t("firstLine", "first<br>second") == "first"

    def test_strips_first_line(self) -> None:
        assert t("firstLine", "  first  \nsecond") == "first"


class TestExtractDate:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Galioja 04.20", "04.20"),
            ("04 20 - 04 26", "04 20 - 04 26"),
            ("04/20-04/26", "04/20-04/26"),
            ("nothing here", "nothing here"),
        ],
    )
    def test_extracts_date_or_returns_input(self, raw: str, expected: str) -> None:
        assert t("extractDate", raw) == expected


class TestStripPrefix:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Galioja: 04.20", "04.20"),
            ("Galioja 04.20", "04.20"),
            ("Iki 04.27", "04.27"),
            ("Nuo 04.20", "04.20"),
            ("nothing", "nothing"),
        ],
    )
    def test_strips_known_prefixes(self, raw: str, expected: str) -> None:
        assert t("stripPrefix", raw) == expected


class TestNormalizeLtPriceSuffix:
    """Recovers prices fragmented across DOM elements by markup glue.

    `"Foo 0 79"` -> `"Foo 0,79 €"` (split integer + cents)
    `"Bar -40 %"` -> `"Bar -40%"` (percent with stray space)
    """

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Label 0 79", "Label 0,79 €"),
            ("Label 12 5", "Label 12,05 €"),
            ("Label -3 99", "Label -3,99 €"),
            ("Label -40 %", "Label -40%"),
            ("untouched", "untouched"),
            ("12,99 €", "12,99 €"),
        ],
    )
    def test_normalizes_known_patterns(self, raw: str, expected: str) -> None:
        assert normalize_lt_price_suffix(raw) == expected
