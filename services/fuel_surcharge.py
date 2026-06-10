"""Fetch + cache DHL/FedEx Japan fuel surcharge %.

Scraping public pages is inherently fragile — both DHL and FedEx
change their fuel-surcharge pages with no warning. We treat the fetch
as best-effort:

  - Successful fetches are stored in data/fuel_surcharge.json with
    {pct, fetched_at, source: "auto"}.
  - If a fetch fails, the previously cached value is kept.
  - cogs.shipping reads the cache first, falling back to the env vars
    SHIPPING_DHL_FUEL_SURCHARGE_PCT / SHIPPING_FEDEX_FUEL_SURCHARGE_PCT
    set in the dashboard (so admins can always override).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)

CACHE_FILE = "fuel_surcharge.json"

DHL_URL = "https://mydhl.express.dhl/jp/ja/ship/surcharges.html#/fuel_surcharge"
FEDEX_URL = "https://www.fedex.com/ja-jp/shipping/surcharges.html"

# Fuel surcharges are historically 5–60% — anything outside this range is
# almost certainly a date/year/zip we accidentally matched.
MIN_PCT = 3.0
MAX_PCT = 70.0


def _cache_path() -> Path:
    return Path("data") / CACHE_FILE


def load_cache() -> dict:
    p = _cache_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError:
        return {}


def save_cache(data: dict) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def get_cached_pct(carrier: str) -> Optional[float]:
    """Return cached % for the carrier, or None if not present."""
    entry = load_cache().get(carrier.upper())
    if not entry:
        return None
    try:
        return float(entry.get("pct"))
    except (TypeError, ValueError):
        return None


def cached_entry(carrier: str) -> Optional[dict]:
    return load_cache().get(carrier.upper())


def _extract_first_plausible_pct(html: str, *, hint_phrases: list[str]) -> Optional[float]:
    """Look for a percentage number near one of the hint phrases.

    The hint phrases narrow the search so we don't match random YoY/zip/etc
    percentages elsewhere on the page.
    """
    for phrase in hint_phrases:
        # Find phrase, then look 800 chars forward for a percentage.
        idx = html.find(phrase)
        if idx < 0:
            continue
        window = html[idx : idx + 800]
        for m in re.finditer(r"(\d{1,2}(?:\.\d{1,2})?)\s*[％%]", window):
            val = float(m.group(1))
            if MIN_PCT <= val <= MAX_PCT:
                return val
    # Fallback: scan the whole document and take the first plausible %.
    for m in re.finditer(r"(\d{1,2}(?:\.\d{1,2})?)\s*[％%]", html):
        val = float(m.group(1))
        if MIN_PCT <= val <= MAX_PCT:
            return val
    return None


async def _fetch_html_via_browser(url: str, wait_selector: Optional[str] = None) -> Optional[str]:
    """Render a JS-heavy page with Playwright and return the resulting HTML.

    Returns None on any failure (browser install missing, timeout, etc.)
    so callers can fall back to cached / env-override values.

    For Angular SPAs the controller data usually arrives AFTER networkidle,
    so we additionally wait for any .progress__counter span to have
    non-empty text content if no specific selector was given.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.warning("playwright not installed; cannot fetch %s", url)
        return None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (compatible; SupportBot/1.0)",
                    locale="ja-JP",
                )
                page = await ctx.new_page()
                await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                if wait_selector:
                    try:
                        await page.wait_for_selector(wait_selector, timeout=20000)
                    except Exception:
                        pass
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                # Some Angular pages finish HTTP but keep digesting JS for a
                # beat. Wait until at least one percentage value is in the DOM.
                try:
                    await page.wait_for_function(
                        "() => /[0-9]+(\\.[0-9]+)?\\s*[％%]/.test(document.body.innerText)",
                        timeout=10000,
                    )
                except Exception:
                    pass
                return await page.content()
            finally:
                await browser.close()
    except Exception:
        log.exception("playwright fetch failed for %s", url)
        return None


async def fetch_dhl_pct() -> Optional[float]:
    """DHL is set to manual-only mode for now.

    The Angular SPA at mydhl.express.dhl loads percentage values
    dynamically into ng-bind spans, and the resulting page structure
    has been too unreliable to scrape (404s on the underlying data
    feeds, frequent layout changes). Until DHL exposes a stable feed,
    the dashboard's `SHIPPING_DHL_FUEL_SURCHARGE_PCT` manual override
    is the source of truth.
    """
    return None


async def fetch_fedex_pct() -> Optional[float]:
    # Try static HTML first (FedEx Japan's surcharge page typically renders
    # current values server-side). Fall back to Playwright if nothing usable
    # comes back.
    static_html: Optional[str] = None
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(
                FEDEX_URL,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; SupportBot/1.0)",
                    "Accept-Language": "ja,en;q=0.8",
                },
            )
            r.raise_for_status()
            static_html = r.text
    except Exception:
        log.warning("FedEx static fetch failed; falling through to playwright")

    hints = ["国際輸送", "燃料サーチャージ", "燃油サーチャージ", "Fuel Surcharge"]
    if static_html:
        v = _extract_first_plausible_pct(static_html, hint_phrases=hints)
        if v is not None:
            return v

    rendered = await _fetch_html_via_browser(FEDEX_URL)
    if not rendered:
        return None
    return _extract_first_plausible_pct(rendered, hint_phrases=hints)


async def refresh_all() -> dict:
    """Auto-fetch any carrier that has a working scraper. Manual-only
    carriers (e.g. DHL right now) are reported as `mode: manual` so the
    moderator broadcast doesn't claim a failed scrape every week.
    """
    cache = load_cache()
    now = time.time()
    status: dict = {}

    # DHL: manual-only mode (see fetch_dhl_pct docstring).
    status["DHL"] = {"ok": False, "mode": "manual", "kept": cache.get("DHL", {}).get("pct")}

    # FedEx: scraper is best-effort.
    fed = await fetch_fedex_pct()
    if fed is not None:
        cache["FEDEX"] = {"pct": fed, "fetched_at": now, "source": "auto"}
        status["FEDEX"] = {"ok": True, "pct": fed}
    else:
        status["FEDEX"] = {"ok": False, "kept": cache.get("FEDEX", {}).get("pct")}

    save_cache(cache)
    return status
