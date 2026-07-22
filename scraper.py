"""
Facebook Marketplace PS4 Deal Watcher
======================================

Scans Facebook Marketplace search results for PlayStation 4 listings in
Cairo, Giza, and Benha, filters them against strict model/price rules, and
sends a Telegram alert for every genuine match.

Designed to run as a short-lived script triggered on a schedule (see
.github/workflows/marketplace.yml), not as a long-running daemon.

Required environment variables:
    TELEGRAM_TOKEN  - Telegram bot token
    CHAT_ID         - Telegram chat ID to notify

Optional local file:
    storage_state.json - a saved Playwright/Facebook login session
                         (see login_helper.py). Without it, Facebook will
                         very likely show a login wall instead of results.
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("marketplace_scraper")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
RAW_CHAT_IDS = os.environ.get("CHAT_ID", "")
CHAT_IDS = [cid.strip() for cid in RAW_CHAT_IDS.split(",") if cid.strip()]

STORAGE_STATE_PATH = "storage_state.json"
SENT_LISTINGS_PATH = "sent_listings.json"
MAX_SENT_AGE_DAYS = 30  # prune the dedupe file so it doesn't grow forever

# Facebook Marketplace search URLs per target city.
#
# REPLACE ALL THREE BEFORE RELYING ON THIS.
# Facebook ties Marketplace location filtering to internal location IDs that
# cannot be reliably guessed from outside. To get a real URL: open
# facebook.com/marketplace in a browser, search "playstation 4", set the
# Location filter to the city you want, and copy the resulting URL from the
# address bar. See README.md, section "3. Set your target city URLs".
CITIES = {
    "Cairo": "https://www.facebook.com/marketplace/104088052961201/search?query=%D8%A8%D9%84%D8%A7%D9%8A%D8%B3%D8%AA%D9%8A%D8%B4%D9%86%204&exact=false",
}

# Words that mark a listing as an installment ("buy now, pay later") offer.
# Any of these appearing in the title means: never notify.
INSTALLMENT_KEYWORDS = ["تقسيط", "قسط", "مقدم", "أقساط"]

# Text markers used to detect and skip sponsored/ad listings.
SPONSORED_MARKERS = ["sponsored", "ممول"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Price parsing
# ---------------------------------------------------------------------------

def parse_price(raw_price: str) -> Optional[int]:
    """
    Extract a plain integer EGP price from a raw price string.

    Handles formats such as '6,500', '6500', '6500 جنيه', 'EGP 6500', and
    'E£6,500' by stripping everything that isn't a digit. Returns None if
    no digits are found at all (e.g. a "Free" listing).

    Examples:
        '6,500'      -> 6500
        '6500'       -> 6500
        '6500 جنيه'  -> 6500
        'EGP 6500'   -> 6500
    """
    if not raw_price:
        return None

    digits_only = re.sub(r"[^\d]", "", raw_price)
    if not digits_only:
        return None

    try:
        return int(digits_only)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Listing card parsing
# ---------------------------------------------------------------------------

def is_sponsored(card_text: str) -> bool:
    """Return True if a listing card's text marks it as a sponsored ad."""
    lower = card_text.lower()
    return any(marker in lower for marker in SPONSORED_MARKERS)


def extract_listing_info(card_text: str) -> Optional[dict]:
    """
    Pull {title, price} out of a listing card's raw inner text.

    Facebook typically renders each card's visible text as separate lines,
    usually in the order: price, title, location. This is a heuristic, not
    a guarantee, and may need adjusting if Facebook changes its markup (see
    README Troubleshooting). We identify the price line by pattern rather
    than fixed position, then take the next non-trivial line as the title,
    so this is somewhat resilient to line-order changes.
    """
    lines = [line.strip() for line in card_text.split("\n") if line.strip()]
    if not lines:
        return None

    price = None
    price_line_index = None
    for idx, line in enumerate(lines):
        looks_like_price = bool(re.search(r"\d", line)) and (
            "egp" in line.lower()
            or "جنيه" in line
            or "£" in line
            or bool(re.fullmatch(r"[\d,.\s]+", line))
        )
        if looks_like_price:
            candidate = parse_price(line)
            if candidate:
                price = candidate
                price_line_index = idx
                break

    if price is None:
        return None  # no usable price found (e.g. "Free") - skip this card

    title = None
    for idx, line in enumerate(lines):
        if idx == price_line_index:
            continue
        if len(line) > 2:
            title = line
            break

    if not title:
        return None

    return {"title": title, "price": price}


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def contains_installment_terms(title: str) -> bool:
    """True if the title mentions any installment-plan keyword."""
    return any(keyword in title for keyword in INSTALLMENT_KEYWORDS)


