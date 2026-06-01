from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlencode, parse_qs, urlunparse

from bs4 import BeautifulSoup, Tag

from fetcher import Fetcher
from transforms import TRANSFORMS

logger = logging.getLogger(__name__)

@dataclass
class Product:
    store: str
    name: str
    price: float | None = None
    old_price: float | None = None
    discount: str | None = None
    condition: str | None = None
    unit_price: str | None = None
    image: str | None = None
    valid_dates: str | None = None
    loyalty_card: bool = False
    url: str | None = None
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class StoreConfig:
    store: str
    url: str
    type: str
    container: str
    selectors: dict[str, Any]
    loyalty_card: dict | None = None
    transforms: dict[str, list[str]] | None = None
    headers: dict[str, str] | None = None
    pagination: dict | None = None
    scroll: dict | None = None
    wait_for: str | None = None
    load_more: dict | None = None
    dismiss: list[str] | None = None
    viewport: dict[str, int] | None = None
    extra_urls: list[str] | None = None
    discover_urls: dict | None = None

    @classmethod
    def from_file(cls, path: str | Path) -> StoreConfig:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            store=raw["store"],
            url=raw["url"],
            type=raw.get("type", "static"),
            container=raw["container"],
            selectors=raw["selectors"],
            loyalty_card=raw.get("loyaltyCard"),
            transforms=raw.get("transforms"),
            headers=raw.get("headers"),
            pagination=raw.get("pagination"),
            scroll=raw.get("scroll"),
            wait_for=raw.get("waitFor"),
            load_more=raw.get("loadMore"),
            dismiss=raw.get("dismiss"),
            viewport=raw.get("viewport"),
            extra_urls=raw.get("extraUrls"),
            discover_urls=raw.get("discoverUrls"),
        )

