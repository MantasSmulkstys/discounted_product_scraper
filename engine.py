from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
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

    def to_dict(self) -> dict:
        return asdict(self)

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
        # Try each option in order; first non-None price wins. Useful when a
        # store renders different markup for different promo types (e.g. Rimi
        # has both ".price-label__price" cards and "-has-discount" cards).
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
        # Defend against markup that reuses the "integer" slot to show a
        # discount label instead of a real price (e.g. IKI's card block
        # can render either "0" + ".99" for €0.99 or "-20%" when the only
        # thing displayed is the loyalty card discount percentage).
        if "%" in int_text:
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
        # Opt-in safety net for feeds where the same promo box can render
        # either a percent ("-40%") or an absolute final price ("0,99 €") —
        # setting requirePercent avoids silently writing an absolute amount
        # into the discount column as if it were a percent.
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

    # Fallback format: try each option in order, return the first non-empty
    # result. Useful when a field (e.g. `condition`) can be represented by
    # one of several distinct DOM shapes, like IKI's pigintuvas coupon vs.
    # its "1+1" / "2+1" nplusn badge.
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
        # When `textSeparator` is provided, inline text nodes inside the
        # selected element are joined with it instead of being glued
        # together. Useful for promo wrappers where the label and the price
        # value sit in sibling spans (e.g. IKI's ".pigintuvas_wrapper").
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
        return self._scrape_single(self.config.url)

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
        # When oldPrice uses a structured price spec (e.g. split integer /
        # fraction selectors), route it through resolve_price so it can pull
        # from two elements the same way the main price does. Plain string /
        # attribute-style configs keep the original read_field path.
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

    def _validate(self, product: Product) -> bool:
        if not product.name:
            return False
        if product.price is None and not product.discount and not product.condition:
            return False
        return True