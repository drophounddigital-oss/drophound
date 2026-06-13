"""DropHound web application (Starlette).

Serves the three product layers:
  Layer 1  /            landing + email capture, /drops public feed
  Layer 2  /app         premium dashboard, /collection P/L tracker, /pricing
  Layer 3  /go/{id}     affiliate redirect (logs the click)
Plus a small JSON API under /api and the AI digest at /digest.
"""

from __future__ import annotations

import html
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import timedelta

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from .. import affiliates, db, filters
from ..config import STATIC_DIR, TEMPLATES_DIR, get_settings
from ..engine import digest as digest_mod
from ..engine import pipeline
from ..engine import resale
from ..engine.alerts import AlertMessage, broadcast_dispatchers
from ..stats import premium_multiple, trend_pct
from ..util import humanize_age, money, now_utc, parse_iso

DEMO_EMAIL = "demo@drophound.app"

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
        "multiple": premium_multiple(row["retail_price"], median),
        "age": humanize_age(parse_iso(row["detected_at"]), now=now),
        "color": row["image_hint"] or "#888",
        "initials": initials(row["character"]),
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


def site_context(conn: sqlite3.Connection) -> dict:
    s = get_settings()
    return {
        "premium_price": s.premium_price,
        "tracked": db.one(conn, "SELECT COUNT(*) c FROM products")["c"],
        "alerts_24h": db.one(
            conn,
            "SELECT COUNT(*) c FROM restock_events WHERE detected_at >= ?",
            ((now_utc() - timedelta(hours=24)).isoformat(),),
        )["c"],
        "subscribers": db.one(conn, "SELECT COUNT(*) c FROM subscribers")["c"],
    }


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
            "site": site_context(conn),
            "subscribed": request.query_params.get("subscribed") == "1",
            "error": request.query_params.get("error") == "1",
        }
        return templates.TemplateResponse(request, "landing.html", ctx)
    finally:
        conn.close()


async def subscribe(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    telegram = (form.get("telegram") or "").strip() or None
    if "@" not in email or "." not in email.split("@")[-1]:
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
            {"events": events, "site": site_context(conn)},
        )
    finally:
        conn.close()


async def dashboard(request: Request):
    conn = open_conn()
    try:
        now = now_utc()
        sub = get_subscriber(conn, DEMO_EMAIL)
        if not sub:
            return templates.TemplateResponse(
                request, "dashboard.html",
                {"sub": None, "site": site_context(conn)},
            )
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
            "site": site_context(conn),
        }
        return templates.TemplateResponse(request, "dashboard.html", ctx)
    finally:
        conn.close()


async def collection_page(request: Request):
    conn = open_conn()
    try:
        sub = get_subscriber(conn, DEMO_EMAIL)
        summary = collection_summary(conn, sub["id"]) if sub else None
        return templates.TemplateResponse(
            request, "collection.html",
            {"sub": sub, "summary": summary, "site": site_context(conn)},
        )
    finally:
        conn.close()


async def pricing(request: Request):
    conn = open_conn()
    try:
        return templates.TemplateResponse(
            request, "pricing.html",
            {"site": site_context(conn),
             "upgraded": request.query_params.get("upgraded") == "1"},
        )
    finally:
        conn.close()


async def upgrade(request: Request):
    form = await request.form()
    email = (form.get("email") or DEMO_EMAIL).strip().lower()
    settings = get_settings()
    conn = open_conn()
    try:
        sub = get_subscriber(conn, email)
        if sub:
            # NOTE: production path -> create a Stripe Checkout Session here using
            # settings.stripe_secret_key / settings.stripe_price_id and redirect to it.
            # This dry-run flips the tier locally so the flow is demonstrable.
            db.execute(
                conn,
                "UPDATE subscribers SET tier='premium', premium_since=? WHERE id=?",
                (now_utc().isoformat(), sub["id"]),
            )
        return RedirectResponse("/pricing?upgraded=1", status_code=303)
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
             "site": site_context(conn)},
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
_RESALE_SUB = ("(SELECT median FROM resale_prices r WHERE r.product_id=p.id "
               "ORDER BY r.captured_at DESC, r.id DESC LIMIT 1)")


def get_or_create_subscriber(conn: sqlite3.Connection, email: str) -> sqlite3.Row | None:
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        return None
    row = get_subscriber(conn, email)
    if row:
        return row
    db.execute(conn, "INSERT INTO subscribers (email, tier, created_at) VALUES (?, 'free', ?)",
               (email, now_utc().isoformat()))
    return get_subscriber(conn, email)


def _product_item(row: sqlite3.Row, watched: set[int]) -> dict:
    return {
        "id": row["id"], "name": row["name"], "brand": row["brand"],
        "character": row["character"], "retailer": row["retailer"],
        "region": row["region"], "price": row["retail_price"],
        "resale_median": row["resale_median"], "status": row["status"] or "unknown",
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
        p.retail_price, p.image_hint, {_STATUS_SUB} AS status, {_RESALE_SUB} AS resale_median
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


async def watch_page(request: Request):
    conn = open_conn()
    try:
        return templates.TemplateResponse(
            request, "watch.html",
            {"site": site_context(conn), "popular": POPULAR_CHARACTERS})
    finally:
        conn.close()


async def api_catalog(request: Request):
    qp = request.query_params
    q = qp.get("q", "").strip()
    character = qp.get("character", "").strip()
    in_stock = qp.get("in_stock") == "1"
    try:
        page = max(1, int(qp.get("page", "1")))
    except ValueError:
        page = 1
    email = (qp.get("email") or "").strip().lower()
    conn = open_conn()
    try:
        watched, _ = _watched_ids(conn, email)
        items, total, pages = catalog_page(conn, q, character, in_stock, page, watched)
        return JSONResponse({"products": items, "page": page, "pages": pages,
                             "total": total, "watch_count": len(watched)})
    finally:
        conn.close()


async def api_watchlist(request: Request):
    email = (request.query_params.get("email") or "").strip().lower()
    conn = open_conn()
    try:
        sub = get_subscriber(conn, email) if "@" in email else None
        if not sub:
            return JSONResponse({"products": [], "count": 0})
        rows = db.q(conn, f"""SELECT p.id, p.name, p.brand, p.character, p.retailer, p.region,
            p.retail_price, p.image_hint, {_STATUS_SUB} AS status, {_RESALE_SUB} AS resale_median
            FROM watchlist w JOIN products p ON p.id = w.product_id
            WHERE w.subscriber_id = ? ORDER BY w.created_at DESC""", (sub["id"],))
        items = [_product_item(r, {r["id"] for r in rows}) for r in rows]
        return JSONResponse({"products": items, "count": len(items)})
    finally:
        conn.close()


async def watch_add(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    try:
        pid = int(form.get("product_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad product_id"}, status_code=400)
    conn = open_conn()
    try:
        sub = get_or_create_subscriber(conn, email)
        if not sub:
            return JSONResponse({"error": "a valid email is required"}, status_code=400)
        if not db.one(conn, "SELECT 1 FROM products WHERE id = ?", (pid,)):
            return JSONResponse({"error": "unknown product"}, status_code=404)
        try:
            db.execute(conn, """INSERT INTO watchlist (subscriber_id, product_id, created_at)
                       VALUES (?,?,?)""", (sub["id"], pid, now_utc().isoformat()))
        except sqlite3.IntegrityError:
            pass  # already watching
        count = db.one(conn, "SELECT COUNT(*) c FROM watchlist WHERE subscriber_id=?",
                       (sub["id"],))["c"]
        return JSONResponse({"watched": True, "count": count})
    finally:
        conn.close()


async def watch_remove(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    try:
        pid = int(form.get("product_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad product_id"}, status_code=400)
    conn = open_conn()
    try:
        sub = get_subscriber(conn, email)
        count = 0
        if sub:
            db.execute(conn, "DELETE FROM watchlist WHERE subscriber_id=? AND product_id=?",
                       (sub["id"], pid))
            count = db.one(conn, "SELECT COUNT(*) c FROM watchlist WHERE subscriber_id=?",
                           (sub["id"],))["c"]
        return JSONResponse({"watched": False, "count": count})
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app):
    conn = open_conn()
    try:
        db.init_db(conn)
        # On a fresh host the bundled DB ships the full catalog; if it's somehow
        # empty, fall back to the demo seed so the site is never blank.
        if db.one(conn, "SELECT COUNT(*) c FROM products")["c"] == 0:
            from .. import seed as seed_mod
            seed_mod.seed(conn, get_settings())
    finally:
        conn.close()
    yield


routes = [
    Route("/", landing),
    Route("/subscribe", subscribe, methods=["POST"]),
    Route("/watch", watch_page),
    Route("/watch/add", watch_add, methods=["POST"]),
    Route("/watch/remove", watch_remove, methods=["POST"]),
    Route("/api/catalog", api_catalog),
    Route("/api/watchlist", api_watchlist),
    Route("/drops", drops_page),
    Route("/app", dashboard),
    Route("/collection", collection_page),
    Route("/pricing", pricing),
    Route("/upgrade", upgrade, methods=["POST"]),
    Route("/digest", digest_page),
    Route("/go/{product_id:int}", go_redirect),
    Route("/hook/restock", hook_restock, methods=["POST"]),
    Route("/api/health", api_health),
    Route("/api/drops", api_drops),
    Route("/api/products", api_products),
    Route("/api/collection/{subscriber_id:int}/value", api_collection_value),
    Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

app = Starlette(routes=routes, lifespan=lifespan)