def classify_listing(title: str, price: int) -> Optional[str]:
    """
    Classify a listing as 'PRO', 'SLIM', or 'FAT' using the exact
    keyword + price rules below. Returns None if it matches no category.

    Do not change this logical grouping - it is intentional and exact.
    """
    title_lower = title.lower()

    is_pro = (
        ('pro' in title_lower or 'برو' in title_lower)
        and (4000 <= price <= 12000)
    )
    is_slim = (
        ('slim' in title_lower or 'سليم' in title_lower)
        and (4000 <= price <= 9500)
    )
    is_fat = (
        ('fat' in title_lower or 'فات' in title_lower or 'عادي' in title_lower)
        and (4000 <= price <= 8050)
    )

    if is_pro:
        return "PRO"
    if is_slim:
        return "SLIM"
    if is_fat:
        return "FAT"
    return None


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_alert(chat_id: str, title: str, price: int, link: str, retries: int = 2) -> bool:
    """
    Send a formatted deal alert to the configured Telegram chat.
    Returns True if Telegram accepted the message, False otherwise.
    """
    if not TELEGRAM_TOKEN or not chat_id:
        logger.error("TELEGRAM_TOKEN or CHAT_ID is not set - cannot send alert.")
        return False

    message = (
        "🔥 صفقة بلايستيشن حقيقية!\n"
        f"🎮 الجهاز: {title}\n"
        f"💰 السعر: {price} جنيه\n"
        "🔗 رابط الإعلان على الماركت بليس:\n"
        f"{link}"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}

    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            logger.info("Telegram alert sent: %s", title)
            return True
        except requests.exceptions.RequestException as exc:
            logger.warning("Telegram send attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(2)

    logger.error("Giving up on Telegram alert for: %s", title)
    return False


# ---------------------------------------------------------------------------
# Dedupe persistence (survives across separate scheduled runs)
# ---------------------------------------------------------------------------

def load_sent_listings() -> dict:
    """Load {link: sent_timestamp} for listings already notified about."""
    path = Path(SENT_LISTINGS_PATH)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s (%s) - starting fresh.", SENT_LISTINGS_PATH, exc)
        return {}


def save_sent_listings(sent: dict) -> None:
    """Persist notified links to disk, pruning anything older than the cutoff."""
    cutoff = time.time() - (MAX_SENT_AGE_DAYS * 86400)
    pruned = {link: ts for link, ts in sent.items() if ts >= cutoff}
    try:
        with open(SENT_LISTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(pruned, f)
    except OSError as exc:
        logger.warning("Could not save %s: %s", SENT_LISTINGS_PATH, exc)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def scrape_city(browser, city_name: str, url: str) -> list:
    """
    Scrape one city's Marketplace search results.

    Returns a list of {title, price, link} dicts for non-sponsored listings
    with a parseable price. Never raises - errors are logged and result in
    an empty (or partial) list so one city's failure doesn't stop the rest.
    """
    listings = []
    context = None
    try:
        context_kwargs = {}
        if Path(STORAGE_STATE_PATH).exists():
            context_kwargs["storage_state"] = STORAGE_STATE_PATH

        context = browser.new_context(
            locale="ar-EG",
            timezone_id="Africa/Cairo",
            viewport={"width": 1366, "height": 768},
            user_agent=USER_AGENT,
            **context_kwargs,
        )
        page = context.new_page()
        page.goto(url, timeout=45000, wait_until="domcontentloaded")

        try:
            page.wait_for_selector('a[href*="/marketplace/item/"]', timeout=20000)
        except PlaywrightTimeoutError:
            logger.warning(
                "[%s] No listing cards appeared - likely a login wall, "
                "checkpoint, or changed markup. See README Troubleshooting.",
                city_name,
            )
            return listings

        # Scroll a few times so more (lazy-loaded) cards render.
        for _ in range(3):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1500)

        cards = page.query_selector_all('a[href*="/marketplace/item/"]')
        logger.info("[%s] %d candidate cards found.", city_name, len(cards))

        seen_this_city = set()
        for card in cards:
            try:
                href = card.get_attribute("href")
                if not href:
                    continue

                link = href.split("?")[0]
                if not link.startswith("http"):
                    link = f"https://www.facebook.com{link}"

                if link in seen_this_city:
                    continue
                seen_this_city.add(link)

                card_text = card.inner_text()
                if is_sponsored(card_text):
                    continue

                info = extract_listing_info(card_text)
                if not info:
                    continue

                listings.append({"title": info["title"], "price": info["price"], "link": link})

            except Exception as exc:  # a single bad card shouldn't kill the run
                logger.debug("[%s] Skipped a card (%s).", city_name, exc)
                continue

    except PlaywrightTimeoutError as exc:
        logger.error("[%s] Page load timed out: %s", city_name, exc)
    except Exception as exc:  # keep scanning other cities even if this one fails
        logger.error("[%s] Unexpected error: %s", city_name, exc)
    finally:
        if context:
            context.close()

    return listings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Starting Facebook Marketplace PS4 scan...")

    if not TELEGRAM_TOKEN or not CHAT_IDS:
        logger.error("TELEGRAM_TOKEN and/or CHAT_IDS are missing. Exiting.")
        sys.exit(1)

    sent_listings = load_sent_listings()   # persisted across scheduled runs
    notified_this_run = set()              # in-memory, this run only
    matches_sent = 0

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                for city_name, url in CITIES.items():
                    logger.info("Scanning %s...", city_name)
                    listings = scrape_city(browser, city_name, url)
                    logger.info("[%s] %d listings parsed.", city_name, len(listings))

                    for listing in listings:
                        title, price, link = listing["title"], listing["price"], listing["link"]

                        # Skip anything we've already notified about, this run
                        # or a previous one.
                        if link in sent_listings or link in notified_this_run:
                            continue

                        if contains_installment_terms(title):
                            logger.info("Skipped (installment terms): %s", title)
                            continue

                        category = classify_listing(title, price)
                        if not category:
                            continue

                        logger.info("Match [%s]: %s - %s EGP", category, title, price)
                        sent_ok = False
                        for chat_id in CHAT_IDS:
                            if send_telegram_alert(chat_id, title, price, link):
                                sent_ok = True
                                
                        if sent_ok:
                            notified_this_run.add(link)
                            sent_listings[link] = time.time()
                            matches_sent += 1

                    time.sleep(2)  # be a little gentler between cities
            finally:
                browser.close()
    except Exception as exc:  # log fatal errors but still persist dedupe state
        logger.error("Fatal error during scan: %s", exc, exc_info=True)
    finally:
        save_sent_listings(sent_listings)

    logger.info("Scan complete. %d new deal(s) sent.", matches_sent)


if __name__ == "__main__":
    main()
