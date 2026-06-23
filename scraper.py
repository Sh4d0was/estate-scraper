#!/usr/bin/env python3
"""
estate-scraper — paginated listing scraper with configurable selectors.

Usage:
    python scraper.py [--max-pages N] [--output PATH] [--format csv|json]
                      [--delay SECONDS] [--workers N] [--quiet]
"""

import argparse
import csv
import json
import logging
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

import config

logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

COLUMNS = ["title", "price", "rating", "availability", "detail_url", "image_url", "scraped_at"]

# Transient HTTP status codes that are worth retrying.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class PageNotFound(Exception):
    """Raised by fetch_page when the server returns HTTP 404.

    404 is a permanent signal that the page does not exist — it is never retried.
    """


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape a paginated listings site and export to CSV or JSON.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--max-pages", type=int, default=config.DEFAULT_MAX_PAGES,
        metavar="N", help="upper bound on catalogue pages to fetch",
    )
    parser.add_argument(
        "--output", default=config.DEFAULT_OUTPUT,
        metavar="PATH", help="output file path",
    )
    parser.add_argument(
        "--format", choices=["csv", "json"], default=config.DEFAULT_FORMAT,
        help="output format",
    )
    parser.add_argument(
        "--delay", type=float, default=config.DEFAULT_DELAY,
        metavar="SECONDS", help="polite delay between requests per worker thread",
    )
    parser.add_argument(
        "--workers", type=int, default=config.DEFAULT_WORKERS,
        metavar="N", help="number of concurrent fetcher threads",
    )
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the progress bar",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

def check_robots(base_url: str, user_agent: str) -> bool:
    """Return True if scraping is permitted, False if disallowed."""
    rp = RobotFileParser()
    rp.set_url(f"{base_url.rstrip('/')}/robots.txt")
    try:
        rp.read()
    except Exception as exc:
        # A missing or unreadable robots.txt is treated as no restriction.
        log.debug("robots.txt unavailable (%s) — proceeding.", exc)
        return True
    return rp.can_fetch(user_agent, base_url)


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def make_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = user_agent
    return session


def fetch_page(url: str, session: requests.Session, delay: float, retries: int = 3) -> bytes | None:
    """Fetch *url* with a polite delay. Returns raw bytes on success, None on transient failure.

    Returns bytes (not str) so BeautifulSoup can detect the correct charset from
    the HTML <meta> tag, avoiding Latin-1 mis-decoding when the server omits a
    charset in the Content-Type header.

    Raises:
        PageNotFound: immediately, without retrying, when the server returns HTTP 404.
    """
    wait = delay
    for attempt in range(1, retries + 1):
        time.sleep(wait)
        try:
            resp = session.get(url, timeout=10)
        except requests.RequestException as exc:
            log.warning("Request error for %s: %s (attempt %d/%d)", url, exc, attempt, retries)
            wait *= 2
            continue

        if resp.ok:
            return resp.content

        if resp.status_code == 404:
            # Permanent — end of pagination or missing page. Do not retry.
            log.info("Page not found (404): %s", url)
            raise PageNotFound(url)

        if resp.status_code in _RETRYABLE_STATUS:
            log.warning("HTTP %s for %s (attempt %d/%d)", resp.status_code, url, attempt, retries)
            wait *= 2
            continue

        # Other 4xx (403, 410, …) — permanent client error, no point retrying.
        log.warning("HTTP %s for %s — skipping.", resp.status_code, url)
        return None

    log.warning("Giving up on %s after %d attempts.", url, retries)
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_rating(tag) -> int | None:
    """Extract integer rating from a p.star-rating element's class list."""
    classes = tag.get("class", [])
    # classes looks like ["star-rating", "Three"]
    for cls in classes:
        if cls in config.RATING_MAP:
            return config.RATING_MAP[cls]
    return None


