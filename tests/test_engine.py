"""Tests for the scraping engine's extraction + debug diagnostics.

A stub fetcher feeds canned HTML so these run without network access.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from engine import BaseScraper, StoreConfig, resolve_discount, resolve_price


class StubFetcher:
    def __init__(self, html: str) -> None:
        self.html = html

    def fetch(self, **_kwargs) -> str:
        return self.html

    def close(self) -> None:
        pass


class UrlFetcher:
    """Returns different HTML per URL, so discovery + multi-page scraping can be
    exercised without network access."""

    def __init__(self, by_url: dict[str, str]) -> None:
        self.by_url = by_url
        self.fetched: list[str] = []

    def fetch(self, *, url: str, **_kwargs) -> str:
        self.fetched.append(url)
        return self.by_url.get(url, "")

    def close(self) -> None:
        pass


def _config() -> StoreConfig:
    return StoreConfig(
        store="test",
        url="http://example.test/offers",
        type="static",
        container=".card",
        selectors={"name": "h2", "price": ".price"},
    )


HTML = """
<div class="card"><h2>Pienas</h2><span class="price">0,99 &euro;</span></div>
<div class="card"><span class="price">1,99 &euro;</span></div>
<div class="card"><h2>Sūris</h2></div>
"""


class TestScrapeDebug:
    def test_reports_one_entry_per_card_with_skip_reasons(self) -> None:
        scraper = BaseScraper(_config(), fetcher=StubFetcher(HTML))
        entries = scraper.scrape_debug()

        assert len(entries) == 3  # every matched card, valid or not

        valid, no_name, no_price = entries
        assert "_skipped_reason" not in valid
        assert valid["name"] == "Pienas" and valid["price"] == 0.99

        assert no_name["_skipped_reason"] == "extract returned None (no name matched the selector)"
        assert "_card_html_excerpt" in no_name

        assert no_price["name"] == "Sūris"
        assert no_price["_skipped_reason"] == "no price, no discount, no condition"

    def test_scrape_excludes_skipped_cards(self) -> None:
        # The production path keeps only the one valid product.
        scraper = BaseScraper(_config(), fetcher=StubFetcher(HTML))
        products = scraper.scrape()
        assert [p.name for p in products] == ["Pienas"]


class TestIkiPercentDiscountCard:
    """The 'Su pigintuvu' card shows two percentages and no euro price:
    -30% in the price block (IKI Card discount) and -50% in the pigintuvas
    badge. Price must be None; discount must be the -30%, not the -50%."""

    CARD = """
    <div class="product">
      <div class="promo_price">
        <div class="price_block_rounded_red_wrapper pigintuvas_wrapper">
          <span class="pigintuvas_text">Su pigintuvu</span>
          <div class="price"><span class="main">-50</span><span class="percentage">%</span></div>
        </div>
        <div class="price_block_wrapper card_tag">
          <div class="card"><img alt="IKI Card"></div>
          <div class="price_cents"><span class="sub text-px-42">–</span></div>
          <div class="price_int">30</div>
          <div class="price_cents"><span class="sub text-px-24">%</span></div>
        </div>
      </div>
    </div>
    """

    PRICE_DEF = {
        "format": "split",
        "integer": ".price_block_wrapper > .price_int",
        "fraction": ".price_block_wrapper > .price_cents > .sub",
        "skipIfPercent": True,
    }
    DISCOUNT_DEF = {
        "format": "fallback",
        "options": [
            {
                "format": "composed",
                "requirePercent": True,
                "parts": [
                    '[class*="red_wrapper"]:not(.pigintuvas_wrapper) .minus',
                    '[class*="red_wrapper"]:not(.pigintuvas_wrapper) .main',
                    '[class*="red_wrapper"]:not(.pigintuvas_wrapper) .percentage',
                ],
            },
            {
                "format": "composed",
                "requirePercent": True,
                "parts": [
                    ".price_block_wrapper .price_int",
                    ".price_block_wrapper .price_int ~ .price_cents .sub",
                ],
            },
        ],
    }

    def _card(self):
        return BeautifulSoup(self.CARD, "html.parser").select_one(".product")

    def test_percent_block_is_not_read_as_price(self) -> None:
        assert resolve_price(self._card(), self.PRICE_DEF) is None

    def test_discount_is_the_price_block_percent_not_pigintuvas(self) -> None:
        assert resolve_discount(self._card(), self.DISCOUNT_DEF) == "-30%"

    def test_real_price_block_still_parses_and_yields_no_false_discount(self) -> None:
        html = (
            '<div class="product"><div class="price_block_wrapper">'
            '<div class="price_int">1</div>'
            '<div class="price_cents"><span class="sub">49</span></div>'
            "</div></div>"
        )
        card = BeautifulSoup(html, "html.parser").select_one(".product")
        assert resolve_price(card, self.PRICE_DEF) == 1.49
        assert resolve_discount(card, self.DISCOUNT_DEF) is None


class TestUrlDiscovery:
    INDEX = """
    <div class="offers-types"><div class="items">
        <a class="item" href="https://shop.test/a/">A</a>
        <a class="item" href="https://shop.test/b/">B</a>
    </div></div>
    <div class="offers-categories"><div class="items">
        <a href="https://shop.test/b/"><div class="item">B again</div></a>
        <a href="/c/"><div class="item">C (relative)</div></a>
    </div></div>
    """

    def _discover_config(self) -> StoreConfig:
        return StoreConfig(
            store="test",
            url="https://shop.test/a/",
            type="static",
            container=".product",
            selectors={"name": "h2", "price": ".price", "url": {"selector": "a", "attribute": "href"}},
            discover_urls={
                "url": "https://shop.test/index/",
                "selector": ".offers-types .items a, .offers-categories .items a",
            },
        )

    def test_discovers_both_navs_dedups_and_resolves_relative(self) -> None:
        fetcher = UrlFetcher({"https://shop.test/index/": self.INDEX})
        scraper = BaseScraper(self._discover_config(), fetcher=fetcher)
        urls = scraper._listing_urls()
        assert urls == ["https://shop.test/a/", "https://shop.test/b/", "https://shop.test/c/"]

    def test_scrape_dedups_products_across_pages_by_url(self) -> None:
        page = lambda pid: (
            f'<div class="product"><h2>P{pid}</h2><span class="price">1,00 €</span>'
            f'<a href="https://shop.test/p/{pid}"></a></div>'
        )
        fetcher = UrlFetcher({
            "https://shop.test/index/": self.INDEX,
            "https://shop.test/a/": page(1),
            "https://shop.test/b/": page(1) + page(2),  # P1 repeats -> deduped
            "https://shop.test/c/": page(3),
        })
        scraper = BaseScraper(self._discover_config(), fetcher=fetcher)
        products = scraper.scrape()
        assert sorted(p.name for p in products) == ["P1", "P2", "P3"]
