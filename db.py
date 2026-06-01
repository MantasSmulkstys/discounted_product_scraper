from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
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


def make_slug(name: str) -> str:
    text = name.strip().lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or "unknown"


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
    """Within-week duplicate key (`content_hash`).

    Two scraped cards collapse to one raw_offers row only when ALL of these
    match. `image` is included so that for url-less stores (norfa, maxima,
    aibe) two genuinely different products that happen to share a name are kept
    apart — without it, the key degrades to roughly store+name and silently
    merges distinct products. `price` is deliberately excluded: it's the value
    we track over time, not part of a product's identity. Image URLs are
    normalized (query/fragment stripped) so cache-busting params don't fork the
    key within a week.
    """
    fingerprint = "|".join(
        [
            _normalize_text(product.store),
            _normalize_text(product.name),
            _normalize_url(product.url),
            _normalize_url(product.image),
            _normalize_text(product.unit_price),
            _normalize_text(product.condition),
        ]
    )
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


_PLACEHOLDER_IMAGE_TOKENS = (
    "placeholder",
    "no-image",
    "no_image",
    "default-product",
)


def _is_placeholder_image(image_url: str) -> bool:
    lowered = image_url.lower()
    return any(token in lowered for token in _PLACEHOLDER_IMAGE_TOKENS)


def compute_store_product_key(
    product: Product, store_config: StoreConfig | None
) -> tuple[str | None, str | None]:
    """Per-store stable identity for a scraped product.

    Tries signals in order and returns the first hit as (key, source):
      1. `url` — only if it differs from the store's listing page (Norfa
         points every card at the same promo URL, which would otherwise
         collapse everything into one product).
      2. `image` — canonicalized image URL, minus obvious placeholders.
      3. `text` — sha256 of (clean name + unit_price). Last resort, used
         when the store exposes neither a per-product URL nor a usable image.

    Returns (None, None) when nothing usable is present — the caller should
    treat the row as unidentifiable rather than synthesizing a key, otherwise
    every nameless row would merge into a single fake product.
    """
    listing_url = _normalize_url(store_config.url) if store_config else ""

    if product.url:
        candidate = _normalize_url(product.url)
        if candidate and candidate != listing_url:
            return candidate, "url"

    if product.image:
        candidate = _normalize_url(product.image)
        if candidate and not _is_placeholder_image(candidate):
            return candidate, "image"

    if product.name:
        fingerprint = "|".join(
            [
                _normalize_text(product.name),
                _normalize_text(product.unit_price),
            ]
        )
        if fingerprint.strip("|"):
            return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest(), "text"

    return None, None


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
    """Resolve config store identifiers to DB ids, looking up by slug.

    Slug is the stable machine identifier (matches the scraper's config
    `store` field). The DB `name` column is lowercase too — the UI capitalises
    via Str::title() — but slug is the key we route on.
    """
    slugs = sorted({make_slug(config.store) for config in store_configs})
    cur.execute("SELECT id, slug FROM stores WHERE slug = ANY(%s)", (slugs,))
    by_slug = {slug: store_id for store_id, slug in cur.fetchall()}

    store_map: dict[str, int] = {}
    for store_config in store_configs:
        slug = make_slug(store_config.store)
        if slug in by_slug:
            store_map[store_config.store] = by_slug[slug]
            continue

        cur.execute(
            """
            INSERT INTO stores (name, slug, created_at, updated_at)
            VALUES (%s, %s, NOW(), NOW())
            ON CONFLICT (slug) DO UPDATE SET updated_at = NOW()
            RETURNING id
            """,
            (store_config.store, slug),
        )
        store_map[store_config.store] = cur.fetchone()[0]

    return store_map


def _current_week_starting():
    """Monday of the current ISO week — the natural batch boundary for weekly
    scraping. Re-running the scraper on Wednesday should reuse Monday's batch
    rather than create a second one for the same promo cycle."""
    from datetime import date, timedelta

    today = date.today()
    return today - timedelta(days=today.weekday())


