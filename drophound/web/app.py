"""DropHound web application (Starlette).

Serves the three product layers:
  Layer 1  /            landing + email capture, /drops public feed
  Layer 2  /app         premium dashboard, /collection P/L tracker, /pricing
  Layer 3  /go/{id}     affiliate redirect (logs the click)
Plus a small JSON API under /api and the AI digest at /digest.
"""

from __future__ import annotations

import hmac
import html
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .. import affiliates, billing, cache, db, filters, firebase_db
from ..config import STATIC_DIR, TEMPLATES_DIR, get_settings
from ..engine import digest as digest_mod
from ..engine import pipeline
from ..engine import resale
from ..engine.alerts import AlertMessage, broadcast_dispatchers
from ..middleware import (  # noqa: F401 — imported for Middleware() registration
    LoggingMiddleware, RateLimitMiddleware, SessionMiddleware, get_session_id,
)
from ..security import (
    CSRF_FIELD, CSRF_HEADER, SecurityHeadersMiddleware,
    clear_failures, csrf_token, is_bot_submission, is_locked,
    record_failure, verify_csrf,
)
from ..stats import premium_multiple, trend_pct
from ..util import humanize_age, money, now_utc, parse_iso
from ..validation import (
    hash_password, sanitize_str, validate_email, validate_password, verify_password,
)

logger = logging.getLogger("drophound")

DEMO_EMAIL = "demo@drophound.app"


def _csrf(request: Request) -> str:
    """Return the CSRF token for the current session."""
    return csrf_token(get_session_id(request), get_settings().csrf_secret)


def _bad_csrf(request: Request, form) -> bool:
    """Return True if the CSRF token in the submitted form/header is invalid."""
    token = form.get(CSRF_FIELD) or request.headers.get(CSRF_HEADER, "")
    return not verify_csrf(token, get_session_id(request), get_settings().csrf_secret)


