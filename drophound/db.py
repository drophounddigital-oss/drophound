"""SQLite storage layer.

Plain stdlib `sqlite3` — no ORM — keeps the dependency surface at zero and the
schema readable. Rows come back as `sqlite3.Row` (dict-like).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sku           TEXT UNIQUE NOT NULL,
    brand         TEXT NOT NULL,
    line          TEXT NOT NULL,
    character     TEXT NOT NULL,
    name          TEXT NOT NULL,
    retailer      TEXT NOT NULL,
    region        TEXT NOT NULL,
    product_url   TEXT,
    image_hint    TEXT,
    retail_price  REAL,
    currency      TEXT NOT NULL DEFAULT 'USD',
    in_stock_signal TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS restock_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    event_type   TEXT NOT NULL,   -- drop | restock | price_drop | low_stock | sold_out
    status       TEXT NOT NULL,   -- in_stock | low_stock | sold_out | preorder
    price        REAL,
    note         TEXT,
    source       TEXT NOT NULL DEFAULT 'sample',
    detected_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_product ON restock_events(product_id);
CREATE INDEX IF NOT EXISTS idx_events_time ON restock_events(detected_at);
CREATE INDEX IF NOT EXISTS idx_events_product_time ON restock_events(product_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON restock_events(event_type);

CREATE TABLE IF NOT EXISTS resale_prices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    source       TEXT NOT NULL DEFAULT 'ebay',
    sample_size  INTEGER NOT NULL,
    low          REAL,
    high         REAL,
    median       REAL,
    average      REAL,
    currency     TEXT NOT NULL DEFAULT 'USD',
    captured_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resale_product ON resale_prices(product_id);
CREATE INDEX IF NOT EXISTS idx_resale_product_time ON resale_prices(product_id, captured_at);

CREATE TABLE IF NOT EXISTS subscribers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT UNIQUE NOT NULL,
    tier              TEXT NOT NULL DEFAULT 'free',  -- free | premium
    telegram          TEXT,
    discord           TEXT,
    filter_brands     TEXT,       -- comma-separated, empty = all
    filter_characters TEXT,
    filter_regions    TEXT,
    price_ceiling     REAL,
    created_at        TEXT NOT NULL,
    premium_since     TEXT,
    stripe_customer_id TEXT
);

CREATE TABLE IF NOT EXISTS collection_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    product_id    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    qty           INTEGER NOT NULL DEFAULT 1,
    paid_price    REAL,
    condition     TEXT NOT NULL DEFAULT 'mint',
    acquired_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_collection_sub ON collection_items(subscriber_id);

CREATE TABLE IF NOT EXISTS watchlist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    product_id    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    created_at    TEXT NOT NULL,
    UNIQUE(subscriber_id, product_id)
);
CREATE INDEX IF NOT EXISTS idx_watch_sub ON watchlist(subscriber_id);
CREATE INDEX IF NOT EXISTS idx_watch_product ON watchlist(product_id);

CREATE TABLE IF NOT EXISTS alerts_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER REFERENCES restock_events(id) ON DELETE SET NULL,
    subscriber_id INTEGER REFERENCES subscribers(id) ON DELETE SET NULL,
    channel       TEXT NOT NULL,
    status        TEXT NOT NULL,   -- sent | dry_run | failed | skipped
    detail        TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS affiliate_clicks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id    INTEGER REFERENCES products(id) ON DELETE SET NULL,
    target        TEXT NOT NULL,
    subscriber_id INTEGER REFERENCES subscribers(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL
);
"""

# Tables wiped by `seed --reset`, child-first to respect foreign keys.
DATA_TABLES = [
    "watchlist",
    "affiliate_clicks",
    "alerts_log",
    "collection_items",
    "resale_prices",
    "restock_events",
    "subscribers",
    "products",
]


def connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Lightweight migrations so an already-deployed DB picks up new columns.
    _ensure_column(conn, "subscribers", "stripe_customer_id", "TEXT")
    _ensure_column(conn, "subscribers", "session_id", "TEXT")
    conn.commit()


def reset_data(conn: sqlite3.Connection) -> None:
    for table in DATA_TABLES:
        conn.execute(f"DELETE FROM {table}")
    # Reset autoincrement counters if the table exists.
    try:
        conn.execute("DELETE FROM sqlite_sequence")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def q(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def execute(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur
