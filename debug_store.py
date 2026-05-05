"""Inspect what a store config produces — including skipped cards and why.

Usage:
    python debug_store.py <store>

Writes `debug_<store>.json` with one entry per card found by the container
selector, including extraction result and a `_skipped_reason` for invalid ones.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from bs4 import BeautifulSoup

from engine import BaseScraper, StoreConfig
from env_loader import load_env
from fetcher import Fetcher

ROOT = Path(__file__).parent
load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _why_invalid(product) -> str | None:
    if product is None:
        return "extract returned None (likely no name)"
    if not product.name:
        return "no name"
    if product.price is None and not product.discount and not product.condition:
        return "no price AND no discount AND no condition"
    return None


def _scrape_with_diagnostics(config: StoreConfig) -> list[dict]:
    fetcher = Fetcher()
    try:
        scraper = BaseScraper(config, fetcher=fetcher)
        html = fetcher.fetch(
            url=config.url,
            fetch_type=config.type,
            headers=config.headers,
            scroll=config.scroll,
            wait_for=config.wait_for,
            load_more=config.load_more,
            dismiss=config.dismiss,
            viewport=config.viewport,
        )
        if not html:
            logger.error("Failed to fetch %s", config.url)
            return []

        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(config.container)
        logger.info("Found %d cards using selector '%s'", len(cards), config.container)

        results: list[dict] = []
        for idx, card in enumerate(cards):
            try:
                product = scraper._extract(card)
            except Exception as exc:
                results.append({
                    "_index": idx,
                    "_skipped_reason": f"extraction error: {type(exc).__name__}: {exc}",
                    "_card_html_excerpt": str(card)[:600],
                })
                continue

            entry: dict = {"_index": idx}
            if product is not None:
                entry.update(asdict(product))

            reason = _why_invalid(product)
            if reason:
                entry["_skipped_reason"] = reason
                entry["_card_html_excerpt"] = str(card)[:600]
            results.append(entry)
        return results
    finally:
        fetcher.close()


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <store>")
        print("e.g.   python debug_store.py rimi")
        return 2

    store = sys.argv[1]
    config_path = ROOT / "configs" / f"{store}.json"
    if not config_path.exists():
        logger.error("No config at %s", config_path)
        return 1

    config = StoreConfig.from_file(config_path)
    results = _scrape_with_diagnostics(config)

    out_path = ROOT / f"debug_{store}.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    valid = sum(1 for r in results if "_skipped_reason" not in r)
    skipped = len(results) - valid
    print()
    print(f"=== {store} ===")
    print(f"Total cards: {len(results)}")
    print(f"Valid:       {valid}")
    print(f"Skipped:     {skipped}")
    print(f"Dumped to:   {out_path.name}")

    if skipped:
        print()
        print("Skipped reasons:")
        from collections import Counter
        for reason, n in Counter(r["_skipped_reason"] for r in results if "_skipped_reason" in r).most_common():
            print(f"  {n:4d}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
