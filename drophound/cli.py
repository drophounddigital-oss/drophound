"""DropHound command line.

  python -m drophound init-db          create the database schema
  python -m drophound seed             load the demo catalog + history
  python -m drophound run [--loop]     run the monitor->alert pipeline
  python -m drophound digest           print today's digest
  python -m drophound resale-refresh   refresh all resale snapshots
  python -m drophound serve            start the web app
  python -m drophound demo             one-shot: init + seed + run + serve hint
"""

from __future__ import annotations

import argparse
import sys
import time

from . import db, seed as seed_mod
from .config import get_settings
from .engine import digest as digest_mod
from .engine import resale
from .engine.pipeline import run_cycle


def _conn(settings):
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    return conn


# Keywords that mark a product as on-niche (blind-box designer toys) for bulk import.
DEFAULT_KEYWORDS = (
    "blind box,blindbox,labubu,pop mart,popmart,the monsters,molly,skullpanda,"
    "crybaby,hirono,dimoo,nanci,baby three,zsiga,smiski,sonny angel,tokidoki,"
    "unicorno,mystery box,mystery mini,kidrobot,dunny,sanrio,hello kitty,"
    "blind bag,surprise,gashapon,figure series"
)


def is_relevant(title: str, vendor: str, ptype: str, keywords: list[str]) -> bool:
    blob = f"{title} {vendor} {ptype}".lower()
    return any(k in blob for k in keywords)


def cmd_init_db(args, settings) -> int:
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    print(f"✓ schema ready at {settings.db_path}")
    return 0


def cmd_seed(args, settings) -> int:
    conn = _conn(settings)
    counts = seed_mod.seed(conn, settings, reset=not args.keep)
    print("✓ seeded demo data:")
    for k, v in counts.items():
        print(f"    {k:18} {v}")
    return 0


def _print_cycle(summary: dict) -> None:
    print(f"  observations={summary['observations']} "
          f"events={summary['events_created']} {summary['by_type'] or ''}")
    for e in summary["events"]:
        price = f"${e['price']:.2f}" if e["price"] is not None else "—"
        print(f"    · {e['event_type']:10} {e['name']}  {price}")
    print(f"  broadcasts={summary['broadcasts']} "
          f"premium_matches={summary['premium_matches']} "
          f"watch_matches={summary.get('watch_matches', 0)} "
          f"personal_emails={summary.get('personal_emails', 0)} "
          f"resale_refreshed={summary['resale_refreshed']}")


def cmd_run(args, settings) -> int:
    conn = _conn(settings)
    if db.one(conn, "SELECT COUNT(*) c FROM products")["c"] == 0:
        print("! no products — run `python -m drophound seed` first", file=sys.stderr)
        return 1
    monitor = None
    if args.source == "http":
        from .engine.monitors import HttpMonitor
        monitor = HttpMonitor(settings)
        print("using live HTTP monitor (one request per product page / Shopify .js)")
    elif args.source == "shopify":
        from .engine.monitors import ShopifyStoreMonitor
        monitor = ShopifyStoreMonitor(settings)
        print("using Shopify store monitor (one request per store — best for many products)")
    interval = args.interval or settings.monitor_interval
    cycle = 0
    try:
        while True:
            cycle += 1
            print(f"— cycle {cycle} —")
            _print_cycle(run_cycle(conn, settings, monitor=monitor))
            if not args.loop and cycle >= args.cycles:
                break
            print(f"  …waiting {interval}s until the next check (press Ctrl+C to stop)")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


def cmd_digest(args, settings) -> int:
    conn = _conn(settings)
    d = digest_mod.build_digest(conn, settings, args.period)
    print(d["body"])
    print(f"\n[generated_with={d['generated_with']}]  captions:")
    for c in d["captions"]:
        print(f"  · {c}")
    return 0


def cmd_resale(args, settings) -> int:
    conn = _conn(settings)
    n = resale.refresh_all(conn, settings)
    print(f"✓ refreshed resale snapshots for {n} products")
    return 0


