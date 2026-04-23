from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "lt-LT,lt;q=0.9,en;q=0.8",
}

DYNAMIC_WAIT = 3
DYNAMIC_TIMEOUT = 30_000

class Fetcher:
    def __init__(self):
        self._browser = None
        self._playwright = None

    def fetch(
        self,
        url: str,
        fetch_type: str = "static",
        headers: dict | None = None,
        scroll: dict[str, Any] | None = None,
        wait_for: str | None = None,
        load_more: dict[str, Any] | None = None,
        dismiss: list[str] | None = None,
        viewport: dict[str, int] | None = None,
    ) -> str | None:
        if fetch_type == "dynamic":
            return self._fetch_dynamic(
                url, headers, scroll=scroll, wait_for=wait_for,
                load_more=load_more, dismiss=dismiss, viewport=viewport,
            )
        return self._fetch_static(url, headers)

    def _fetch_static(self, url: str, headers: dict | None = None) -> str | None:
        merged_headers = {**DEFAULT_HEADERS, **(headers or {})}
        try:
            resp = requests.get(url, headers=merged_headers, timeout=20)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except requests.RequestException as exc:
            logger.error("Static fetch failed for %s: %s", url, exc)
            return None

    def _fetch_dynamic(
        self,
        url: str,
        headers: dict | None = None,
        scroll: dict[str, Any] | None = None,
        wait_for: str | None = None,
        load_more: dict[str, Any] | None = None,
        dismiss: list[str] | None = None,
        viewport: dict[str, int] | None = None,
    ) -> str | None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            )
            return None

        try:
            if not self._playwright:
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch(headless=True)

            context_kwargs = dict(
                user_agent=DEFAULT_HEADERS["User-Agent"],
                locale="lt-LT",
                extra_http_headers=headers or {},
            )
            if viewport:
                context_kwargs["viewport"] = viewport
            context = self._browser.new_context(**context_kwargs)
            page = context.new_page()
            page.goto(url, timeout=DYNAMIC_TIMEOUT, wait_until="domcontentloaded")

            time.sleep(DYNAMIC_WAIT)

            if dismiss:
                self._dismiss_overlays(page, dismiss)

            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=15_000)
                except Exception:
                    logger.warning("Timed out waiting for selector '%s' on %s", wait_for, url)

            if scroll or load_more:
                self._expand_dynamic_content(page, scroll, load_more)
            else:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1)

            html = page.content()
            context.close()
            return html

        except Exception as exc:
            logger.error("Dynamic fetch failed for %s: %s", url, exc)
            return None

    def _dismiss_overlays(self, page: Any, selectors: list[str]) -> None:
        for selector in selectors:
            try:
                btn = page.query_selector(selector)
                if btn and btn.is_visible():
                    btn.click()
                    logger.info("Dismissed overlay via '%s'", selector)
                    time.sleep(1)
            except Exception:
                logger.debug("Could not dismiss overlay '%s'", selector)

    def _expand_dynamic_content(
        self,
        page: Any,
        scroll_config: dict[str, Any] | None,
        load_more_config: dict[str, Any] | None,
    ) -> None:
        count_selector = (
            (load_more_config or {}).get("countSelector")
            or (scroll_config or {}).get("countSelector")
        )
        max_rounds = max(
            (scroll_config or {}).get("rounds", 0),
            (load_more_config or {}).get("maxClicks", 0),
            5,
        )
        prev_count = self._count_items(page, count_selector)
        stale_rounds = 0

        for round_num in range(max_rounds):
            progressed = False

            if scroll_config:
                progressed = self._scroll_round(page, scroll_config) or progressed

            if load_more_config:
                progressed = self._click_load_more(page, load_more_config) or progressed

            current_count = self._count_items(page, count_selector)
            if current_count > prev_count:
                progressed = True

            if progressed:
                stale_rounds = 0
            else:
                stale_rounds += 1
                if stale_rounds >= 3:
                    logger.info(
                        "Dynamic content stabilised after %d rounds (count=%d)",
                        round_num + 1,
                        current_count,
                    )
                    break

            prev_count = current_count

    def _count_items(self, page: Any, selector: str | None) -> int:
        if not selector:
            return 0
        return len(page.query_selector_all(selector))

    def _click_load_more(self, page: Any, config: dict) -> bool:
        selector = config.get("selector")
        button_text = config.get("text")
        delay = config.get("delay", 2)
        count_selector = config.get("countSelector")
        btn = None

        if selector:
            btn = page.query_selector(selector)
            if btn and not btn.is_visible():
                btn = None

        if btn is None and button_text:
            try:
                candidate = page.get_by_role("button", name=button_text).first
                if candidate.count() and candidate.is_visible():
                    btn = candidate
            except Exception:
                btn = None

        if not btn:
            return False

        prev_count = self._count_items(page, count_selector)
        btn.scroll_into_view_if_needed()
        btn.click()
        time.sleep(delay)

        new_count = self._count_items(page, count_selector)
        return new_count > prev_count if count_selector else True

    def _scroll_round(self, page: Any, scroll_config: dict) -> bool:
        max_scrolls = min(
            scroll_config.get("maxScrolls", 50),
            scroll_config.get("batchScrolls", 6),
        )
        delay = scroll_config.get("delay", 1.5)
        mode = scroll_config.get("mode", "bottom")
        step = scroll_config.get("step", 1200)
        count_selector = scroll_config.get("countSelector")

        prev_height = page.evaluate("document.body.scrollHeight")
        prev_count = self._count_items(page, count_selector) if count_selector else None
        stale_rounds = 0
        progressed = False

        for i in range(max_scrolls):
            if mode == "incremental":
                page.mouse.wheel(0, step)
            else:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(delay)

            new_height = page.evaluate("document.body.scrollHeight")
            new_count = self._count_items(page, count_selector) if count_selector else None
            if new_height == prev_height and new_count == prev_count:
                stale_rounds += 1
                if stale_rounds >= 3:
                    break
            else:
                progressed = True
                stale_rounds = 0

            prev_height = new_height
            prev_count = new_count
        return progressed

    def close(self):
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()
        self._browser = None
        self._playwright = None

    def __del__(self):
        self.close()