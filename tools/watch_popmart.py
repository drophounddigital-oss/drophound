#!/usr/bin/env python3
"""Local Pop Mart stock watcher — free, no third-party service.

Pop Mart's site is a JavaScript app whose stock lives in a *signed* backend API,
so a plain HTTP fetch can't read it. This script drives a real headless browser
(Playwright) to open each product page, captures the page's own product-detail
API response, and reads the authoritative per-SKU `stockFlag`. When an item flips
to in-stock it POSTs to DropHound's /hook/restock webhook, which fans the alert
out to your Telegram + Discord + email.

One-time setup:
    pip install playwright
    python -m playwright install chromium

Usage:
    # 1) In one terminal, run the DropHound web app (receives the webhook):
    python -m drophound serve

    # 2) In another terminal, watch some products (URLs on the command line
    #    or one-per-line in tools/popmart_watch.txt):
    python tools/watch_popmart.py --loop --interval 120 \
        "https://www.popmart.com/us/products/7838/MEGA%20SPACE%20MOLLY..."

Notes:
    * Be polite: keep --interval reasonable (>= 60s). Respect Pop Mart's ToS.
    * State is kept in var/popmart_state.json so you only alert on real changes.
    * Set DROPHOUND_HOOK_SECRET (or --secret) if your webhook is protected.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "var" / "popmart_state.json"
DEFAULT_LIST = Path(__file__).resolve().parent / "popmart_watch.txt"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _extract(data: dict) -> tuple[bool, str | None, float | None]:
    """From a groupSpu API payload, return (in_stock, name, price)."""
    name = (data.get("commonInfo") or {}).get("title")
    in_stock = False
    price = None
    for grp in data.get("groupSpuList") or []:
        for sku in grp.get("skuList") or []:
            try:
                if int(sku.get("stockFlag") or 0) > 0:
                    in_stock = True
            except (TypeError, ValueError):
                pass
            raw = sku.get("price")
            if price is None and raw not in (None, ""):
                try:
                    val = float(raw)
                    price = round(val / 100.0, 2) if val >= 1000 else val
                except (TypeError, ValueError):
                    pass
    return in_stock, name, price


def check_product(page, url: str) -> tuple[bool | None, str | None, float | None]:
    """Load the page, capture its product API, return (in_stock|None, name, price)."""
    captured: dict = {}

    def on_resp(resp):
        if "productDetail/groupSpu" in resp.url:
            try:
                captured["data"] = resp.json().get("data")
            except Exception:
                pass

    page.on("response", on_resp)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
    except Exception:
        return None, None, None
    finally:
        page.remove_listener("response", on_resp)

    data = captured.get("data")
    if not data:
        return None, None, None
    return _extract(data)


def post_restock(webhook: str, secret: str | None, name: str | None,
                 url: str, price: float | None) -> int | None:
    body = json.dumps({
        "name": name or "Pop Mart drop", "url": url, "price": price,
        "retailer": "Pop Mart US", "region": "US", "event_type": "restock",
    }).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-DropHound-Secret"] = secret
    try:
        with urllib.request.urlopen(
            urllib.request.Request(webhook, data=body, headers=headers), timeout=20
        ) as r:
            return r.status
    except Exception as exc:
        print(f"     ! webhook error: {exc}")
        return None


def run_once(urls: list[str], webhook: str, secret: str | None) -> None:
    from playwright.sync_api import sync_playwright

    state = load_state()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=UA)
        for url in urls:
            in_stock, name, price = check_product(page, url)
            label = "IN STOCK" if in_stock else ("sold out" if in_stock is False else "unknown")
            shown = name or url.rsplit("/", 1)[-1][:48]
            print(f"  [{label:8}] {shown}")
            if in_stock:
                if state.get(url) != "in_stock":
                    print("     -> RESTOCK! pinging DropHound")
                    post_restock(webhook, secret, name, url, price)
                state[url] = "in_stock"
            elif in_stock is False:
                state[url] = "sold_out"
        browser.close()
    save_state(state)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Local Pop Mart stock watcher")
    ap.add_argument("urls", nargs="*", help="Pop Mart product URLs")
    ap.add_argument("--list", default=str(DEFAULT_LIST), help="file with one URL per line")
    ap.add_argument("--webhook", default="http://localhost:8000/hook/restock")
    ap.add_argument("--secret", default=os.environ.get("DROPHOUND_HOOK_SECRET"))
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=120)
    args = ap.parse_args(argv)

    urls = list(args.urls)
    if not urls and Path(args.list).exists():
        urls = [ln.strip() for ln in Path(args.list).read_text().splitlines()
                if ln.strip() and not ln.startswith("#")]
    if not urls:
        print(f"No URLs given. Pass them as arguments or list them in {args.list}")
        return 1

    while True:
        print(f"— checking {len(urls)} Pop Mart product(s) —")
        run_once(urls, args.webhook, args.secret)
        if not args.loop:
            break
        print(f"  …waiting {args.interval}s (Ctrl+C to stop)")
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
