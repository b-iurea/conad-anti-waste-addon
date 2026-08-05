"""Stock queries and the consumption log.

Every change to stock goes through `log_event`, which is the single place that
writes to `events`, decrements the lot, and feeds the learning loop. Keeping it
to one function is what makes the learning evidence trustworthy — there is no
path that changes stock without recording why.
"""

import sqlite3
from datetime import date, datetime
from typing import Optional

from app import learning

VALID_KINDS = ("consumed", "wasted", "already_bad", "adjust")

STOCK_SELECT = """
SELECT
    l.id            AS lot_id,
    l.product_id,
    l.qty_remaining,
    l.qty_initial,
    l.unit_price_eur,
    l.delivery_date,
    l.expiry_date,
    l.opened_date,
    l.status,
    l.order_code,
    p.display_name,
    p.category,
    p.storage_zone,
    p.is_food,
    p.unit_size
FROM lots l
JOIN products p ON p.id = l.product_id
"""


def _days_left(expiry: Optional[str], today: Optional[date] = None) -> Optional[int]:
    if not expiry:
        return None
    try:
        return (date.fromisoformat(expiry) - (today or date.today())).days
    except ValueError:
        return None


def _decorate(rows, today: Optional[date] = None) -> list[dict]:
    out = []
    for r in rows:
        d = dict(r)
        d["days_left"] = _days_left(d.get("expiry_date"), today)
        d["value_eur"] = round((d.get("unit_price_eur") or 0) * (d.get("qty_remaining") or 0), 2)
        out.append(d)
    return out


def current_stock(conn: sqlite3.Connection, zone: Optional[str] = None,
                  include_non_food: bool = True, today: Optional[date] = None) -> list[dict]:
    """Everything in stock, soonest expiry first. Undated items sort last."""
    sql = STOCK_SELECT + " WHERE l.status = 'in_stock' AND l.qty_remaining > 0"
    params: list = []
    if zone:
        sql += " AND p.storage_zone = ?"
        params.append(zone)
    if not include_non_food:
        sql += " AND p.is_food = 1"
    sql += " ORDER BY (l.expiry_date IS NULL), l.expiry_date, p.display_name"
    return _decorate(conn.execute(sql, params).fetchall(), today)


def expiring_soon(conn: sqlite3.Connection, within_days: int = 3,
                  today: Optional[date] = None) -> list[dict]:
    """Food that expires within N days and has not expired yet."""
    return [
        it for it in current_stock(conn, include_non_food=False, today=today)
        if it["days_left"] is not None and 0 <= it["days_left"] <= within_days
    ]


def overdue(conn: sqlite3.Connection, today: Optional[date] = None) -> list[dict]:
    """Past expiry and still in stock — the Sunday reckoning list.

    Archived history is excluded by construction: those lots are not `in_stock`.
    """
    return [
        it for it in current_stock(conn, include_non_food=False, today=today)
        if it["days_left"] is not None and it["days_left"] < 0
    ]


def get_lot(conn: sqlite3.Connection, lot_id: int, today: Optional[date] = None) -> Optional[dict]:
    row = conn.execute(STOCK_SELECT + " WHERE l.id = ?", (lot_id,)).fetchone()
    return _decorate([row], today)[0] if row else None


def log_event(conn: sqlite3.Connection, lot_id: int, kind: str, qty: float = 1.0,
              happened_on: Optional[str] = None, source: str = "dashboard",
              meal_id: Optional[int] = None) -> dict:
    """Record consumption/waste for one lot, and learn from it.

    Returns the lot as it stands afterwards. Quantity is clamped to what is
    actually left, so stock can never go negative.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown event kind: {kind}")

    lot = conn.execute(
        "SELECT l.*, p.category FROM lots l JOIN products p ON p.id = l.product_id WHERE l.id = ?",
        (lot_id,),
    ).fetchone()
    if lot is None:
        raise LookupError(f"no lot {lot_id}")

    happened_on = happened_on or date.today().isoformat()
    qty = max(0.0, min(float(qty), float(lot["qty_remaining"])))
    if qty == 0:
        return get_lot(conn, lot_id)

    conn.execute(
        "INSERT INTO events(lot_id, happened_on, logged_at, qty, kind, source, meal_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (lot_id, happened_on, datetime.now().isoformat(timespec="seconds"),
         qty, kind, source, meal_id),
    )

    remaining = float(lot["qty_remaining"]) - qty
    conn.execute(
        "UPDATE lots SET qty_remaining = ?, status = ? WHERE id = ?",
        (remaining, "finished" if remaining <= 0 else "in_stock", lot_id),
    )

    # Learning: how many days after delivery did this happen?
    if kind != "adjust" and lot["delivery_date"]:
        try:
            observed = (date.fromisoformat(happened_on)
                        - date.fromisoformat(lot["delivery_date"])).days
        except ValueError:
            observed = None
        if observed is not None and observed >= 0:
            learning.record_observation(conn, lot["category"], kind, observed)
            learning.refresh_product_shelf_life(conn, lot["category"])

    return get_lot(conn, lot_id)


def consume_product_fifo(conn: sqlite3.Connection, product_id: int, qty: float = 1.0,
                         kind: str = "consumed", happened_on: Optional[str] = None,
                         source: str = "meal", meal_id: Optional[int] = None) -> list[dict]:
    """Consume `qty` of a product across its lots, oldest expiry first.

    FIFO by expiry is both correct for the data and what a person actually does
    standing at the fridge door: you reach for the one that dies first.
    """
    lots = conn.execute(
        "SELECT id, qty_remaining FROM lots "
        "WHERE product_id = ? AND status = 'in_stock' AND qty_remaining > 0 "
        "ORDER BY (expiry_date IS NULL), expiry_date, id",
        (product_id,),
    ).fetchall()

    touched, left = [], float(qty)
    for lot in lots:
        if left <= 0:
            break
        take = min(left, float(lot["qty_remaining"]))
        touched.append(log_event(conn, lot["id"], kind, take, happened_on, source, meal_id))
        left -= take
    return touched


def set_expiry(conn: sqlite3.Connection, lot_id: int, new_date: str) -> Optional[dict]:
    """User correction of a single lot's expiry date."""
    date.fromisoformat(new_date)  # validate, raises ValueError
    conn.execute("UPDATE lots SET expiry_date = ? WHERE id = ?", (new_date, lot_id))
    return get_lot(conn, lot_id)


def extend_expiry(conn: sqlite3.Connection, lot_id: int, extra_days: int) -> Optional[dict]:
    """'Ancora buono' during the weekly reckoning.

    Pushes the date out AND records the lower bound: the food was demonstrably
    still fine this many days after delivery.
    """
    lot = conn.execute(
        "SELECT l.expiry_date, l.delivery_date, p.category FROM lots l "
        "JOIN products p ON p.id = l.product_id WHERE l.id = ?", (lot_id,)
    ).fetchone()
    if lot is None:
        raise LookupError(f"no lot {lot_id}")

    base = date.today()
    if lot["expiry_date"]:
        try:
            base = max(base, date.fromisoformat(lot["expiry_date"]))
        except ValueError:
            pass
    from datetime import timedelta
    new_date = (base + timedelta(days=int(extra_days))).isoformat()
    conn.execute("UPDATE lots SET expiry_date = ? WHERE id = ?", (new_date, lot_id))

    if lot["delivery_date"]:
        try:
            observed = (date.today() - date.fromisoformat(lot["delivery_date"])).days
            # "still_good" rather than "consumed": the user is explicitly telling
            # us the food is fine right now, so it is trusted without the
            # plausibility cap that guards inferred consumption.
            learning.record_observation(conn, lot["category"], "still_good", observed)
            learning.refresh_product_shelf_life(conn, lot["category"])
        except ValueError:
            pass
    return get_lot(conn, lot_id)


def review_queue(conn: sqlite3.Connection) -> list[dict]:
    """Products the rules could not classify — they need a human."""
    rows = conn.execute(
        "SELECT id, display_name, category, storage_zone, shelf_life_days "
        "FROM products WHERE category = 'sconosciuto' ORDER BY display_name"
    ).fetchall()
    return [dict(r) for r in rows]
