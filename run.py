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

def main():
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

    configs = load_configs(args.store)
    if not configs:
        logger.error("No configs found. Check the configs/ directory.")
        sys.exit(1)

    logger.info("Loaded %d store configs: %s", len(configs), [c.store for c in configs])

    if args.dry_run:
        for c in configs:
            print(f"  [{c.store}] {c.url} (type={c.type}, container='{c.container}')")
        return

    fetcher = Fetcher()
    all_products: list[Product] = []

    try:
        for config in configs:
            scraper = BaseScraper(config, fetcher=fetcher)
            products = scraper.scrape()
            all_products.extend(products)
    finally:
        fetcher.close()

    if args.output == "json":
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = OUTPUT_DIR / f"products_{timestamp}.json"
        save_json(all_products, out_path)

    elif args.output == "jsonl":
        for p in all_products:
            print(json.dumps(p.to_dict(), ensure_ascii=False))

    elif args.output == "stdout":
        pass

    elif args.output == "db":
        batch_source_name = configs[0].store if len(configs) == 1 else "multi_store_scrape"
        inserted = save_raw_offers(
            all_products,
            configs,
            DBConfig(
                host=args.db_host,
                port=args.db_port,
                dbname=args.db_name,
                user=args.db_user,
                password=args.db_password,
            ),
            source_name=batch_source_name,
        )
        logger.info("Saved %d products to raw_offers", inserted)

    print_summary(all_products)

if __name__ == "__main__":
    main()