def parse_price_string(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = raw.strip()
    cleaned = re.sub(r"[€$EUR\s]", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace(",", ".")
    match = re.search(r"\d+\.?\d*", cleaned)
    if match:
        try:
            return round(float(match.group()), 2)
        except ValueError:
            return None
    return None


def resolve_price(card: Tag, price_def: dict | str) -> float | None:
    if isinstance(price_def, str):
        el = card.select_one(price_def)
        return parse_price_string(el.get_text() if el else None)

    fmt = price_def.get("format", "single")

    if fmt == "fallback":
        for option in price_def.get("options", []):
            result = resolve_price(card, option)
            if result is not None:
                return result
        return None

    if fmt == "single":
        el = card.select_one(price_def["selector"])
        if not el:
            return None
        return parse_price_string(el.get_text())

    elif fmt == "split":
        int_el = card.select_one(price_def["integer"])
        frac_el = card.select_one(price_def["fraction"])
        if not int_el:
            return None
        int_text = int_el.get_text().strip()
        if "%" in int_text:
            return None
        # Some stores render a discount ("-30%") inside the same price block used
        # for real prices. When opted in, treat a block containing "%" as a
        # percentage badge, not a price, so it isn't misread as e.g. 30 €.
        if price_def.get("skipIfPercent"):
            parent = int_el.parent
            if parent and "%" in parent.get_text():
                return None
        integer = re.sub(r"[^\d]", "", int_text)
        fraction = re.sub(r"[^\d]", "", frac_el.get_text().strip()) if frac_el else "00"
        if not integer:
            return None
        try:
            return round(float(f"{integer}.{fraction}"), 2)
        except ValueError:
            return None

    elif fmt == "attribute":
        el = card.select_one(price_def.get("selector", ""))
        if not el:
            return None
        attr_value = el.get(price_def["attribute"], "")
        regex = price_def.get("regex")
        if regex:
            match = re.search(regex, attr_value)
            if match:
                return parse_price_string(match.group(1))
            return None
        return parse_price_string(attr_value)

    logger.warning("Unknown price format: %s", fmt)
    return None

def resolve_discount(card: Tag, discount_def: dict | str | None) -> str | None:
    if discount_def is None:
        return None

    if isinstance(discount_def, str):
        el = card.select_one(discount_def)
        return el.get_text(strip=True) if el else None

    fmt = discount_def.get("format", "simple")

    if fmt == "fallback":
        for option in discount_def.get("options", []):
            result = resolve_discount(card, option)
            if result:
                return result
        return None

    if fmt == "composed":
        parts = []
        for selector in discount_def.get("parts", []):
            el = card.select_one(selector)
            if el:
                parts.append(el.get_text(strip=True))
        result = "".join(parts)
        if not result or not any(c.isdigit() for c in result):
            return None
        if discount_def.get("requirePercent") and "%" not in result:
            return None
        if not result.startswith("-"):
            result = f"-{result}"
        if not result.endswith("%"):
            result = f"{result}%"
        return result

    selector = discount_def.get("selector", "")
    el = card.select_one(selector)
    return el.get_text(strip=True) if el else None

def read_field(card: Tag, selector_def: dict | str | None, base_url: str = "") -> str | None:
    if selector_def is None:
        return None

    if isinstance(selector_def, str):
        el = card.select_one(selector_def)
        return el.get_text(strip=True) if el else None

    if selector_def.get("format") == "fallback":
        for option in selector_def.get("options", []):
            result = read_field(card, option, base_url)
            if result:
                return result
        return None

    selector = selector_def.get("selector", "")
    el = card.select_one(selector)
    if not el:
        return None

    attr = selector_def.get("attribute")
    if attr:
        raw = el.get(attr, "")
    else:
        text_separator = selector_def.get("textSeparator")
        if text_separator is not None:
            raw = el.get_text(text_separator, strip=True)
        else:
            raw = el.get_text(strip=True)

    if not raw:
        return None

    regex = selector_def.get("regex")
    if regex:
        match = re.search(regex, raw, re.DOTALL)
        raw = match.group(1).strip() if match else raw

    field_base_url = selector_def.get("baseUrl", base_url)
    if field_base_url and attr in ("href", "src", "data-src") and not raw.startswith("http"):
        raw = urljoin(field_base_url, raw)

    return raw


def read_image(card: Tag, image_def: dict | None, base_url: str = "") -> str | None:
    if image_def is None:
        return None

    selector = image_def.get("selector", "")
    attr = image_def.get("attribute", "src")
    el = card.select_one(selector)
    if not el:
        return None

    url = el.get(attr) or el.get("src", "")
    if not url:
        return None

    img_base = image_def.get("baseUrl", base_url)
    if img_base and not url.startswith("http"):
        url = urljoin(img_base, url)

    return url

def apply_transforms(value: str | None, transform_names: list[str]) -> str | None:
    if value is None:
        return None
    for name in transform_names:
        fn = TRANSFORMS.get(name)
        if fn:
            value = fn(value)
        else:
            logger.warning("Unknown transform: %s", name)
    return value

class BaseScraper:
    def __init__(self, config: StoreConfig, fetcher: Fetcher | None = None):
        self.config = config
        self.fetcher = fetcher or Fetcher()

    def scrape(self) -> list[Product]:
        if self.config.pagination:
            return self._scrape_paginated()
        return self._scrape_urls(self._listing_urls())

    def _listing_urls(self) -> list[str]:
        """The listing pages to scrape: either discovered from a nav on the
        site (so weekly-changing promo links never go stale) or the static
        `url` + `extra_urls` from the config."""
        if self.config.discover_urls:
            discovered = self._discover_listing_urls()
            if discovered:
                return discovered
            logger.warning(
                "[%s] URL discovery found nothing; falling back to config url",
                self.config.store,
            )
        return [self.config.url] + (self.config.extra_urls or [])

    def _discover_listing_urls(self) -> list[str]:
        """Fetch a page and read listing URLs from a nav selector. Lets a store
        track its own (weekly-rotating) promo sections instead of hardcoding
        links that go stale."""
        disc = self.config.discover_urls or {}
        source_url = disc.get("url") or self.config.url
        selector = disc.get("selector")
        attribute = disc.get("attribute", "href")
        exclude = disc.get("exclude") or []
        if not selector:
            logger.error("[%s] discoverUrls needs a 'selector'", self.config.store)
            return []

        logger.info("[%s] Discovering listing URLs from %s", self.config.store, source_url)
        html = self.fetcher.fetch(
            url=source_url,
            fetch_type=disc.get("type", self.config.type),
            headers=self.config.headers,
            wait_for=disc.get("waitFor"),
            dismiss=self.config.dismiss,
            viewport=self.config.viewport,
        )
        if not html:
            logger.error("[%s] Discovery fetch failed for %s", self.config.store, source_url)
            return []

        soup = BeautifulSoup(html, "html.parser")
        urls: list[str] = []
        seen: set[str] = set()
        for el in soup.select(selector):
            href = el.get(attribute)
            if not href:
                continue
            absolute = urljoin(source_url, href.strip())
            if any(token in absolute for token in exclude):
                continue
            if absolute not in seen:
                seen.add(absolute)
                urls.append(absolute)

        logger.info("[%s] Discovered %d listing URLs: %s", self.config.store, len(urls), urls)
        return urls

    def _scrape_urls(self, urls: list[str]) -> list[Product]:
        """Scrape each listing URL, de-duplicating products by their URL so a
        product appearing in two promo sections is only kept once."""
        store = self.config.store
        products: list[Product] = []
        seen_urls: set[str] = set()
        for url in urls:
            more = self._scrape_single(url)
            added = 0
            for product in more:
                if product.url and product.url in seen_urls:
                    continue
                if product.url:
                    seen_urls.add(product.url)
                products.append(product)
                added += 1
            logger.info("[%s] %s -> +%d new (%d dups)", store, url, added, len(more) - added)
        return products

    def _scrape_single(self, url: str) -> list[Product]:
        store = self.config.store
        logger.info("[%s] Fetching %s", store, url)

        html = self.fetcher.fetch(
            url=url,
            fetch_type=self.config.type,
            headers=self.config.headers,
            scroll=self.config.scroll,
            wait_for=self.config.wait_for,
            load_more=self.config.load_more,
            dismiss=self.config.dismiss,
            viewport=self.config.viewport,
        )

        if not html:
            logger.error("[%s] Failed to fetch page", store)
            return []

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(self.config.container)
        logger.info("[%s] Found %d product cards", store, len(cards))

        products: list[Product] = []
        for card in cards:
            try:
                product = self._extract(card)
                if product and self._validate(product):
                    products.append(product)
            except Exception:
                logger.exception("[%s] Failed to extract product from card", store)

        logger.info("[%s] Extracted %d valid products", store, len(products))
        return products

    def _scrape_paginated(self) -> list[Product]:
        store = self.config.store
        pag = self.config.pagination
        page_param = pag.get("param", "page")
        start_page = pag.get("startPage", 1)
        page_size = pag.get("pageSize")
        page_size_param = pag.get("pageSizeParam")
        max_pages = pag.get("maxPages", 100)

        all_products: list[Product] = []

        for page_num in range(start_page, start_page + max_pages):
            url = self._build_page_url(page_num, page_param, page_size, page_size_param)
            products = self._scrape_single(url)

            if not products:
                logger.info("[%s] Page %d returned 0 products — stopping", store, page_num)
                break

            all_products.extend(products)
            logger.info("[%s] Page %d: %d products (total so far: %d)",
                        store, page_num, len(products), len(all_products))

        logger.info("[%s] Pagination complete — %d total products", store, len(all_products))
        return all_products

    def _build_page_url(
        self, page: int, param: str, page_size: int | None, page_size_param: str | None,
    ) -> str:
        parsed = urlparse(self.config.url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        qs[param] = [str(page)]
        if page_size is not None and page_size_param:
            qs[page_size_param] = [str(page_size)]
        new_query = urlencode(qs, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    def _extract(self, card: Tag) -> Product | None:
        sel = self.config.selectors
        base_url = self.config.url

        name = read_field(card, sel.get("name"), base_url)
        if not name:
            return None

        price = resolve_price(card, sel.get("price", {}))
        old_price_def = sel.get("oldPrice")
        if isinstance(old_price_def, dict) and old_price_def.get("format"):
            old_price = resolve_price(card, old_price_def)
        else:
            old_price_raw = read_field(card, old_price_def, base_url)
            old_price = parse_price_string(old_price_raw)
        discount = resolve_discount(card, sel.get("discount"))
        condition = read_field(card, sel.get("condition"), base_url)
        unit_price = read_field(card, sel.get("unitPrice"), base_url)
        image = read_image(card, sel.get("image"), base_url)
        valid_dates = read_field(card, sel.get("validDates"), base_url)
        product_url = read_field(card, sel.get("url"), base_url)

        loyalty = False
        if self.config.loyalty_card:
            indicator = self.config.loyalty_card.get("indicator", "")
            loyalty = card.select_one(indicator) is not None

        transforms = self.config.transforms or {}
        name = apply_transforms(name, transforms.get("name", []))
        discount = apply_transforms(discount, transforms.get("discount", []))
        condition = apply_transforms(condition, transforms.get("condition", []))
        unit_price = apply_transforms(unit_price, transforms.get("unitPrice", []))
        valid_dates = apply_transforms(valid_dates, transforms.get("validDates", []))

        if price and old_price and not discount:
            pct = round((1 - price / old_price) * 100)
            discount = f"-{pct}%"

        return Product(
            store=self.config.store,
            name=name,
            price=price,
            old_price=old_price,
            discount=discount,
            condition=condition,
            unit_price=unit_price,
            image=image,
            valid_dates=valid_dates,
            loyalty_card=loyalty,
            url=product_url,
        )

    def _why_invalid(self, product: Product | None) -> str | None:
        """Reason a card would be dropped, or None if it's a valid product.

        Single source of truth for both `_validate` (production filter) and
        `scrape_debug` (manual review), so the two can never disagree.
        """
        if product is None:
            return "extract returned None (no name matched the selector)"
        if not product.name:
            return "no name"
        if product.price is None and not product.discount and not product.condition:
            return "no price, no discount, no condition"
        return None

    def _validate(self, product: Product) -> bool:
        return self._why_invalid(product) is None

    def _debug_urls(self) -> list[str]:
        """Listing URLs scrape() would visit (incl. discovered ones). For
        paginated stores this is page 1 only — enough to spot selector problems
        without fetching every page."""
        if self.config.pagination:
            return [self.config.url]
        return self._listing_urls()

    def scrape_debug(self) -> list[dict]:
        """Like scrape(), but returns ONE entry per matched card — including the
        ones that would be skipped — each with a `_skipped_reason` and an HTML
        excerpt when invalid. No validation filtering. For manual review only.
        """
        store = self.config.store
        entries: list[dict] = []
        index = 0

        for url in self._debug_urls():
            logger.info("[%s] (debug) Fetching %s", store, url)
            html = self.fetcher.fetch(
                url=url,
                fetch_type=self.config.type,
                headers=self.config.headers,
                scroll=self.config.scroll,
                wait_for=self.config.wait_for,
                load_more=self.config.load_more,
                dismiss=self.config.dismiss,
                viewport=self.config.viewport,
            )
            if not html:
                logger.error("[%s] (debug) Failed to fetch %s", store, url)
                continue

            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select(self.config.container)
            logger.info("[%s] (debug) %d cards from %s", store, len(cards), url)

            for card in cards:
                entry: dict = {"_index": index, "_source_url": url, "store": store}
                index += 1
                try:
                    product = self._extract(card)
                except Exception as exc:
                    entry["_skipped_reason"] = f"extraction error: {type(exc).__name__}: {exc}"
                    entry["_card_html_excerpt"] = str(card)[:600]
                    entries.append(entry)
                    continue

                if product is not None:
                    entry.update(asdict(product))

                reason = self._why_invalid(product)
                if reason:
                    entry["_skipped_reason"] = reason
                    entry["_card_html_excerpt"] = str(card)[:600]
                entries.append(entry)

        valid = sum(1 for e in entries if "_skipped_reason" not in e)
        logger.info("[%s] (debug) %d cards: %d valid, %d skipped",
                    store, len(entries), valid, len(entries) - valid)
        return entries