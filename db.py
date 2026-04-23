from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse, urlunparse

from engine import Product, StoreConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DBConfig:
    host: str = "localhost"
    port: int = 5432
    dbname: str = "discount_tracker"
    user: str = "postgres"
    password: str | None = None


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip().lower()


def _normalize_url(value: str | None) -> str:
    if not value:
        return ""

    parsed = urlparse(value.strip())
    path = parsed.path.rstrip("/")
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            "",
            "",
        )
    )


def build_external_id(product: Product) -> str:
    fingerprint = "|".join(
        [
            _normalize_text(product.store),
            _normalize_text(product.name),
            _normalize_url(product.url),
            _normalize_text(product.unit_price),
            _normalize_text(product.condition),
        ]
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _parse_scraped_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _store_base_url(config: StoreConfig) -> str:
    parsed = urlparse(config.url)
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _store_display_name(config: StoreConfig) -> str:
    return config.store


def _parse_discount_value(product: Product) -> float | None:
    if product.discount:
        match = re.search(r"-?\d+(?:[.,]\d+)?", product.discount)
        if match:
            return round(abs(float(match.group().replace(",", "."))), 2)

    if product.price is not None and product.old_price and product.old_price > product.price:
        return round((1 - product.price / product.old_price) * 100, 2)

    return None


def _build_offer_url(product: Product, store_config: StoreConfig) -> str:
    if product.url:
        return _normalize_url(product.url)
    return f"scraper://{store_config.store}/{build_external_id(product)[:24]}"


def _fit_varchar(value: str | None, field_name: str, max_length: int = 255) -> str | None:
    if value is None:
        return None
    if len(value) <= max_length:
        return value

    logger.warning("Dropping overlong %s value (%d > %d chars)", field_name, len(value), max_length)
    return None


def _ensure_store_ids(cur, store_configs: list[StoreConfig]) -> dict[str, int]:
    store_names = sorted({config.store for config in store_configs})
    cur.execute("SELECT id, name FROM stores WHERE name = ANY(%s)", (store_names,))
    store_map = {name: store_id for store_id, name in cur.fetchall()}

    for store_config in store_configs:
        if store_config.store in store_map:
            continue

        cur.execute(
            """
            INSERT INTO stores (name)
            VALUES (%s)
            RETURNING id
            """,
            (_store_display_name(store_config),),
        )
        store_map[store_config.store] = cur.fetchone()[0]

    return store_map


def create_scrape_batch(cur, source_name: str | None = None, notes: str | None = None) -> int:
    cur.execute(
        """
        INSERT INTO scrape_batches (source_name, notes, started_at)
        VALUES (%s, %s, NOW())
        RETURNING id
        """,
        (source_name, notes),
    )
    row = cur.fetchone()
    if not row:
        raise RuntimeError("Failed to create scrape_batches row")
    return int(row[0])


def finish_scrape_batch(cur, batch_id: int) -> None:
    cur.execute(
        """
        UPDATE scrape_batches
        SET finished_at = NOW()
        WHERE id = %s
        """,
        (batch_id,),
    )


def save_raw_offers(
    products: list[Product],
    store_configs: list[StoreConfig],
    config: DBConfig,
    source_name: str | None = None,
    notes: str | None = None,
) -> int:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "Database output requires psycopg. Install it with: pip install psycopg[binary]"
        ) from exc

    config_map = {store_config.store: store_config for store_config in store_configs}

    with psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=config.password,
    ) as conn:
        with conn.cursor() as cur:
            store_map = _ensure_store_ids(cur, store_configs)
            batch_id = create_scrape_batch(cur, source_name=source_name, notes=notes)
            conn.commit()

            rows = []
            skipped = 0
            for product in products:
                store_config = config_map[product.store]
                external_id = build_external_id(product)
                offer_url = _fit_varchar(_build_offer_url(product, store_config), "url")
                if offer_url is None:
                    logger.warning("[%s] Skipping product without storable url: %s", product.store, product.name)
                    skipped += 1
                    continue

                rows.append(
                    {
                        "external_id": external_id,
                        "store_id": store_map[product.store],
                        "name": product.name,
                        "price": product.price,
                        "old_price": product.old_price,
                        "discount": _parse_discount_value(product),
                        "condition": _fit_varchar(product.condition, "condition"),
                        "unit_price": _fit_varchar(product.unit_price, "unit_price"),
                        "image": _fit_varchar(product.image, "image"),
                        "url": offer_url,
                        "valid_dates": _fit_varchar(product.valid_dates, "valid_dates"),
                        "loyalty_card": product.loyalty_card,
                        "scraped_at": _parse_scraped_at(product.scraped_at),
                        "scrape_batch_id": batch_id,
                    }
                )

            if rows:
                cur.executemany(
                    """
                    INSERT INTO raw_offers (
                        external_id,
                        store_id,
                        name,
                        price,
                        old_price,
                        discount,
                        condition,
                        unit_price,
                        image,
                        url,
                        valid_dates,
                        loyalty_card,
                        scraped_at,
                        scrape_batch_id
                    )
                    VALUES (
                        %(external_id)s,
                        %(store_id)s,
                        %(name)s,
                        %(price)s,
                        %(old_price)s,
                        %(discount)s,
                        %(condition)s,
                        %(unit_price)s,
                        %(image)s,
                        %(url)s,
                        %(valid_dates)s,
                        %(loyalty_card)s,
                        %(scraped_at)s,
                        %(scrape_batch_id)s
                    )
                    ON CONFLICT (scrape_batch_id, external_id)
                    DO UPDATE SET
                        store_id = EXCLUDED.store_id,
                        name = EXCLUDED.name,
                        price = EXCLUDED.price,
                        old_price = EXCLUDED.old_price,
                        discount = EXCLUDED.discount,
                        condition = EXCLUDED.condition,
                        unit_price = EXCLUDED.unit_price,
                        image = EXCLUDED.image,
                        valid_dates = EXCLUDED.valid_dates,
                        loyalty_card = EXCLUDED.loyalty_card,
                        scraped_at = EXCLUDED.scraped_at,
                        scrape_batch_id = EXCLUDED.scrape_batch_id,
                        updated_at = NOW()
                    """,
                    rows,
                )

            if skipped:
                logger.info("Skipped %d products that could not fit the DB schema", skipped)

            finish_scrape_batch(cur, batch_id)
        conn.commit()

    return len(rows)
