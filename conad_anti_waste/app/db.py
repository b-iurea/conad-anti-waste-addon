"""SQLite access and schema.

Single-writer by design: one server replica, one bot replica, WAL mode so the
readers never block. Schema changes go in SCHEMA as new statements plus a
migration step keyed on `schema_version` in the `state` table.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from app.config import get_settings

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    code           TEXT PRIMARY KEY,
    service        TEXT,
    order_date     TEXT,
    delivery_date  TEXT,
    total_eur      REAL,
    fetched_at     TEXT,
    is_historical  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY,
    norm_name       TEXT UNIQUE,
    display_name    TEXT,
    category        TEXT,
    storage_zone    TEXT,
    is_food         INTEGER DEFAULT 1,
    shelf_life_days INTEGER,
    unit_size       TEXT,
    last_price_eur  REAL,
    classified_by   TEXT DEFAULT 'rules',
    catalog_code    TEXT,
    catalog_match   TEXT   -- 'exact' | 'fuzzy', so a guess never passes as fact
);

CREATE TABLE IF NOT EXISTS lots (
    id             INTEGER PRIMARY KEY,
    product_id     INTEGER REFERENCES products(id),
    order_code     TEXT REFERENCES orders(code),
    qty_initial    REAL,
    qty_remaining  REAL,
    unit_price_eur REAL,
    delivery_date  TEXT,
    expiry_date    TEXT,
    opened_date    TEXT,
    status         TEXT DEFAULT 'in_stock',
    UNIQUE(order_code, product_id)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    lot_id      INTEGER REFERENCES lots(id),
    happened_on TEXT,
    logged_at   TEXT,
    qty         REAL,
    kind        TEXT,
    source      TEXT,
    meal_id     INTEGER
);

CREATE TABLE IF NOT EXISTS meals (
    id         INTEGER PRIMARY KEY,
    name       TEXT UNIQUE,
    times_used INTEGER DEFAULT 0,
    last_used  TEXT,
    created_by TEXT DEFAULT 'user'
);

CREATE TABLE IF NOT EXISTS meal_ingredients (
    meal_id    INTEGER REFERENCES meals(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id),
    qty        REAL DEFAULT 1,
    UNIQUE(meal_id, product_id)
);

CREATE TABLE IF NOT EXISTS shelf_life (
    category    TEXT PRIMARY KEY,
    prior_days  INTEGER,
    upper_sum   REAL DEFAULT 0,
    upper_count INTEGER DEFAULT 0,
    lower_max   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS job_runs (
    job_name TEXT PRIMARY KEY,
    last_run TEXT
);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- The spesaonline.conad.it catalogue. Separate from `products` because it is
-- the shop's view of the world (everything for sale), while `products` is
-- yours (what you have actually bought). They are joined on norm_name.
CREATE TABLE IF NOT EXISTS catalog (
    code          TEXT PRIMARY KEY,   -- Conad's own product code, stable
    norm_name     TEXT,
    display_name  TEXT,
    brand         TEXT,
    cat1          TEXT,               -- Conad's taxonomy, three levels
    cat2          TEXT,
    cat3          TEXT,
    net_qty       REAL,
    net_qty_um    TEXT,
    bassi_fissi   INTEGER DEFAULT 0,
    price_eur     REAL,
    first_seen    TEXT,
    last_seen     TEXT
);

-- One row per observed price, so a drop is something we can prove rather than
-- infer. Conad exposes no promo badge without a selected store, so a discount
-- here means "cheaper than we have seen it", which is the honest claim.
CREATE TABLE IF NOT EXISTS catalog_price (
    code        TEXT REFERENCES catalog(code),
    observed_on TEXT,
    price_eur   REAL,
    PRIMARY KEY (code, observed_on)
);

CREATE INDEX IF NOT EXISTS idx_catalog_norm  ON catalog(norm_name);
CREATE INDEX IF NOT EXISTS idx_catprice_code ON catalog_price(code, observed_on);
CREATE INDEX IF NOT EXISTS idx_lots_status  ON lots(status, expiry_date);
CREATE INDEX IF NOT EXISTS idx_lots_product ON lots(product_id);
CREATE INDEX IF NOT EXISTS idx_events_lot   ON events(lot_id);
CREATE INDEX IF NOT EXISTS idx_events_date  ON events(happened_on);
"""


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False because FastAPI may run a request's dependency
    # setup and its endpoint on different threadpool threads. Each request still
    # gets its own connection and never shares it concurrently, so this relaxes
    # a check that does not apply rather than hiding real cross-thread sharing.
    conn = sqlite3.connect(str(path), timeout=15.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def session(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Transactional connection: commits on success, rolls back on error."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init(path: Optional[Path] = None) -> None:
    with session(path) as conn:
        conn.executescript(SCHEMA)
        current = get_state(conn, "schema_version")
        if current is None:
            # Fresh database: CREATE TABLE above already produced the current
            # shape, so there is nothing to migrate.
            set_state(conn, "schema_version", str(SCHEMA_VERSION))
        else:
            _migrate(conn, int(current))


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """Bring an existing database up to SCHEMA_VERSION.

    CREATE TABLE IF NOT EXISTS in SCHEMA covers new *tables* for free; only
    changes to existing tables need a step here.
    """
    if from_version >= SCHEMA_VERSION:
        return

    if from_version < 2:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
        if "catalog_code" not in cols:
            # Nullable: a product only gets a code once it is matched against
            # the catalogue, and plenty never will (delisted, renamed).
            conn.execute("ALTER TABLE products ADD COLUMN catalog_code TEXT")

    if from_version < 3:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
        if "catalog_match" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN catalog_match TEXT")

    set_state(conn, "schema_version", str(SCHEMA_VERSION))


# --- small key/value state helpers ----------------------------------------

def get_state(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
