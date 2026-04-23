#!/usr/bin/env python3
"""
Runner — scrape all configured stores (or a specific one) and save results.

Usage:
    python run.py                  # scrape all stores
    python run.py --store maxima   # scrape only Maxima
    python run.py --store iki norfa # scrape IKI and Norfa
    python run.py --output db      # save to database instead of JSON
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from db import DBConfig, save_raw_offers
from engine import BaseScraper, Product, StoreConfig
from env_loader import load_env
from fetcher import Fetcher

CONFIGS_DIR = Path(__file__).parent / "configs"
OUTPUT_DIR = Path(__file__).parent / "output"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

load_env()


def safe_print(text: str = "") -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))

def load_configs(store_filter: list[str] | None = None) -> list[StoreConfig]:
    configs = []
    for path in sorted(CONFIGS_DIR.glob("*.json")):
        config = StoreConfig.from_file(path)
        if store_filter and config.store not in store_filter:
            continue
        configs.append(config)
    return configs

def save_json(products: list[Product], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [p.to_dict() for p in products]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Saved %d products to %s", len(data), path)

def print_summary(products: list[Product]) -> None:
    from collections import Counter
    counts = Counter(p.store for p in products)
    safe_print("\n" + "=" * 50)
    safe_print(f"{'Store':<12} {'Products':>8}")
    safe_print("-" * 50)
    for store, count in sorted(counts.items()):
        safe_print(f"{store:<12} {count:>8}")
    safe_print("-" * 50)
    safe_print(f"{'TOTAL':<12} {len(products):>8}")
    safe_print("=" * 50)

    if products:
        safe_print("\nSample products:")
        for p in products[:5]:
            discount_str = f" ({p.discount})" if p.discount else ""
            card_str = " [card]" if p.loyalty_card else ""
            safe_print(f"  [{p.store}] {p.name} - {p.price} EUR{discount_str}{card_str}")
        if len(products) > 5:
            safe_print(f"  ... and {len(products) - 5} more")

def _scrape_products(configs: list[StoreConfig]) -> list[Product]:
    fetcher = Fetcher()
    all_products: list[Product] = []
    try:
        for config in configs:
            scraper = BaseScraper(config, fetcher=fetcher)
            products = scraper.scrape()
            all_products.extend(products)
    finally:
        fetcher.close()
    return all_products


def run_scrape_pipeline(
    *,
    store_filter: list[str] | None = None,
    output: str = "json",
    db_config: DBConfig | None = None,
    dry_run: bool = False,
) -> list[Product]:
    configs = load_configs(store_filter)
    if not configs:
        raise ValueError("No configs found. Check the configs/ directory.")

    logger.info("Loaded %d store configs: %s", len(configs), [c.store for c in configs])

    if dry_run:
        for c in configs:
            print(f"  [{c.store}] {c.url} (type={c.type}, container='{c.container}')")
        return []

    all_products = _scrape_products(configs)

    if output == "json":
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = OUTPUT_DIR / f"products_{timestamp}.json"
        save_json(all_products, out_path)

    elif output == "jsonl":
        for p in all_products:
            print(json.dumps(p.to_dict(), ensure_ascii=False))

    elif output == "stdout":
        pass

    elif output == "db":
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
    else:
        raise ValueError(f"Unsupported output mode: {output}")

    print_summary(all_products)
    return all_products


def main() -> int:
    parser = argparse.ArgumentParser(description="Universal Discount Product Scraper")
    parser.add_argument(
        "--store", "-s",
        nargs="*",
        help="Scrape only specific stores (e.g. --store maxima iki)",
    )
    parser.add_argument(
        "--output", "-o",
        choices=["json", "jsonl", "stdout", "db"],
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument("--db-host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PGPORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PGDATABASE", "discount_tracker"))
    parser.add_argument("--db-user", default=os.getenv("PGUSER", "postgres"))
    parser.add_argument("--db-password", default=os.getenv("PGPASSWORD"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Load configs and show what would be scraped without fetching",
    )
    args = parser.parse_args()
    try:
        run_scrape_pipeline(
            store_filter=args.store,
            output=args.output,
            db_config=DBConfig(
                host=args.db_host,
                port=args.db_port,
                dbname=args.db_name,
                user=args.db_user,
                password=args.db_password,
            ),
            dry_run=args.dry_run,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())