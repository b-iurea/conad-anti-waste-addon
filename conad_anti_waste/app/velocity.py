"""Consumption velocity and the pre-order shopping list.

Pure arithmetic over the event log — no AI involved.

The discipline that keeps this honest: a product needs at least MIN_EVENTS
consumption events before any prediction is made about it. Below that threshold
we report the stock we can see and pass no judgement. A confident wrong
"non comprare il latte" costs far more trust than a missing suggestion.
"""

import sqlite3
import statistics
from datetime import date, datetime, timedelta
from typing import Optional

WINDOW_WEEKS = 8          # how far back consumption is measured
MIN_EVENTS = 2            # below this, no rate is inferred
LONG_STOCK_DAYS = 30      # more than a month of cover => "hai già"
DEFAULT_ORDER_GAP = 14    # fallback when order history is too thin


def order_gap_days(conn: sqlite3.Connection) -> int:
    """Median gap between real orders — how long the next shop must cover.

    Derived from your actual rhythm instead of a magic constant, so the
    threshold adapts if you start ordering more or less often.
    """
    rows = conn.execute(
        "SELECT delivery_date FROM orders WHERE delivery_date != '' ORDER BY delivery_date"
    ).fetchall()
    dates = []
    for r in rows:
        try:
            dates.append(date.fromisoformat(r["delivery_date"]))
        except ValueError:
            continue
    if len(dates) < 3:
        return DEFAULT_ORDER_GAP
    gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
    return int(statistics.median(gaps)) if gaps else DEFAULT_ORDER_GAP


def consumption_rates(conn: sqlite3.Connection, today: Optional[date] = None) -> dict[int, dict]:
    """Per product: units consumed per week over the trailing window."""
    today = today or date.today()
    since = (today - timedelta(weeks=WINDOW_WEEKS)).isoformat()

    rows = conn.execute(
        """
        SELECT l.product_id,
               COUNT(*)      AS n_events,
               SUM(e.qty)    AS total_qty,
               MIN(e.happened_on) AS first_seen
        FROM events e
        JOIN lots l ON l.id = e.lot_id
        WHERE e.happened_on >= ? AND e.kind IN ('consumed', 'wasted', 'already_bad')
        GROUP BY l.product_id
        """,
        (since,),
    ).fetchall()

    rates = {}
    for r in rows:
        # Measure over the period actually observed, not the full window, so a
        # product first bought last week is not treated as idle for eight.
        try:
            span_days = max(7, (today - date.fromisoformat(r["first_seen"])).days)
        except (ValueError, TypeError):
            span_days = WINDOW_WEEKS * 7
        per_week = (r["total_qty"] or 0) / (span_days / 7)
        rates[r["product_id"]] = {
            "per_week": round(per_week, 2),
            "n_events": r["n_events"],
            "trusted": r["n_events"] >= MIN_EVENTS and per_week > 0,
        }
    return rates


def product_stock(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute(
        """
        SELECT p.id, p.display_name, p.category, p.storage_zone, p.is_food, p.unit_size,
               COALESCE(SUM(CASE WHEN l.status = 'in_stock' THEN l.qty_remaining END), 0) AS qty,
               MIN(CASE WHEN l.status = 'in_stock' AND l.qty_remaining > 0
                        THEN l.expiry_date END) AS next_expiry
        FROM products p
        LEFT JOIN lots l ON l.product_id = p.id
        GROUP BY p.id
        """
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def shopping_list(conn: sqlite3.Connection, today: Optional[date] = None) -> dict:
    """The pre-order answer: what you need, and what you must NOT buy again."""
    today = today or date.today()
    horizon = order_gap_days(conn)
    rates = consumption_rates(conn, today)
    stock = product_stock(conn)

    serve, hai_gia, unknown = [], [], []

    for pid, p in stock.items():
        rate = rates.get(pid, {})
        per_week = rate.get("per_week", 0.0)
        trusted = rate.get("trusted", False)
        qty = p["qty"] or 0
        days_cover = (qty / (per_week / 7)) if (trusted and per_week > 0) else None

        entry = {
            "product_id": pid,
            "display_name": p["display_name"],
            "category": p["category"],
            "storage_zone": p["storage_zone"],
            "qty": qty,
            "per_week": per_week,
            "days_cover": round(days_cover) if days_cover is not None else None,
            "next_expiry": p["next_expiry"],
        }

        if qty <= 0:
            if trusted:
                entry["reason"] = f"finito · ne usi ~{per_week:g}/sett"
                serve.append(entry)
            else:
                entry["reason"] = "finito"
                unknown.append(entry)
            continue

        if days_cover is not None and days_cover < horizon:
            entry["reason"] = (f"{qty:g} rimasti · ~{per_week:g}/sett · "
                               f"bastano ~{round(days_cover)}g")
            serve.append(entry)
        elif days_cover is not None and days_cover > LONG_STOCK_DAYS:
            entry["reason"] = f"{qty:g} in casa · te ne bastano per ~{round(days_cover)}g"
            hai_gia.append(entry)
        elif days_cover is None and qty >= 2:
            # No trusted rate, but a visible pile. Report the stock, judge nothing.
            entry["reason"] = f"{qty:g} in casa"
            hai_gia.append(entry)

    serve.sort(key=lambda e: (e["days_cover"] if e["days_cover"] is not None else -1))
    hai_gia.sort(key=lambda e: -(e["qty"] or 0))
    return {
        "horizon_days": horizon,
        "serve": serve,
        "hai_gia": hai_gia,
        "finiti_senza_storico": unknown,
    }


def overbought(conn: sqlite3.Connection, order_code: str) -> list[dict]:
    """After an order lands: what did you just re-buy while still well stocked?

    Too late to cancel, but this is exactly the feedback that changes the next
    order — which is the point.
    """
    rates = consumption_rates(conn)
    rows = conn.execute(
        """
        SELECT l.product_id, l.qty_initial, p.display_name,
               COALESCE((SELECT SUM(o.qty_remaining) FROM lots o
                         WHERE o.product_id = l.product_id AND o.status = 'in_stock'
                           AND o.order_code != l.order_code), 0) AS had_before
        FROM lots l JOIN products p ON p.id = l.product_id
        WHERE l.order_code = ?
        """,
        (order_code,),
    ).fetchall()

    flagged = []
    for r in rows:
        had = r["had_before"] or 0
        if had <= 0:
            continue
        rate = rates.get(r["product_id"], {})
        per_week = rate.get("per_week", 0)
        cover = (had / (per_week / 7)) if rate.get("trusted") and per_week > 0 else None
        if cover is not None and cover > LONG_STOCK_DAYS:
            flagged.append({
                "display_name": r["display_name"],
                "had_before": had,
                "bought": r["qty_initial"],
                "reason": f"ne avevi già {had:g} (~{round(cover)}g di scorta)",
            })
        elif cover is None and had >= 3:
            flagged.append({
                "display_name": r["display_name"],
                "had_before": had,
                "bought": r["qty_initial"],
                "reason": f"ne avevi già {had:g}",
            })
    return flagged
