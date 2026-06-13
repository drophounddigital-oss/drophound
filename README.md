# 🐾 DropHound

**Drop tracking & resale intelligence for blind-box designer-toy collectors.**
Labubu, Molly, Skullpanda, Crybaby, Hirono, Dimoo, Sonny Angel, Smiski — the
moment they drop or restock, collectors know, *before* they sell out.

This repo is a working MVP of the business in [the plan](../plan.pdf): a runnable
web app + an automation engine. It runs **end-to-end with zero configuration**
(offline sample monitor, fixture resale data, console/"dry-run" alerts), and every
external integration is a single env var away from going live.

---

## Quickstart

```bash
cd drophound
./run.sh                 # creates a venv, installs deps, seeds demo, serves
# → open http://localhost:8000
```

Or step by step:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m drophound demo          # init schema + seed demo + run one engine cycle
python -m drophound serve         # web app at http://localhost:8000
pytest                            # run the test suite
```

---

## The three layers (straight from the plan)

| Layer | What it is | Where it lives |
|------|-------------|----------------|
| **1 · Free funnel** | Public landing + live drops feed + broadcast alerts (Telegram / Discord / email) | `web/` landing & `/drops`, `engine/alerts.py` |
| **2 · Premium ($8/mo)** | Filtered alerts, eBay resale tracking, collection P/L, restock predictions | `/app`, `/collection`, `filters.py`, `patterns.py`, `engine/resale.py` |
| **3 · Affiliate / margin** | Every buy/resale link routes through affiliate tags; sponsorship slots in the digest | `affiliates.py`, `/go/{id}` redirect |

### The automation stack (the core engine)

```
monitors  →  events  →  resale refresh  →  AI digest  →  alerts  →  logs
(monitors.py) (db)     (resale.py)        (digest.py)   (alerts.py)
                         \__________ pipeline.run_cycle() __________/
```

- **Monitors** — `SampleMonitor` simulates retailer stock churn offline (default);
  `HttpMonitor` is the real, polite page-poller (Phase-1 "watch the top retailer
  pages", in code).
- **Resale** — eBay Finding API (`findCompletedItems`, sold only); falls back to
  bundled fixtures when `EBAY_APP_ID` is unset.
- **AI digest** — assembles the day's signal into a digest + social captions;
  rewrites it with Claude when `ANTHROPIC_API_KEY` is set, else a clean template.
- **Alerts** — Telegram, Discord, Resend email; each sends for real when
  configured and otherwise logs a `dry_run`.

---

## CLI

```bash
python -m drophound init-db          # create the schema
python -m drophound seed             # load demo catalog + ~10wk restock history
python -m drophound run              # one monitor→alert cycle
python -m drophound run --loop       # run forever (interval from env)
python -m drophound digest --period weekly
python -m drophound resale-refresh   # refresh all resale snapshots
python -m drophound serve            # web app
python -m drophound demo             # init + seed + one cycle (one-shot)
```

A real deployment runs `run --loop` on a worker (or cron/n8n), `serve` behind a
web host, and `digest` on a daily schedule.

---

## Configuration

Everything is optional. Copy `.env.example` → `.env` and fill in what you have.
With nothing set, the system runs fully in offline/dry-run mode.

| Variable | Enables |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Real Telegram channel alerts |
| `DISCORD_WEBHOOK_URL` | Real Discord `#drops` alerts |
| `RESEND_API_KEY` + `DROPHOUND_EMAIL_FROM` | Real email alerts (swap in Beehiiv the same way) |
| `EBAY_APP_ID` | Live eBay sold-listing resale data |
| `EBAY_CAMPAIGN_ID` / `POPMART_AFFILIATE_REF` / `STOCKX_AFFILIATE_REF` | Affiliate tags on `/go` links |
| `STRIPE_SECRET_KEY` + `STRIPE_PRICE_ID` | Real premium checkout (stub in `web/app.py:upgrade`) |
| `ANTHROPIC_API_KEY` (+ `DROPHOUND_DIGEST_MODEL`) | Claude-written digests |