def cmd_add_product(args, settings) -> int:
    import httpx
    from urllib.parse import urlparse
    from .util import iso, now_utc, thumb_color

    url = args.url.split("?")[0].rstrip("/")
    if "/products/" not in url:
        print("! that doesn't look like a Shopify product URL (needs /products/...)",
              file=sys.stderr)
        return 1
    try:
        r = httpx.get(url + ".js", timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/124 Safari/537.36"})
        product = r.json()  # the .js endpoint returns the product object directly
    except Exception as exc:
        print(f"! couldn't read Shopify data at {url}.js ({exc}).", file=sys.stderr)
        print("  Only Shopify stores expose this; for Pop Mart use the browser watcher.")
        return 1

    variants = product.get("variants", [])
    available = bool(product.get("available")) if "available" in product \
        else any(v.get("available") for v in variants)
    # The .js endpoint reports prices in integer cents.
    price = None
    if variants:
        try:
            price = round(float(variants[0].get("price")) / 100.0, 2)
        except (TypeError, ValueError):
            price = None

    domain = urlparse(url).netloc.replace("www.", "")
    handle = product.get("handle") or str(product.get("id"))
    sku = (args.sku or f"SHOP-{domain.split('.')[0][:10]}-{handle}")[:60]

    conn = _conn(settings)
    if db.one(conn, "SELECT id FROM products WHERE sku = ?", (sku,)):
        print(f"! already watching sku {sku}")
        return 1

    cur = db.execute(
        conn,
        """INSERT INTO products (sku, brand, line, character, name, retailer, region,
           product_url, image_hint, retail_price, currency, in_stock_signal, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sku, args.brand or product.get("vendor") or domain,
         product.get("type") or product.get("product_type") or "",
         args.character or product.get("vendor") or "Unknown",
         product.get("title") or handle, domain, args.region, url,
         thumb_color(product.get("title") or handle),
         price, "USD", None, iso(now_utc())),
    )
    pid = cur.lastrowid
    # Baseline event = current live status, so monitoring only alerts on CHANGES.
    db.execute(
        conn,
        """INSERT INTO restock_events (product_id, event_type, status, price, note, source, detected_at)
           VALUES (?,?,?,?,?,?,?)""",
        (pid, "restock" if available else "sold_out", "in_stock" if available else "sold_out",
         price, "baseline (added to watch list)", "baseline", iso(now_utc())),
    )
    print(f"✓ now watching #{pid}: {product.get('title')}")
    print(f"    store: {domain} | price: ${price} | live status: "
          f"{'IN STOCK' if available else 'sold out'}")
    print("    monitor it with:  python -m drophound run --source http --loop")
    return 0


def cmd_bulk_import(args, settings) -> int:
    import httpx
    from .util import iso, now_utc, thumb_color

    keywords = [k.strip().lower() for k in (args.filter or DEFAULT_KEYWORDS).split(",")
                if k.strip()]
    conn = _conn(settings)
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/124 Safari/537.36"}
    total_added = total_seen = 0

    for store in args.store:
        domain = store.replace("https://", "").replace("http://", "").strip("/")
        added = 0
        for page in range(1, args.pages + 1):
            try:
                resp = httpx.get(f"https://{domain}/products.json?limit=250&page={page}",
                                 timeout=30, follow_redirects=True, headers=headers)
                products = resp.json().get("products", [])
            except Exception as exc:
                print(f"  {domain}: page {page} error ({exc})")
                break
            if not products:
                break
            for p in products:
                total_seen += 1
                if not is_relevant(p.get("title", ""), p.get("vendor", ""),
                                   p.get("product_type", ""), keywords):
                    continue
                handle = p.get("handle") or str(p.get("id"))
                sku = (f"SHOP-{domain.split('.')[0][:10]}-{handle}")[:60]
                if db.one(conn, "SELECT 1 FROM products WHERE sku = ?", (sku,)):
                    continue
                variants = p.get("variants", [])
                available = any(v.get("available") for v in variants)
                price = None
                if variants:
                    try:
                        price = float(variants[0].get("price"))  # list endpoint = dollars
                    except (TypeError, ValueError):
                        price = None
                cur = db.execute(
                    conn,
                    """INSERT INTO products (sku, brand, line, character, name, retailer,
                       region, product_url, image_hint, retail_price, currency,
                       in_stock_signal, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (sku, p.get("vendor") or domain, p.get("product_type") or "",
                     p.get("vendor") or "Unknown", p.get("title") or handle, domain,
                     args.region, f"https://{domain}/products/{handle}",
                     thumb_color(p.get("title") or handle),
                     price, "USD", None, iso(now_utc())),
                )
                db.execute(
                    conn,
                    """INSERT INTO restock_events (product_id, event_type, status, price,
                       note, source, detected_at) VALUES (?,?,?,?,?,?,?)""",
                    (cur.lastrowid, "restock" if available else "sold_out",
                     "in_stock" if available else "sold_out", price,
                     "baseline (bulk import)", "baseline", iso(now_utc())),
                )
                added += 1
                total_added += 1
            if len(products) < 250:
                break
        print(f"  {domain:24} +{added}")
    print(f"✓ imported {total_added} products (scanned {total_seen})")
    print("  monitor them with:  python -m drophound run --source shopify --loop")
    return 0


def cmd_test_alert(args, settings) -> int:
    from .engine.alerts import AlertMessage, broadcast_dispatchers
    msg = AlertMessage(
        title="DropHound test alert",
        text=("🐾 DropHound is connected! This is a test alert — real drop "
              "alerts will look like this, the moment something restocks."),
        url=f"{settings.base_url}/drops",
    )
    any_live = False
    for d in broadcast_dispatchers(settings):
        result = d.send(msg)
        extra = f" ({result.detail})" if result.detail else ""
        print(f"  {result.channel:9} -> {result.status}{extra}")
        any_live = any_live or result.status == "sent"
    if any_live:
        print("\n✓ Sent for real on at least one channel — go check your Telegram channel.")
    else:
        print("\nAll channels are dry-run (no keys found in .env yet). Add them to go live.")
    return 0


def cmd_backup(args, settings) -> int:
    from . import backup as backup_mod
    conn = _conn(settings)
    try:
        dest = backup_mod.backup(conn, settings.db_path)
        print(f"✓ backup written: {dest}")
    finally:
        conn.close()
    return 0


def cmd_restore(args, settings) -> int:
    from . import backup as backup_mod
    from pathlib import Path
    backups = backup_mod.list_backups(settings.db_path)
    if not getattr(args, "file", None):
        if not backups:
            print("No backups found.")
            return 1
        print("Available backups (newest first):")
        for i, b in enumerate(backups):
            print(f"  [{i}] {b.name}  ({b.stat().st_size // 1024} KB)")
        print("\nRun:  python -m drophound restore <file>")
        return 0
    target = Path(args.file)
    if not target.is_absolute():
        # Try resolving relative to the backup dir
        guesses = [b for b in backups if b.name == target.name or str(b) == str(target)]
        if not guesses:
            print(f"Cannot find backup: {args.file}")
            return 1
        target = guesses[0]
    backup_mod.restore(target, settings.db_path)
    print(f"✓ restored from {target.name}")
    return 0


def cmd_serve(args, settings) -> int:
    import uvicorn
    print(f"→ DropHound on http://{args.host}:{args.port}  (db: {settings.db_path})")
    uvicorn.run("drophound.web.app:app", host=args.host, port=args.port,
                log_level="info", reload=args.reload)
    return 0


def cmd_demo(args, settings) -> int:
    conn = _conn(settings)
    counts = seed_mod.seed(conn, settings, reset=True)
    print(f"✓ seeded {counts['products']} products, {counts['events']} events")
    print("— running one engine cycle —")
    _print_cycle(run_cycle(conn, settings))
    print("\nNext:  python -m drophound serve   then open http://localhost:8000")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="drophound", description="DropHound CLI")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the database schema").set_defaults(func=cmd_init_db)

    sp = sub.add_parser("seed", help="load demo catalog + history")
    sp.add_argument("--keep", action="store_true", help="don't wipe existing data first")
    sp.set_defaults(func=cmd_seed)

    rp = sub.add_parser("run", help="run the monitor->alert pipeline")
    rp.add_argument("--cycles", type=int, default=1, help="number of cycles (default 1)")
    rp.add_argument("--loop", action="store_true", help="run forever")
    rp.add_argument("--interval", type=int, default=0, help="seconds between cycles")
    rp.add_argument("--source", choices=["sample", "http", "shopify"], default="sample",
                    help="sample=simulated (default); http=per-product; shopify=per-store batch")
    rp.set_defaults(func=cmd_run)

    bp = sub.add_parser("bulk-import", help="bulk-add many real products from Shopify stores")
    bp.add_argument("--store", action="append", required=True,
                    help="store domain, repeatable (e.g. --store strangecattoys.com)")
    bp.add_argument("--pages", type=int, default=4, help="pages of 250 to scan per store")
    bp.add_argument("--filter", default=None, help="comma keywords (default = blind-box niche)")
    bp.add_argument("--region", default="US")
    bp.set_defaults(func=cmd_bulk_import)

    dp = sub.add_parser("digest", help="print the daily/weekly digest")
    dp.add_argument("--period", choices=["daily", "weekly"], default="daily")
    dp.set_defaults(func=cmd_digest)

    sub.add_parser("resale-refresh", help="refresh all resale snapshots").set_defaults(func=cmd_resale)

    sub.add_parser("test-alert", help="send one test alert to configured channels").set_defaults(func=cmd_test_alert)

    ap = sub.add_parser("add-product", help="watch a real Shopify product by URL")
    ap.add_argument("url", help="full Shopify product URL (…/products/…)")
    ap.add_argument("--brand", help="override brand")
    ap.add_argument("--character", help="override character")
    ap.add_argument("--sku", help="override generated SKU")
    ap.add_argument("--region", default="US")
    ap.set_defaults(func=cmd_add_product)

    svp = sub.add_parser("serve", help="start the web app")
    svp.add_argument("--host", default="127.0.0.1")
    svp.add_argument("--port", type=int, default=8000)
    svp.add_argument("--reload", action="store_true")
    svp.set_defaults(func=cmd_serve)

    sub.add_parser("demo", help="init + seed + one cycle").set_defaults(func=cmd_demo)

    sub.add_parser("backup", help="write a timestamped DB backup").set_defaults(func=cmd_backup)

    rsp = sub.add_parser("restore", help="rollback to a DB backup")
    rsp.add_argument("file", nargs="?", help="backup file path (omit to list available backups)")
    rsp.set_defaults(func=cmd_restore)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    return args.func(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