def create_scrape_batch(
    cur,
    store_id: int | None = None,
    source_name: str | None = None,
    notes: str | None = None,
) -> int:
    """Idempotent per (store_id, week_starting): re-running for the same week
    returns the existing batch's id rather than creating a duplicate."""
    week_starting = _current_week_starting()

    if store_id is not None:
        cur.execute(
            """
            INSERT INTO scrape_batches (store_id, source_name, notes, week_starting, started_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (store_id, week_starting) DO UPDATE SET
                source_name = EXCLUDED.source_name,
                notes = COALESCE(EXCLUDED.notes, scrape_batches.notes),
                started_at = NOW(),
                status = 'running',
                finished_at = NULL,
                updated_at = NOW()
            RETURNING id
            """,
            (store_id, source_name, notes, week_starting),
        )
    else:
        cur.execute(
            """
            INSERT INTO scrape_batches (source_name, notes, week_starting, started_at)
            VALUES (%s, %s, %s, NOW())
            RETURNING id
            """,
            (source_name, notes, week_starting),
        )

    row = cur.fetchone()
    if not row:
        raise RuntimeError("Failed to create scrape_batches row")
    return int(row[0])


def finish_scrape_batch(cur, batch_id: int) -> None:
    cur.execute(
        """
        UPDATE scrape_batches
        SET finished_at = NOW(),
            status = 'completed',
            updated_at = NOW()
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
    """Persist scraped products into raw_offers, one batch per store per week.

    The (store_id, week_starting) unique constraint on scrape_batches makes
    re-running the scraper for the same week reuse the existing batch — useful
    when a partial scrape needs to be retried mid-week.
    """
    try:
        import psycopg  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "Database output requires psycopg. Install it with: pip install psycopg[binary]"
        ) from exc

    import psycopg

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
            conn.commit()

            products_by_store: dict[str, list[Product]] = {}
            for product in products:
                products_by_store.setdefault(product.store, []).append(product)

            total_inserted = 0
            for store_name, store_products in products_by_store.items():
                store_id = store_map[store_name]
                store_config = config_map[store_name]
                batch_id = create_scrape_batch(
                    cur, store_id=store_id, source_name=source_name, notes=notes,
                )
                conn.commit()

                rows = []
                skipped = 0
                for product in store_products:
                    content_hash = build_external_id(product)
                    store_product_key, key_source = compute_store_product_key(product, store_config)
                    offer_url = _fit_varchar(_build_offer_url(product, store_config), "url")
                    if offer_url is None:
                        logger.warning("[%s] Skipping product without storable url: %s", product.store, product.name)
                        skipped += 1
                        continue

                    rows.append({
                        "content_hash": content_hash,
                        "store_product_key": store_product_key,
                        "key_source": key_source,
                        "store_id": store_id,
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
                    })

                if rows:
                    cur.executemany(
                        """
                        INSERT INTO raw_offers (
                            content_hash, store_product_key, key_source,
                            store_id, name, price, old_price, discount,
                            condition, unit_price, image, url, valid_dates,
                            loyalty_card, scraped_at, scrape_batch_id
                        )
                        VALUES (
                            %(content_hash)s, %(store_product_key)s, %(key_source)s,
                            %(store_id)s, %(name)s, %(price)s,
                            %(old_price)s, %(discount)s, %(condition)s, %(unit_price)s,
                            %(image)s, %(url)s, %(valid_dates)s, %(loyalty_card)s,
                            %(scraped_at)s, %(scrape_batch_id)s
                        )
                        ON CONFLICT (scrape_batch_id, content_hash) DO UPDATE SET
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
                            store_product_key = EXCLUDED.store_product_key,
                            key_source = EXCLUDED.key_source,
                            updated_at = NOW()
                        """,
                        rows,
                    )

                if skipped:
                    logger.info("[%s] Skipped %d products that could not fit the DB schema", store_name, skipped)

                # Same-run accounting: how many scraped products actually became
                # distinct raw_offers rows. `collapsed` = duplicates merged by the
                # (scrape_batch_id, content_hash) constraint; the difference
                # between this and a JSON-only scrape count is the DB dedup.
                distinct = len({row["content_hash"] for row in rows})
                collapsed = len(rows) - distinct
                logger.info(
                    "[%s] scraped=%d stored=%d collapsed_by_content_hash=%d skipped=%d",
                    store_name, len(store_products), distinct, collapsed, skipped,
                )

                finish_scrape_batch(cur, batch_id)
                conn.commit()
                total_inserted += distinct

    return total_inserted