### Going live, concretely

- **Real monitoring:** swap `SampleMonitor()` for `HttpMonitor(settings)` in
  `engine/pipeline.run_cycle` (or pass it in). Each product's `product_url` +
  `in_stock_signal` already drive it.
- **Stripe:** in `web/app.py:upgrade`, create a Checkout Session and redirect to
  it instead of the local dry-run tier flip.
- **Per-subscriber DMs:** `pipeline` already matches premium subscribers'
  filters and logs the match — extend it to DM each matched user's Telegram.

---

## Real store monitoring

Different stores need different approaches:

### Shopify stores (free, built-in)
Many designer-toy retailers run on Shopify, which publishes live stock at a
`<product-url>.js` endpoint. Add any such product and monitor it for real:

```bash
python -m drophound add-product "https://strangecattoys.com/products/<handle>"
python -m drophound run --source http --loop      # reads real stock, alerts on changes
```

Confirmed-working stores include strangecattoys.com, mindzai.com, toytokyo.com,
kidrobot.com, tenacioustoys.com. `toytokyo.com` even carries Pop Mart / Labubu.

### Pop Mart (free, local browser watcher)
Pop Mart's site is a JavaScript app whose stock lives in a *signed* backend API,
so plain HTTP can't read it. `tools/watch_popmart.py` drives a real headless
browser, captures the page's own product API, reads the authoritative per-SKU
`stockFlag`, and pings the `/hook/restock` webhook when an item flips in-stock.

```bash
pip install -r requirements-watch.txt && python -m playwright install chromium
python -m drophound serve                       # terminal 1: receives the webhook
python tools/watch_popmart.py --loop --interval 120 \
    "https://www.popmart.com/us/products/<id>/<name>"   # terminal 2
```

### Inbound webhook (anything else)
`POST /hook/restock` turns any external signal — a no-code page watcher
(Distill/Visualping), Zapier, n8n, a cron script — into a real fan-out alert.
Body: a known `sku`, or at least a `name` (+ optional price/retailer/region/url).
Protect it with `DROPHOUND_HOOK_SECRET` once your server is internet-reachable.

---

## Project layout

```
drophound/
  drophound/
    config.py        env-driven settings (+ .env loader)
    db.py            sqlite schema + helpers
    seed.py          demo catalog, restock history, resale, demo user
    stats.py         resale math (median/low/high/trend/multiple)
    patterns.py      restock cadence + next-window prediction
    filters.py       premium alert filter matching
    affiliates.py    outbound affiliate URL building
    engine/
      monitors.py    SampleMonitor (offline) + HttpMonitor (real)
      resale.py      eBay sold-listings + fixture fallback
      digest.py      AI/template digest writer
      alerts.py      Telegram / Discord / email dispatchers
      pipeline.py    run_cycle(): observe→record→refresh→alert→log
    web/
      app.py         Starlette routes + JSON API
      templates/     landing, drops, dashboard, collection, pricing, digest
      static/        styles.css, app.js
    fixtures/        catalog.json, ebay_sold.json
    cli.py           command line (incl. add-product, test-alert, run --source)
  tools/
    watch_popmart.py local headless-browser Pop Mart watcher -> webhook
  tests/             pytest suite (logic + resale + pipeline + API + monitors)
```

## Tech

Pure-Python, no compiled dependencies (installs clean on Python 3.10–3.14):
**Starlette** + **Jinja2** + **uvicorn** + **httpx**, stdlib **sqlite3**. Tests
use Starlette's `TestClient`.

## Tests

```bash
pytest            # ~25 tests: stats, patterns, filters, affiliates, resale, pipeline, API
```

---

*Demo build. Resale numbers in the seed are illustrative; wire `EBAY_APP_ID` for
real sold-listing data. Always respect each retailer's robots.txt and ToS when
enabling the live `HttpMonitor`.*