def parse_card(card, page_url: str) -> dict | None:
    """Parse one article.product_pod element into a row dict, or None to skip."""
    sel = config.SELECTORS

    title_tag = card.select_one(sel["title"])
    price_tag = card.select_one(sel["price"])
    if not title_tag or not price_tag:
        return None

    title = title_tag.get("title", "").strip()
    price = price_tag.get_text(strip=True)
    if not title or not price:
        return None

    rating_tag = card.select_one(sel["rating"])
    rating = _parse_rating(rating_tag) if rating_tag else None

    avail_tag = card.select_one(sel["availability"])
    availability = avail_tag.get_text(strip=True) if avail_tag else None

    detail_url = urljoin(page_url, title_tag["href"])

    img_tag = card.select_one(sel["image"])
    image_url = urljoin(page_url, img_tag["src"]) if img_tag else None

    return {
        "title": title,
        "price": price,
        "rating": rating,
        "availability": availability,
        "detail_url": detail_url,
        "image_url": image_url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_listing_page(html: bytes, page_url: str) -> tuple[list[dict], str | None]:
    """Parse a catalogue page. Returns (items, next_page_url_or_None)."""
    soup = BeautifulSoup(html, "html.parser")

    items = []
    for card in soup.select(config.SELECTORS["card"]):
        row = parse_card(card, page_url)
        if row:
            items.append(row)

    next_tag = soup.select_one(config.SELECTORS["next_page"])
    next_url = urljoin(page_url, next_tag["href"]) if next_tag else None

    return items, next_url


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape(args: argparse.Namespace) -> list[dict]:
    """Fetch pages with a sliding-window executor, stop at pagination end.

    --max-pages is an upper bound. Scraping stops as soon as a page returns 404
    or has no "next" link, whichever comes first.
    """
    if not check_robots(config.BASE_URL, config.USER_AGENT):
        log.warning(
            "robots.txt disallows scraping %s. "
            "Respect the site owner's wishes before proceeding.",
            config.BASE_URL,
        )

    session = make_session(config.USER_AGENT)
    all_items: list[dict] = []
    seen: set[str] = set()
    last_good_page = 0

    # pending holds (page_num, page_url, future) in submission order.
    # Processing popleft() keeps output naturally sorted by page number.
    pending: deque[tuple[int, str, object]] = deque()
    next_page = 1
    stop = False

    def _submit_next() -> None:
        nonlocal next_page
        if stop or next_page > args.max_pages:
            return
        url = config.START_PAGE.format(page=next_page)
        fut = pool.submit(fetch_page, url, session, args.delay)
        pending.append((next_page, url, fut))
        next_page += 1

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            # Fill the initial window.
            for _ in range(args.workers):
                _submit_next()

            with tqdm(
                desc="Pages",
                unit="pg",
                disable=args.quiet,
            ) as pbar:
                while pending:
                    page_num, page_url, fut = pending.popleft()
                    content = None

                    try:
                        content = fut.result()
                    except PageNotFound:
                        stop = True
                    except Exception as exc:
                        log.warning("Unexpected error fetching page %d: %s", page_num, exc)

                    if content:
                        last_good_page = page_num
                        items, next_url = parse_listing_page(content, page_url)
                        for item in items:
                            if item["detail_url"] not in seen:
                                seen.add(item["detail_url"])
                                all_items.append(item)
                        if not next_url:
                            # Site's own pagination signal: this was the last page.
                            stop = True

                    pbar.update(1)

                    # Replenish the window with the next page (if not done).
                    if not stop:
                        _submit_next()

    except KeyboardInterrupt:
        log.warning("Interrupted — writing the %d items collected so far.", len(all_items))
        for _, _, fut in pending:
            fut.cancel()

    log.info("Scraped %d unique listings from %d page(s).", len(all_items), last_good_page)
    return all_items


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_csv(data: list[dict], path: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(data)
    log.info("Saved %d rows → %s", len(data), out)


def export_json(data: list[dict], path: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info("Saved %d records → %s", len(data), out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    try:
        data = scrape(args)
    except KeyboardInterrupt:
        # Fires only if the interrupt lands before the scrape loop starts
        # (e.g. during robots.txt check). scrape() handles its own interrupts.
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)

    if not data:
        log.error("No data collected. Check your selectors or network connection.")
        sys.exit(1)

    if args.format == "json":
        export_json(data, args.output)
    else:
        export_csv(data, args.output)


if __name__ == "__main__":
    main()
