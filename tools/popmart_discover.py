#!/usr/bin/env python3
"""Harvest current Pop Mart product URLs into tools/popmart_watch.txt.

Pop Mart's listing pages lazy-load as you scroll, so this drives a headless
browser, scrolls each listing to pull in more items, collects every /products/
link, and writes them (deduped) to the watch file that watch_popmart.py reads.

    python tools/popmart_discover.py                 # default listing pages
    python tools/popmart_discover.py URL1 URL2 ...    # custom listing pages
"""

from __future__ import annotations

import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "popmart_watch.txt"
BASE = "https://www.popmart.com"
DEFAULT_LISTINGS = [
    "https://www.popmart.com/us/new-arrivals",
    "https://www.popmart.com/us/best-sellers",
    "https://www.popmart.com/us",
]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def harvest(listings: list[str], scrolls: int = 8) -> list[str]:
    from playwright.sync_api import sync_playwright

    found: list[str] = []
    seen: set[str] = set()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        for listing in listings:
            try:
                page.goto(listing, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)
                for _ in range(scrolls):
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(1200)
                hrefs = page.eval_on_selector_all(
                    "a[href*='/products/']", "els => els.map(e => e.getAttribute('href'))")
            except Exception as exc:
                print(f"  {listing}: {type(exc).__name__}")
                continue
            new = 0
            for h in hrefs:
                if not h:
                    continue
                url = h if h.startswith("http") else BASE + h
                if url not in seen:
                    seen.add(url)
                    found.append(url)
                    new += 1
            print(f"  {listing}: +{new} (total {len(found)})")
        browser.close()
    return found


def dedupe_by_id(urls: list[str]) -> list[str]:
    """One URL per product id; prefer the version that includes the name slug."""
    def parts(u: str) -> tuple[str, bool]:
        tail = u.split("/products/", 1)[1].split("?")[0]
        seg = tail.split("/")
        return seg[0], len(seg) > 1 and bool(seg[1])  # (id, has_slug)

    best: dict[str, str] = {}
    for u in urls:
        if "/products/" not in u:
            continue
        pid, has_slug = parts(u)
        if pid not in best or (has_slug and not parts(best[pid])[1]):
            best[pid] = u
    return list(best.values())


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    listings = argv or DEFAULT_LISTINGS
    print(f"harvesting Pop Mart product URLs from {len(listings)} listing page(s)…")
    urls = dedupe_by_id(harvest(listings))
    if not urls:
        print("no URLs found (Pop Mart may have changed its markup).")
        return 1
    header = ("# Pop Mart products to watch — one URL per line. '#' lines are ignored.\n"
              "# Auto-discovered by tools/popmart_discover.py; edit freely.\n")
    OUT.write_text(header + "\n".join(urls) + "\n")
    print(f"✓ wrote {len(urls)} product URLs to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
