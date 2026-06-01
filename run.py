"""Scrape pipeline used by main.py.

The only output mode is the database. Earlier JSON/JSONL/stdout modes were
removed when the project moved to weekly DB-only scraping.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from db import DBConfig, save_raw_offers
from engine import BaseScraper, Product, StoreConfig
from env_loader import load_env
from fetcher import Fetcher

CONFIGS_DIR = Path(__file__).parent / "configs"

# Files in configs/ that are not store configs and must be skipped when loading.
NON_STORE_CONFIGS = {"categories.json"}

logger = logging.getLogger(__name__)

load_env()


def _safe_print(text: str = "") -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def load_configs(store_filter: list[str] | None = None) -> list[StoreConfig]:
    configs: list[StoreConfig] = []
    for path in sorted(CONFIGS_DIR.glob("*.json")):
        if path.name in NON_STORE_CONFIGS:
            continue
        config = StoreConfig.from_file(path)
        if store_filter and config.store not in store_filter:
            continue
        configs.append(config)
    return configs


def print_summary(products: list[Product]) -> None:
    from collections import Counter

    counts = Counter(p.store for p in products)
    _safe_print("\n" + "=" * 50)
    _safe_print(f"{'Store':<12} {'Products':>8}")
    _safe_print("-" * 50)
    for store, count in sorted(counts.items()):
        _safe_print(f"{store:<12} {count:>8}")
    _safe_print("-" * 50)
    _safe_print(f"{'TOTAL':<12} {len(products):>8}")
    _safe_print("=" * 50)


def _scrape_products(configs: list[StoreConfig]) -> list[Product]:
    fetcher = Fetcher()
    all_products: list[Product] = []
    try:
        for config in configs:
            scraper = BaseScraper(config, fetcher=fetcher)
            all_products.extend(scraper.scrape())
    finally:
        fetcher.close()
    return all_products


def _scrape_debug(configs: list[StoreConfig]) -> list[dict]:
    """Per-card diagnostics for every store: valid AND skipped cards."""
    fetcher = Fetcher()
    entries: list[dict] = []
    try:
        for config in configs:
            scraper = BaseScraper(config, fetcher=fetcher)
            entries.extend(scraper.scrape_debug())
    finally:
        fetcher.close()
    return entries


def print_debug_summary(entries: list[dict]) -> None:
    from collections import Counter

    by_store: dict[str, list[dict]] = {}
    for entry in entries:
        by_store.setdefault(entry.get("store", "?"), []).append(entry)

    _safe_print("\n" + "=" * 60)
    _safe_print(f"{'Store':<12} {'Cards':>7} {'Valid':>7} {'Skipped':>8}")
    _safe_print("-" * 60)
    for store, items in sorted(by_store.items()):
        valid = sum(1 for e in items if "_skipped_reason" not in e)
        _safe_print(f"{store:<12} {len(items):>7} {valid:>7} {len(items) - valid:>8}")
    _safe_print("=" * 60)

    reasons = Counter(e["_skipped_reason"] for e in entries if "_skipped_reason" in e)
    if reasons:
        _safe_print("Skip reasons:")
        for reason, count in reasons.most_common():
            _safe_print(f"  {count:>5}  {reason}")


def run_scrape_pipeline(
    *,
    store_filter: list[str] | None = None,
    db_config: DBConfig | None = None,
) -> list[Product]:
    """Scrape every store in configs/ and persist to raw_offers."""
    configs = load_configs(store_filter)
    if not configs:
        raise ValueError("No configs found. Check the configs/ directory.")

    logger.info("Loaded %d store configs: %s", len(configs), [c.store for c in configs])

    all_products = _scrape_products(configs)

    effective_db_config = db_config or DBConfig(
        host=os.getenv("PGHOST", "localhost"),
        port=int(os.getenv("PGPORT", "5432")),
        dbname=os.getenv("PGDATABASE", "discount_tracker"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD"),
    )
    batch_source_name = configs[0].store if len(configs) == 1 else "multi_store_scrape"
    inserted = save_raw_offers(
        all_products,
        configs,
        effective_db_config,
        source_name=batch_source_name,
    )
    logger.info("Saved %d products to raw_offers", inserted)

    print_summary(all_products)
    return all_products