def _client_ip(request: Request) -> str:
    return (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["money"] = lambda v: money(v)
templates.env.filters["pct"] = lambda v: ("—" if v is None else f"{'+' if v >= 0 else ''}{v}%")

BADGES = {
    "drop": ("DROP", "b-drop"),
    "restock": ("RESTOCK", "b-restock"),
    "price_drop": ("PRICE DROP", "b-price"),
    "sold_out": ("SOLD OUT", "b-sold"),
    "low_stock": ("LOW STOCK", "b-low"),
}


# --------------------------------------------------------------------------- #
# Connection + small presentation helpers
# --------------------------------------------------------------------------- #
def open_conn() -> sqlite3.Connection:
    return db.connect(get_settings().db_path)


def initials(character: str) -> str:
    words = (character or "").split()
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    word = words[0] if words else "?"
    return (word[:2]).title()


def event_view(conn: sqlite3.Connection, row: sqlite3.Row, now) -> dict:
    res = resale.latest(conn, row["product_id"])
    median = res["median"] if res else None
    badge, badge_class = BADGES.get(row["event_type"], (row["event_type"].upper(), "b-low"))
    return {
        "id": row["id"],
        "product_id": row["product_id"],
        "name": row["name"],
        "brand": row["brand"],
        "character": row["character"],
        "retailer": row["retailer"],
        "region": row["region"],
        "event_type": row["event_type"],
        "badge": badge,
        "badge_class": badge_class,
        "price": row["price"] if row["price"] is not None else row["retail_price"],
        "resale_median": median,
        "resale_low": res["low"] if res else None,
        "resale_high": res["high"] if res else None,
        "multiple": premium_multiple(row["retail_price"], median),
        "age": humanize_age(parse_iso(row["detected_at"]), now=now),
        "color": row["image_hint"] or "#888",
        "initials": initials(row["character"]),
        "buy_target": "site",
    }


def recent_event_rows(conn: sqlite3.Connection, *, types: tuple[str, ...] | None = None,
                      limit: int = 40, since_hours: int | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT e.*, p.name, p.brand, p.character, p.region, p.retailer, "
           "p.retail_price, p.image_hint, p.product_url "
           "FROM restock_events e JOIN products p ON p.id = e.product_id")
    clauses, params = [], []
    if types:
        clauses.append("e.event_type IN (%s)" % ",".join("?" * len(types)))
        params.extend(types)
    if since_hours is not None:
        clauses.append("e.detected_at >= ?")
        params.append((now_utc() - timedelta(hours=since_hours)).isoformat())
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY e.detected_at DESC, e.id DESC LIMIT ?"
    params.append(limit)
    return db.q(conn, sql, params)


def get_subscriber(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    return db.one(conn, "SELECT * FROM subscribers WHERE email = ?", (email,))


def collection_summary(conn: sqlite3.Connection, subscriber_id: int) -> dict:
    rows = db.q(
        conn,
        """SELECT c.*, p.name, p.character, p.brand, p.image_hint, p.retail_price
           FROM collection_items c JOIN products p ON p.id = c.product_id
           WHERE c.subscriber_id = ? ORDER BY c.id""",
        (subscriber_id,),
    )
    items, cost_total, value_total = [], 0.0, 0.0
    for r in rows:
        res = resale.latest(conn, r["product_id"])
        median = res["median"] if res else None
        cost = (r["paid_price"] or 0) * r["qty"]
        value = (median or 0) * r["qty"]
        cost_total += cost
        value_total += value
        items.append({
            "name": r["name"],
            "character": r["character"],
            "qty": r["qty"],
            "condition": r["condition"],
            "paid_price": r["paid_price"],
            "median": median,
            "cost": cost,
            "value": value,
            "gain": value - cost,
            "gain_pct": trend_pct(cost, value) if cost else None,
            "color": r["image_hint"] or "#888",
            "initials": initials(r["character"]),
        })
    gain = value_total - cost_total
    return {
        "items": items,
        "cost_total": round(cost_total, 2),
        "value_total": round(value_total, 2),
        "gain": round(gain, 2),
        "gain_pct": trend_pct(cost_total, value_total) if cost_total else None,
    }


def md_lite(text: str) -> str:
    """Tiny, safe markdown -> HTML for digest bodies (headings, bullets, bold)."""
    def fmt(s: str) -> str:
        s = html.escape(s)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

    out, in_list = [], False

    def close():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in text.split("\n"):
        line = raw.rstrip()
        if line.startswith("## "):
            close(); out.append(f"<h3>{fmt(line[3:])}</h3>")
        elif line.startswith("# "):
            close(); out.append(f"<h2>{fmt(line[2:])}</h2>")
        elif line.startswith("- "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{fmt(line[2:])}</li>")
        elif line == "":
            close()
        else:
            close(); out.append(f"<p>{fmt(line)}</p>")
    close()
    return "\n".join(out)


def site_context(conn: sqlite3.Connection, request: Request | None = None) -> dict:
    s = get_settings()
    ctx: dict = {
        "base_url": s.base_url.rstrip("/"),
        "premium_price": s.premium_price,
        "tracked": db.one(conn, "SELECT COUNT(*) c FROM products")["c"],
        "alerts_24h": db.one(
            conn,
            "SELECT COUNT(*) c FROM restock_events WHERE detected_at >= ?",
            ((now_utc() - timedelta(hours=24)).isoformat(),),
        )["c"],
        "subscribers": db.one(conn, "SELECT COUNT(*) c FROM subscribers")["c"],
    }
    if request is not None:
        ctx["current_user"] = get_current_user(conn, request)
        ctx["csrf_token"] = _csrf(request)
    return ctx


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
async def landing(request: Request):
    conn = open_conn()
    try:
        now = now_utc()
        rows = recent_event_rows(conn, types=("drop", "restock", "price_drop"), limit=6)
        drops = [event_view(conn, r, now) for r in rows]
        ctx = {
            "drops": drops,
            "site": site_context(conn, request),
            "subscribed": request.query_params.get("subscribed") == "1",
            "error": request.query_params.get("error") == "1",
        }
        return templates.TemplateResponse(request, "landing.html", ctx)
    finally:
        conn.close()


async def subscribe(request: Request):
    form = await request.form()
    if is_bot_submission(form) or _bad_csrf(request, form):
        return RedirectResponse("/?subscribed=1#join", status_code=303)  # silent drop
    telegram = sanitize_str(form.get("telegram") or "", 64) or None
    try:
        email = validate_email(form.get("email") or "")
    except ValueError:
        return RedirectResponse("/?error=1#join", status_code=303)
    conn = open_conn()
    try:
        existing = get_subscriber(conn, email)
        if not existing:
            db.execute(
                conn,
                """INSERT INTO subscribers (email, tier, telegram, created_at)
                   VALUES (?, 'free', ?, ?)""",
                (email, telegram, now_utc().isoformat()),
            )
            firebase_db.upsert_user(email, {"email": email, "tier": "free",
                                            "created_at": now_utc().isoformat()})
        return RedirectResponse("/?subscribed=1#join", status_code=303)
    finally:
        conn.close()


async def drops_page(request: Request):
    conn = open_conn()
    try:
        now = now_utc()
        rows = recent_event_rows(conn, limit=50)
        events = [event_view(conn, r, now) for r in rows]
        return templates.TemplateResponse(
            request, "drops.html",
            {"events": events, "site": site_context(conn, request)},
        )
    finally:
        conn.close()


async def dashboard(request: Request):
    conn = open_conn()
    try:
        sub = get_current_user(conn, request)
        if not sub:
            return RedirectResponse("/login?next=/app", status_code=303)
        now = now_utc()
        rows = recent_event_rows(conn, types=("drop", "restock", "price_drop"),
                                 limit=60, since_hours=24 * 14)
        matched = [event_view(conn, r, now) for r in rows
                   if filters.matches(sub, r, price=r["price"])][:10]
        movers = digest_mod._top_movers(conn, limit=5)
        upcoming = digest_mod._upcoming(conn, limit=6)
        ctx = {
            "sub": sub,
            "filter_label": filters.describe(sub),
            "matched": matched,
            "movers": movers,
            "upcoming": upcoming,
            "site": site_context(conn, request),
        }
        return templates.TemplateResponse(request, "dashboard.html", ctx)
    finally:
        conn.close()


async def collection_page(request: Request):
    conn = open_conn()
    try:
        sub = get_current_user(conn, request)
        if not sub:
            return RedirectResponse("/login?next=/collection", status_code=303)
        summary = collection_summary(conn, sub["id"])
        return templates.TemplateResponse(
            request, "collection.html",
            {"sub": sub, "summary": summary, "site": site_context(conn, request)},
        )
    finally:
        conn.close()


async def pricing(request: Request):
    conn = open_conn()
    try:
        sub = get_current_user(conn, request)
        return templates.TemplateResponse(
            request, "pricing.html",
            {"site": site_context(conn, request),
             "stripe_enabled": get_settings().has_stripe,
             "upgraded": request.query_params.get("upgraded") == "1",
             "error": request.query_params.get("error") == "1",
             "sub": sub},
        )
    finally:
        conn.close()


async def upgrade(request: Request):
    form = await request.form()
    if _bad_csrf(request, form):
        return RedirectResponse("/pricing?error=1", status_code=303)
    settings = get_settings()
    conn = open_conn()
    try:
        sub = get_current_user(conn, request)
        if not sub:
            return RedirectResponse("/login?next=/pricing", status_code=303)
        email = sub["email"]

        # Real payments: hand the buyer to Stripe's hosted checkout.
        if settings.has_stripe:
            try:
                url, _ = billing.create_checkout_session(settings, email)
                if url:
                    firebase_db.log_event(email, "checkout_started", ip=_client_ip(request))
                    return RedirectResponse(url, status_code=303)
            except Exception:
                pass
            return RedirectResponse("/pricing?error=1", status_code=303)

        # No Stripe configured -> demo flip so the flow is still walkable.
        db.execute(conn,
            "UPDATE subscribers SET tier='premium', premium_since=? WHERE id=?",
            (now_utc().isoformat(), sub["id"]))
        firebase_db.upsert_user(email, {"tier": "premium",
                                        "premium_since": now_utc().isoformat()})
        return RedirectResponse("/pricing?upgraded=1", status_code=303)
    finally:
        conn.close()


async def upgrade_success(request: Request):
    conn = open_conn()
    try:
        return templates.TemplateResponse(
            request, "upgrade_success.html", {"site": site_context(conn, request)})
    finally:
        conn.close()


async def stripe_webhook(request: Request):
    settings = get_settings()
    payload = await request.body()
    event = billing.verify_webhook(
        payload, request.headers.get("stripe-signature", ""),
        settings.stripe_webhook_secret or "")
    if event is None:
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    conn = open_conn()
    try:
        if etype == "checkout.session.completed":
            email = (obj.get("client_reference_id") or obj.get("customer_email") or "").lower()
            if email and "@" in email:
                sub = get_or_create_subscriber(conn, email)
                if sub:
                    db.execute(
                        conn,
                        """UPDATE subscribers SET tier='premium', premium_since=?,
                           stripe_customer_id=? WHERE id=?""",
                        (now_utc().isoformat(), obj.get("customer"), sub["id"]),
                    )
                    firebase_db.upsert_user(email, {
                        "tier": "premium",
                        "premium_since": now_utc().isoformat(),
                        "stripe_customer_id": obj.get("customer"),
                    })
                    firebase_db.log_event(email, "payment_success",
                                          detail=f"stripe_session={obj.get('id')}")
        elif etype == "customer.subscription.deleted":
            customer = obj.get("customer")
            if customer:
                db.execute(conn, "UPDATE subscribers SET tier='free' WHERE stripe_customer_id=?",
                           (customer,))
                sub_row = db.one(conn, "SELECT email FROM subscribers WHERE stripe_customer_id=?",
                                 (customer,))
                if sub_row:
                    firebase_db.upsert_user(sub_row["email"], {"tier": "free"})
                    firebase_db.log_event(sub_row["email"], "subscription_cancelled")
        return JSONResponse({"received": True})
    finally:
        conn.close()


async def digest_page(request: Request):
    period = request.query_params.get("period", "daily")
    if period not in ("daily", "weekly"):
        period = "daily"
    settings = get_settings()
    conn = open_conn()
    try:
        d = digest_mod.build_digest(conn, settings, period)
        return templates.TemplateResponse(
            request, "digest.html",
            {"digest": d, "body_html": md_lite(d["body"]), "period": period,
             "site": site_context(conn, request)},
        )
    finally:
        conn.close()


async def go_redirect(request: Request):
    product_id = int(request.path_params["product_id"])
    target = request.query_params.get("to", "ebay")
    settings = get_settings()
    conn = open_conn()
    try:
        product = db.one(conn, "SELECT * FROM products WHERE id = ?", (product_id,))
        if not product:
            return JSONResponse({"error": "unknown product"}, status_code=404)
        db.execute(
            conn,
            "INSERT INTO affiliate_clicks (product_id, target, created_at) VALUES (?,?,?)",
            (product_id, target, now_utc().isoformat()),
        )
        url = affiliates.build_url(settings, product, target)
        return RedirectResponse(url, status_code=302)
    finally:
        conn.close()


# ---- Inbound webhook: real monitoring -> real alert ----------------------- #
async def hook_restock(request: Request):
    """Receive a restock signal from any source (a no-code page watcher like
    Distill/Visualping, Zapier, n8n, a Shopify webhook, a cron script) and fan it
    out to Telegram + Discord + email. Protected by an optional shared secret.

    Body (JSON or form): either a known `sku`, or at least a `name`. Optional:
    price, retailer, region, url, event_type (restock|drop|price_drop).
    """
    settings = get_settings()
    provided = request.headers.get("x-drophound-secret") or request.query_params.get("secret")
    if settings.hook_secret and provided != settings.hook_secret:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    payload: dict = {}
    try:
        payload = await request.json()
    except Exception:
        try:
            payload = dict(await request.form())
        except Exception:
            payload = {}

    sku = str(payload.get("sku") or "").strip()
    name = str(payload.get("name") or "").strip()
    event_type = str(payload.get("event_type") or "restock").strip() or "restock"
    raw_price = payload.get("price")
    try:
        price = float(raw_price) if raw_price not in (None, "") else None
    except (TypeError, ValueError):
        price = None

    conn = open_conn()
    try:
        product = db.one(conn, "SELECT * FROM products WHERE sku = ?", (sku,)) if sku else None
        if not product and not name:
            return JSONResponse(
                {"error": "provide a known 'sku' or at least a 'name'"}, status_code=400)

        if product:
            pdict = dict(product)
            cur = db.execute(
                conn,
                """INSERT INTO restock_events
                   (product_id, event_type, status, price, note, source, detected_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (pdict["id"], event_type, "in_stock",
                 price if price is not None else pdict.get("retail_price"),
                 "via webhook", "webhook", now_utc().isoformat()),
            )
            event_id = cur.lastrowid
            msg = pipeline._build_alert(conn, settings, event_id, event_type, pdict, price)
            label = pdict["name"]
        else:
            event_id = None
            retailer = str(payload.get("retailer") or "")
            region = str(payload.get("region") or "")
            where = (f" at {retailer}" if retailer else "") + (f" ({region})" if region else "")
            verb = event_type.replace("_", " ").upper()
            text = f"🔔 {verb}: {name} — {money(price)}{where}.".strip()
            msg = AlertMessage(title=f"{verb}: {name}", text=text, url=payload.get("url"))
            label = name

        results = []
        for d in broadcast_dispatchers(settings):
            r = d.send(msg)
            db.execute(
                conn,
                """INSERT INTO alerts_log (event_id, subscriber_id, channel, status, detail, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (event_id, None, r.channel, r.status, r.detail, now_utc().isoformat()),
            )
            results.append({"channel": r.channel, "status": r.status})
        return JSONResponse({"status": "ok", "product": label,
                             "event_type": event_type, "channels": results})
    finally:
        conn.close()


# ---- SEO: robots + sitemap ------------------------------------------------ #
async def robots_txt(request: Request):
    base = get_settings().base_url.rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /go/\n"
        "Disallow: /hook\n"
        "Disallow: /watch/add\n"
        "Disallow: /watch/remove\n"
        "Disallow: /subscribe\n"
        "Disallow: /upgrade\n"
        "Disallow: /admin\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return PlainTextResponse(body)


async def sitemap_xml(request: Request):
    base = get_settings().base_url.rstrip("/")
    pages = [("/", "hourly", "1.0"), ("/watch", "hourly", "0.9"),
             ("/drops", "hourly", "0.8"), ("/pricing", "weekly", "0.6"),
             ("/digest", "daily", "0.5")]
    urls = "".join(
        f"<url><loc>{base}{p}</loc><changefreq>{cf}</changefreq>"
        f"<priority>{pr}</priority></url>"
        for p, cf, pr in pages
    )
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"{urls}</urlset>")
    return Response(xml, media_type="application/xml")


# ---- JSON API ------------------------------------------------------------- #
async def api_health(request: Request):
    return JSONResponse({"status": "ok", "service": "drophound"})


async def api_drops(request: Request):
    conn = open_conn()
    try:
        now = now_utc()
        rows = recent_event_rows(conn, limit=int(request.query_params.get("limit", 25)))
        return JSONResponse({"drops": [
            {k: v for k, v in event_view(conn, r, now).items() if k != "color"}
            for r in rows
        ]})
    finally:
        conn.close()


async def api_products(request: Request):
    conn = open_conn()
    try:
        rows = db.q(conn, "SELECT * FROM products ORDER BY brand, character, name")
        return JSONResponse({"products": [dict(r) for r in rows]})
    finally:
        conn.close()


async def api_collection_value(request: Request):
    subscriber_id = int(request.path_params["subscriber_id"])
    conn = open_conn()
    try:
        sub = db.one(conn, "SELECT * FROM subscribers WHERE id = ?", (subscriber_id,))
        if not sub:
            return JSONResponse({"error": "unknown subscriber"}, status_code=404)
        return JSONResponse(collection_summary(conn, subscriber_id))
    finally:
        conn.close()


# ---- Catalog browse + per-product watchlist ------------------------------- #
POPULAR_CHARACTERS = ["Labubu", "Molly", "Skullpanda", "Crybaby", "Hirono",
                      "Dimoo", "Smiski", "Sonny Angel", "tokidoki", "Dunny"]
PAGE_SIZE = 24
_STATUS_SUB = ("(SELECT status FROM restock_events e WHERE e.product_id=p.id "
               "ORDER BY e.detected_at DESC, e.id DESC LIMIT 1)")
_RESALE_SUB  = ("(SELECT median FROM resale_prices r WHERE r.product_id=p.id "
                "ORDER BY r.captured_at DESC, r.id DESC LIMIT 1)")
_RESALE_LOW  = ("(SELECT low    FROM resale_prices r WHERE r.product_id=p.id "
                "ORDER BY r.captured_at DESC, r.id DESC LIMIT 1)")
_RESALE_HIGH = ("(SELECT high   FROM resale_prices r WHERE r.product_id=p.id "
                "ORDER BY r.captured_at DESC, r.id DESC LIMIT 1)")


def get_current_user(conn: sqlite3.Connection, request: Request) -> sqlite3.Row | None:
    """Return the logged-in subscriber for this browser session, or None."""
    sid = get_session_id(request)
    return db.one(conn, "SELECT * FROM subscribers WHERE session_id = ?", (sid,))


def get_or_create_subscriber(conn: sqlite3.Connection, email: str,
                             session_id: str | None = None) -> sqlite3.Row | None:
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        return None
    row = get_subscriber(conn, email)
    if row:
        # Stamp session_id on first use if not set
        if session_id and not (row["session_id"] if "session_id" in row.keys() else None):
            db.execute(conn, "UPDATE subscribers SET session_id=? WHERE id=?",
                       (session_id, row["id"]))
        return get_subscriber(conn, email)
    db.execute(conn,
               "INSERT INTO subscribers (email, tier, created_at, session_id) VALUES (?, 'free', ?, ?)",
               (email, now_utc().isoformat(), session_id))
    return get_subscriber(conn, email)


def _product_item(row: sqlite3.Row, watched: set[int]) -> dict:
    return {
        "id": row["id"], "name": row["name"], "brand": row["brand"],
        "character": row["character"], "retailer": row["retailer"],
        "region": row["region"], "price": row["retail_price"],
        "resale_median": row["resale_median"],
        "resale_low": row["resale_low"] if "resale_low" in row.keys() else None,
        "resale_high": row["resale_high"] if "resale_high" in row.keys() else None,
        "status": row["status"] or "unknown",
        "color": row["image_hint"] or "#7c6cff", "initials": initials(row["character"]),
        "watched": row["id"] in watched,
    }


def catalog_page(conn, q, character, in_stock_only, page, watched):
    where, params = [], []
    if q:
        where.append("(p.name LIKE ? OR p.character LIKE ? OR p.brand LIKE ?)")
        params += [f"%{q}%"] * 3
    if character:
        where.append("p.character LIKE ?")
        params.append(f"%{character}%")
    if in_stock_only:
        where.append(f"{_STATUS_SUB} = 'in_stock'")
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.one(conn, f"SELECT COUNT(*) c FROM products p{wsql}", params)["c"]
    rows = db.q(conn, f"""SELECT p.id, p.name, p.brand, p.character, p.retailer, p.region,
        p.retail_price, p.image_hint, {_STATUS_SUB} AS status, {_RESALE_SUB} AS resale_median,
        {_RESALE_LOW} AS resale_low, {_RESALE_HIGH} AS resale_high
        FROM products p{wsql} ORDER BY ({_STATUS_SUB}='in_stock') DESC, p.id
        LIMIT ? OFFSET ?""", params + [PAGE_SIZE, (page - 1) * PAGE_SIZE])
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return [_product_item(r, watched) for r in rows], total, pages


def _watched_ids(conn, email):
    sub = get_subscriber(conn, email) if email and "@" in email else None
    if not sub:
        return set(), None
    ids = {r["product_id"] for r in
           db.q(conn, "SELECT product_id FROM watchlist WHERE subscriber_id=?", (sub["id"],))}
    return ids, sub


async def register_page(request: Request):
    conn = open_conn()
    try:
        if request.method == "POST":
            form = await request.form()
            if is_bot_submission(form) or _bad_csrf(request, form):
                return RedirectResponse("/register?next=" +
                    sanitize_str(request.query_params.get("next") or "/watch", 200),
                    status_code=303)
            try:
                email = validate_email(form.get("email") or "")
            except ValueError as exc:
                return templates.TemplateResponse(request, "login.html",
                    {"site": site_context(conn, request), "error": str(exc), "tab": "register"},
                    status_code=400)
            try:
                password = validate_password(form.get("password") or "")
            except ValueError as exc:
                return templates.TemplateResponse(request, "login.html",
                    {"site": site_context(conn, request), "error": str(exc), "tab": "register"},
                    status_code=400)
            existing = get_subscriber(conn, email)
            if existing and existing["password_hash"]:
                return templates.TemplateResponse(request, "login.html",
                    {"site": site_context(conn, request),
                     "error": "An account with that email already exists. Log in instead.",
                     "tab": "register"}, status_code=400)
            sid = get_session_id(request)
            pw_hash = hash_password(password)
            if existing:
                db.execute(conn,
                    "UPDATE subscribers SET password_hash=?, session_id=? WHERE id=?",
                    (pw_hash, sid, existing["id"]))
            else:
                db.execute(conn,
                    """INSERT INTO subscribers (email, tier, created_at, session_id, password_hash)
                       VALUES (?, 'free', ?, ?, ?)""",
                    (email, now_utc().isoformat(), sid, pw_hash))
            firebase_db.upsert_user(email, {"email": email, "tier": "free",
                                            "created_at": now_utc().isoformat()})
            firebase_db.log_event(email, "register", ip=_client_ip(request),
                                  user_agent=request.headers.get("user-agent"))
            next_url = sanitize_str(request.query_params.get("next") or "/watch", 200)
            if not next_url.startswith("/"):
                next_url = "/watch"
            return RedirectResponse(next_url, status_code=303)
        return templates.TemplateResponse(request, "login.html",
            {"site": site_context(conn, request), "tab": "register"})
    finally:
        conn.close()


async def login_page(request: Request):
    conn = open_conn()
    try:
        if request.method == "POST":
            form = await request.form()
            if _bad_csrf(request, form):
                return templates.TemplateResponse(request, "login.html",
                    {"site": site_context(conn, request),
                     "error": "Session expired. Please try again.", "tab": "login"},
                    status_code=403)
            try:
                email = validate_email(form.get("email") or "")
            except ValueError as exc:
                return templates.TemplateResponse(request, "login.html",
                    {"site": site_context(conn, request), "error": str(exc), "tab": "login"},
                    status_code=400)

            if is_locked(email):
                firebase_db.log_event(email, "login_blocked", ip=_client_ip(request))
                return templates.TemplateResponse(request, "login.html",
                    {"site": site_context(conn, request),
                     "error": "Too many failed attempts. Try again in 15 minutes.",
                     "tab": "login"}, status_code=429)

            password = form.get("password") or ""
            sub = get_subscriber(conn, email)
            pw_hash = (sub["password_hash"] if sub and "password_hash" in sub.keys() else None)
            if not sub or not pw_hash or not verify_password(password, pw_hash):
                record_failure(email)
                firebase_db.log_event(email, "login_failed", ip=_client_ip(request))
                return templates.TemplateResponse(request, "login.html",
                    {"site": site_context(conn, request),
                     "error": "Incorrect email or password.", "tab": "login"}, status_code=400)

            clear_failures(email)
            sid = get_session_id(request)
            db.execute(conn, "UPDATE subscribers SET session_id=? WHERE id=?", (sid, sub["id"]))
            firebase_db.upsert_user(email, {
                "email": email,
                "tier": sub["tier"],
                "last_login": now_utc().isoformat(),
            })
            firebase_db.log_event(email, "login_success", ip=_client_ip(request))
            next_url = sanitize_str(request.query_params.get("next") or "/watch", 200)
            if not next_url.startswith("/"):
                next_url = "/watch"
            return RedirectResponse(next_url, status_code=303)
        return templates.TemplateResponse(request, "login.html",
            {"site": site_context(conn, request), "tab": "login"})
    finally:
        conn.close()


async def logout(request: Request):
    form = await request.form()
    if _bad_csrf(request, form):
        return RedirectResponse("/", status_code=303)
    conn = open_conn()
    try:
        sid = get_session_id(request)
        sub = db.one(conn, "SELECT * FROM subscribers WHERE session_id=?", (sid,))
        if sub:
            db.execute(conn, "UPDATE subscribers SET session_id=NULL WHERE id=?", (sub["id"],))
            firebase_db.log_event(sub["email"], "logout", ip=_client_ip(request))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("dh_sid")
        return response
    finally:
        conn.close()


async def watch_page(request: Request):
    conn = open_conn()
    try:
        return templates.TemplateResponse(
            request, "watch.html",
            {"site": site_context(conn, request), "popular": POPULAR_CHARACTERS})
    finally:
        conn.close()


async def api_catalog(request: Request):
    qp = request.query_params
    q = sanitize_str(qp.get("q", ""), 100)
    character = sanitize_str(qp.get("character", ""), 100)
    in_stock = qp.get("in_stock") == "1"
    try:
        page = max(1, int(qp.get("page", "1")))
    except ValueError:
        page = 1

    # Cache unfiltered catalog for 30s; skip cache for logged-in users (watched state varies)
    cache_key = f"catalog:{q}:{character}:{in_stock}:{page}"
    conn = open_conn()
    try:
        sub = get_current_user(conn, request)
        logged_in = sub is not None

        hit = cache.get(cache_key) if not logged_in else None
        if hit:
            return JSONResponse({**hit, "logged_in": False, "watch_count": 0})

        watched = {r["product_id"] for r in
                   db.q(conn, "SELECT product_id FROM watchlist WHERE subscriber_id=?",
                        (sub["id"],))} if sub else set()
        items, total, pages = catalog_page(conn, q, character, in_stock, page, watched)
        payload = {
            "products": items, "page": page, "pages": pages, "total": total,
            "watch_count": len(watched), "logged_in": logged_in,
        }
        if not logged_in:
            cache.set(cache_key, payload, ttl=30)
        return JSONResponse(payload)
    finally:
        conn.close()


async def api_watchlist(request: Request):
    conn = open_conn()
    try:
        sub = get_current_user(conn, request)
        if not sub:
            return JSONResponse({"error": "Authentication required.", "login_required": True},
                                status_code=401)
        rows = db.q(conn, f"""SELECT p.id, p.name, p.brand, p.character, p.retailer, p.region,
            p.retail_price, p.image_hint, {_STATUS_SUB} AS status, {_RESALE_SUB} AS resale_median,
            {_RESALE_LOW} AS resale_low, {_RESALE_HIGH} AS resale_high
            FROM watchlist w JOIN products p ON p.id = w.product_id
            WHERE w.subscriber_id = ? ORDER BY w.created_at DESC""", (sub["id"],))
        items = [_product_item(r, {r["id"] for r in rows}) for r in rows]
        return JSONResponse({"products": items, "count": len(items)})
    finally:
        conn.close()


async def watch_add(request: Request):
    form = await request.form()
    if _bad_csrf(request, form):
        return JSONResponse({"error": "Invalid CSRF token."}, status_code=403)
    try:
        pid = int(form.get("product_id") or 0)
        if pid <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid product ID."}, status_code=400)

    conn = open_conn()
    try:
        sub = get_current_user(conn, request)
        if not sub:
            return JSONResponse({"error": "Log in to watch products.", "login_required": True},
                                status_code=401)
        if not db.one(conn, "SELECT 1 FROM products WHERE id = ?", (pid,)):
            return JSONResponse({"error": "Product not found."}, status_code=404)
        try:
            db.execute(conn, """INSERT INTO watchlist (subscriber_id, product_id, created_at)
                       VALUES (?,?,?)""", (sub["id"], pid, now_utc().isoformat()))
        except sqlite3.IntegrityError:
            pass  # already watching
        count = db.one(conn, "SELECT COUNT(*) c FROM watchlist WHERE subscriber_id=?",
                       (sub["id"],))["c"]
        cache.invalidate(f"watchlist:{sub['id']}")
        return JSONResponse({"watched": True, "count": count})
    finally:
        conn.close()


async def watch_remove(request: Request):
    form = await request.form()
    if _bad_csrf(request, form):
        return JSONResponse({"error": "Invalid CSRF token."}, status_code=403)
    try:
        pid = int(form.get("product_id") or 0)
        if pid <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return JSONResponse({"error": "Invalid product ID."}, status_code=400)

    conn = open_conn()
    try:
        sub = get_current_user(conn, request)
        if not sub:
            return JSONResponse({"error": "Log in to watch products.", "login_required": True},
                                status_code=401)
        db.execute(conn, "DELETE FROM watchlist WHERE subscriber_id=? AND product_id=?",
                   (sub["id"], pid))
        count = db.one(conn, "SELECT COUNT(*) c FROM watchlist WHERE subscriber_id=?",
                       (sub["id"],))["c"]
        cache.invalidate(f"watchlist:{sub['id']}")
        return JSONResponse({"watched": False, "count": count})
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Error handlers
# --------------------------------------------------------------------------- #
async def _not_found(request: Request, exc: Exception) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Not found."}, status_code=404)
    conn = open_conn()
    try:
        return templates.TemplateResponse(
            request, "error.html",
            {"site": site_context(conn, request), "code": 404,
             "message": "We couldn't find that page.",
             "detail": "The URL might have changed or the page may have been removed."},
            status_code=404)
    finally:
        conn.close()


async def _server_error(request: Request, exc: Exception) -> Response:
    logger.exception("unhandled error %s %s", request.method, request.url.path)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "An unexpected error occurred."}, status_code=500)
    conn = open_conn()
    try:
        return templates.TemplateResponse(
            request, "error.html",
            {"site": site_context(conn, request), "code": 500,
             "message": "Something went wrong on our end.",
             "detail": "We've logged the error. Please try again in a moment."},
            status_code=500)
    finally:
        conn.close()


def _admin_check(request: Request) -> str | None:
    """Return the admin key from query/form, or None if missing/wrong."""
    settings = get_settings()
    if not settings.admin_secret:
        return None
    key = request.query_params.get("key") or ""
    if not hmac.compare_digest(key, settings.admin_secret):
        return None
    return key


async def admin_page(request: Request):
    key = _admin_check(request)
    if not key:
        return PlainTextResponse("Forbidden", status_code=403)
    conn = open_conn()
    try:
        users_raw = db.q(conn, """
            SELECT s.*, (SELECT COUNT(*) FROM watchlist w WHERE w.subscriber_id=s.id) AS watch_count
            FROM subscribers s ORDER BY s.created_at DESC
        """)
        users = [dict(u) for u in users_raw]
        total = len(users)
        premium = sum(1 for u in users if u["tier"] == "premium")
        return templates.TemplateResponse(request, "admin.html", {
            "users": users, "total": total, "premium": premium,
            "free": total - premium, "key": key,
            "csrf_token": _csrf(request),
            "message": request.query_params.get("msg"),
        })
    finally:
        conn.close()


async def admin_delete_user(request: Request):
    form = await request.form()
    key = form.get("key") or ""
    settings = get_settings()
    if not settings.admin_secret or not hmac.compare_digest(key, settings.admin_secret):
        return PlainTextResponse("Forbidden", status_code=403)
    if _bad_csrf(request, form):
        return PlainTextResponse("Invalid CSRF token", status_code=403)
    email = sanitize_str(form.get("email") or "", 320).lower()
    if not email or "@" not in email:
        return RedirectResponse(f"/admin?key={key}&msg=Invalid+email", status_code=303)
    conn = open_conn()
    try:
        sub = get_subscriber(conn, email)
        if not sub:
            return RedirectResponse(f"/admin?key={key}&msg=User+not+found", status_code=303)
        db.execute(conn, "DELETE FROM watchlist WHERE subscriber_id=?", (sub["id"],))
        db.execute(conn, "DELETE FROM collection_items WHERE subscriber_id=?", (sub["id"],))
        db.execute(conn, "DELETE FROM subscribers WHERE id=?", (sub["id"],))
        firebase_db.log_event(email, "admin_deleted", detail=f"by_admin ip={_client_ip(request)}")
        firebase_db.delete_user(email)
        return RedirectResponse(f"/admin?key={key}&msg=Deleted+{email}", status_code=303)
    finally:
        conn.close()


async def privacy_page(request: Request):
    conn = open_conn()
    try:
        return templates.TemplateResponse(
            request, "privacy.html", {"site": site_context(conn, request)})
    finally:
        conn.close()


async def terms_page(request: Request):
    conn = open_conn()
    try:
        return templates.TemplateResponse(
            request, "terms.html", {"site": site_context(conn, request)})
    finally:
        conn.close()


async def delete_account(request: Request):
    """Allow a logged-in user to permanently delete their account (GDPR Art. 17)."""
    form = await request.form()
    if _bad_csrf(request, form):
        return JSONResponse({"error": "Invalid CSRF token."}, status_code=403)
    conn = open_conn()
    try:
        sub = get_current_user(conn, request)
        if not sub:
            return JSONResponse({"error": "Not authenticated."}, status_code=401)
        email = sub["email"]
        db.execute(conn, "DELETE FROM watchlist WHERE subscriber_id=?", (sub["id"],))
        db.execute(conn, "DELETE FROM collection_items WHERE subscriber_id=?", (sub["id"],))
        db.execute(conn, "DELETE FROM subscribers WHERE id=?", (sub["id"],))
        firebase_db.log_event(email, "account_deleted", ip=_client_ip(request))
        firebase_db.delete_user(email)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("dh_sid")
        return response
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    settings = get_settings()
    firebase_db.init(settings.firebase_credentials_json, settings.firebase_project_id)
    conn = open_conn()
    try:
        db.init_db(conn)
        if db.one(conn, "SELECT COUNT(*) c FROM products")["c"] == 0:
            from .. import seed as seed_mod
            seed_mod.seed(conn, settings)
    finally:
        conn.close()
    logger.info("drophound started")
    yield
    logger.info("drophound shutdown")


routes = [
    Route("/", landing),
    Route("/subscribe", subscribe, methods=["POST"]),
    Route("/register", register_page, methods=["GET", "POST"]),
    Route("/login", login_page, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST"]),
    Route("/watch", watch_page),
    Route("/watch/add", watch_add, methods=["POST"]),
    Route("/watch/remove", watch_remove, methods=["POST"]),
    Route("/account/delete", delete_account, methods=["POST"]),
    Route("/api/catalog", api_catalog),
    Route("/api/watchlist", api_watchlist),
    Route("/drops", drops_page),
    Route("/app", dashboard),
    Route("/collection", collection_page),
    Route("/pricing", pricing),
    Route("/upgrade", upgrade, methods=["POST"]),
    Route("/upgrade/success", upgrade_success),
    Route("/stripe/webhook", stripe_webhook, methods=["POST"]),
    Route("/digest", digest_page),
    Route("/go/{product_id:int}", go_redirect),
    Route("/robots.txt", robots_txt),
    Route("/sitemap.xml", sitemap_xml),
    Route("/hook/restock", hook_restock, methods=["POST"]),
    Route("/api/health", api_health),
    Route("/api/drops", api_drops),
    Route("/api/products", api_products),
    Route("/api/collection/{subscriber_id:int}/value", api_collection_value),
    Route("/privacy", privacy_page),
    Route("/terms", terms_page),
    Route("/admin", admin_page),
    Route("/admin/delete-user", admin_delete_user, methods=["POST"]),
    Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

# Middleware stack (outermost = first to process the request)
_ALLOWED_ORIGINS = [
    "https://drophound-xyy4.onrender.com",
    "http://localhost:8000",
    "http://localhost:8012",
    "http://127.0.0.1:8000",
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    exception_handlers={404: _not_found, 500: _server_error},
    middleware=[
        Middleware(LoggingMiddleware),
        Middleware(SecurityHeadersMiddleware),
        Middleware(RateLimitMiddleware),
        Middleware(SessionMiddleware),
        Middleware(CORSMiddleware,
                   allow_origins=_ALLOWED_ORIGINS,
                   allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type", "X-DropHound-Secret"],
                   allow_credentials=True),
        Middleware(GZipMiddleware, minimum_size=600),
    ],
)
