"""Tests for the within-week duplicate key (`content_hash`).

These exercise only the pure `build_external_id` function — no database
connection is opened.
"""

from __future__ import annotations

from db import build_external_id
from engine import Product


def _p(**kwargs) -> Product:
    base = {"store": "norfa", "name": "Sūris"}
    base.update(kwargs)
    return Product(**base)


class TestBuildExternalId:
    def test_identical_cards_collapse(self) -> None:
        a = _p(image="https://x.lt/img/a.jpg", price=1.99)
        b = _p(image="https://x.lt/img/a.jpg", price=2.49)  # price is NOT identity
        assert build_external_id(a) == build_external_id(b)

    def test_same_name_different_image_kept_apart(self) -> None:
        # The bug this fixes: two distinct products sharing a name on a url-less
        # store must not merge.
        a = _p(image="https://x.lt/img/a.jpg")
        b = _p(image="https://x.lt/img/b.jpg")
        assert build_external_id(a) != build_external_id(b)

    def test_different_name_kept_apart(self) -> None:
        assert build_external_id(_p(name="Sūris A")) != build_external_id(_p(name="Sūris B"))

    def test_image_url_query_is_ignored(self) -> None:
        # Cache-busting query params must not fork the key within a week.
        a = _p(image="https://x.lt/img/a.jpg")
        b = _p(image="https://x.lt/img/a.jpg?v=2")
        assert build_external_id(a) == build_external_id(b)
