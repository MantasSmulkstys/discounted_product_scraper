"""Tests for the AI-normalization helpers that don't touch the network.

`normalize_with_ai` itself calls OpenAI and is therefore not unit-tested here;
the *validation* and *fallback* paths around it are covered, since those are
where most regressions land.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from normalization.ai_normalize import (
    build_fallback_ai_result,
    normalize_identity_text,
    resolve_category_ids,
    validate_ai_output,
)


class TestNormalizeIdentityText:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Šviežias Pienas 1L", "sviezias pienas 1l"),
            ("ąčęėįšųūž", "aceeisuuz"),
            ("Pienas, 1l!", "pienas 1l"),
            ("multi   space", "multi space"),
            ("", ""),
            (None, ""),
        ],
    )
    def test_normalizes_lithuanian_text(self, raw: str | None, expected: str) -> None:
        assert normalize_identity_text(raw or "") == expected


class TestValidateAiOutput:
    @staticmethod
    def _valid_payload() -> dict:
        return {
            "canonical_name": "Pienas Rokiškio",
            "normalized_name": "pienas rokiskio",
            "brand": "Rokiškio",
            "generic_name": "pienas",
            "category_name": "ŠVIEŽI MAISTO PRODUKTAI",
            "subcategory_name": "PIENO PRODUKTAI",
            "variant_name": "1 l",
            "size_value": 1,
            "size_unit": "l",
            "package_count": None,
            "package_unit": None,
            "net_quantity_value": 1,
            "net_quantity_unit": "l",
            "attributes": {},
            "confidence": 0.95,
            "needs_review": False,
        }

    def test_accepts_a_full_payload(self) -> None:
        result = validate_ai_output(self._valid_payload())
        assert result["canonical_name"] == "Pienas Rokiškio"
        assert result["normalized_name"] == "pienas rokiskio"
        assert result["size_value"] == Decimal("1")

    def test_capitalizes_canonical_name_first_letter(self) -> None:
        payload = self._valid_payload()
        payload["canonical_name"] = "pienas rokiškio"
        result = validate_ai_output(payload)
        assert result["canonical_name"].startswith("P")

    def test_recomputes_normalized_name_from_canonical(self) -> None:
        payload = self._valid_payload()
        payload["canonical_name"] = "Sūris Džiugas"
        payload["normalized_name"] = "stale value"
        result = validate_ai_output(payload)
        assert result["normalized_name"] == "suris dziugas"

    def test_drops_out_of_range_confidence(self) -> None:
        payload = self._valid_payload()
        payload["confidence"] = 1.5
        result = validate_ai_output(payload)
        assert result["confidence"] is None

    def test_raises_when_canonical_name_missing(self) -> None:
        payload = self._valid_payload()
        payload["canonical_name"] = ""
        with pytest.raises(ValueError, match="canonical_name"):
            validate_ai_output(payload)

    def test_raises_when_required_keys_missing(self) -> None:
        with pytest.raises(ValueError, match="Missing keys"):
            validate_ai_output({"canonical_name": "x"})

    def test_raises_when_attributes_not_dict(self) -> None:
        payload = self._valid_payload()
        payload["attributes"] = "not a dict"
        with pytest.raises(ValueError, match="attributes"):
            validate_ai_output(payload)

    def test_raises_when_needs_review_not_bool(self) -> None:
        payload = self._valid_payload()
        payload["needs_review"] = "true"
        with pytest.raises(ValueError, match="needs_review"):
            validate_ai_output(payload)

    @pytest.mark.parametrize("unit", ["kg", "g", "l", "ml", "vnt"])
    def test_accepts_known_units(self, unit: str) -> None:
        payload = self._valid_payload()
        payload["size_unit"] = unit
        payload["net_quantity_unit"] = unit
        result = validate_ai_output(payload)
        assert result["size_unit"] == unit


class TestResolveCategoryIds:
    ALLOWED = [
        {
            "category_id": 1,
            "category_name": "ŠVIEŽI MAISTO PRODUKTAI",
            "subcategories": [
                {"subcategory_id": 10, "subcategory_name": "PIENO PRODUKTAI"},
                {"subcategory_id": 11, "subcategory_name": "SŪRIS"},
            ],
        },
        {"category_id": 2, "category_name": "GĖRIMAI", "subcategories": []},
    ]

    def test_resolves_category_and_subcategory(self) -> None:
        result = resolve_category_ids(
            {"category_name": "ŠVIEŽI MAISTO PRODUKTAI", "subcategory_name": "SŪRIS"},
            self.ALLOWED,
        )
        assert result == (1, 11)

    def test_returns_category_only_when_subcategory_unknown(self) -> None:
        result = resolve_category_ids(
            {"category_name": "ŠVIEŽI MAISTO PRODUKTAI", "subcategory_name": "MADE-UP"},
            self.ALLOWED,
        )
        assert result == (1, None)

    def test_returns_none_pair_for_unknown_category(self) -> None:
        result = resolve_category_ids(
            {"category_name": "NOT REAL", "subcategory_name": None},
            self.ALLOWED,
        )
        assert result == (None, None)

    def test_returns_none_pair_when_no_category_name(self) -> None:
        result = resolve_category_ids(
            {"category_name": None, "subcategory_name": "PIENO PRODUKTAI"},
            self.ALLOWED,
        )
        assert result == (None, None)


class TestBuildFallbackAiResult:
    def test_uses_clean_name_for_canonical(self) -> None:
        result = build_fallback_ai_result({
            "clean_name": "Pienas Rokiškio",
            "detected_size_value": Decimal("1"),
            "detected_size_unit": "l",
            "detected_package_count": None,
            "detected_package_unit": None,
        })
        assert result["canonical_name"] == "Pienas Rokiškio"
        assert result["normalized_name"] == "pienas rokiskio"
        assert result["needs_review"] is True
        assert result["confidence"] is None
        assert result["variant_name"] == "1 l"

    def test_handles_empty_clean_name(self) -> None:
        result = build_fallback_ai_result({
            "clean_name": "",
            "detected_size_value": None,
            "detected_size_unit": None,
            "detected_package_count": None,
            "detected_package_unit": None,
        })
        assert result["canonical_name"] == ""
        assert result["variant_name"] is None
