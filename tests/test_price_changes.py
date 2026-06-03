"""Tests for the change-percentage arithmetic behind `price_changes`.

These exercise only the pure helper — the raw_name matching that selects the
previous price is SQL and is covered by the Laravel rebuild command tests.
"""

from __future__ import annotations

from scripts.process_raw_offers_normalized import compute_change_pct


class TestComputeChangePct:
    def test_rise(self) -> None:
        assert compute_change_pct(4.49, 4.99) == 11.14

    def test_drop_is_negative(self) -> None:
        assert compute_change_pct(2.00, 1.50) == -25.0

    def test_none_inputs_yield_none(self) -> None:
        assert compute_change_pct(None, 4.49) is None
        assert compute_change_pct(4.49, None) is None

    def test_zero_previous_yields_none(self) -> None:
        assert compute_change_pct(0, 4.49) is None

    def test_identical_prices_yield_none(self) -> None:
        assert compute_change_pct(4.49, 4.49) is None

    def test_handles_decimal_like_strings(self) -> None:
        assert compute_change_pct("2.00", "3.00") == 50.0
