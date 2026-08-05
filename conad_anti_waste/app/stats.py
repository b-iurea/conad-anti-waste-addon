"""Waste analytics.

Everything derives from `events`, `lots` and the prices Conad already gives us —
no extra schema. Waste is measured, not guessed.

`logging_compliance` is deliberately included: if nobody is logging, every other
number here is fiction, and the dashboard should say so rather than present a
confident 0% waste rate built on silence.
"""

import sqlite3
from datetime import date, timedelta
from typing import Optional

WASTE_KINDS = ("wasted", "already_bad")


def _euro(value) -> float:
    return round(float(value or 0), 2)


def summary(conn: sqlite3.Connection, today: Optional[date] = None) -> dict:
    today = today or date.today()

    rows = conn.execute(
        """
        SELECT e.kind, SUM(e.qty) AS qty, SUM(e.qty * COALESCE(l.unit_price_eur, 0)) AS eur
        FROM events e JOIN lots l ON l.id = e.lot_id
        WHERE e.kind != 'adjust'
        GROUP BY e.kind
        """
    ).fetchall()

    by_kind = {r["kind"]: {"qty": r["qty"] or 0, "eur": _euro(r["eur"])} for r in rows}
    consumed = by_kind.get("consumed", {"qty": 0, "eur": 0.0})
    wasted_qty = sum(by_kind.get(k, {}).get("qty", 0) for k in WASTE_KINDS)
    wasted_eur = sum(by_kind.get(k, {}).get("eur", 0.0) for k in WASTE_KINDS)

    total = consumed["qty"] + wasted_qty
    return {
        "consumed_qty": consumed["qty"],
        "consumed_eur": consumed["eur"],
        "wasted_qty": wasted_qty,
        "wasted_eur": _euro(wasted_eur),
        "total_logged_qty": total,
        "waste_rate": round(wasted_qty / total, 3) if total else None,
        "stock_value_eur": stock_value(conn),
        "at_risk_eur": at_risk_value(conn, today),
    }


def stock_value(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT SUM(qty_remaining * COALESCE(unit_price_eur, 0)) v FROM lots "
        "WHERE status = 'in_stock'"
    ).fetchone()
    return _euro(row["v"])


def at_risk_value(conn: sqlite3.Connection, today: Optional[date] = None,
                  within_days: int = 3) -> float:
    """€ of food that will be wasted if nothing is cooked in the next few days."""
    today = today or date.today()
    limit = (today + timedelta(days=within_days)).isoformat()
    row = conn.execute(
        "SELECT SUM(l.qty_remaining * COALESCE(l.unit_price_eur, 0)) v FROM lots l "
        "JOIN products p ON p.id = l.product_id "
        "WHERE l.status = 'in_stock' AND p.is_food = 1 "
        "AND l.expiry_date IS NOT NULL AND l.expiry_date <= ?",
        (limit,),
    ).fetchone()
    return _euro(row["v"])


def weekly_waste(conn: sqlite3.Connection, weeks: int = 12,
                 today: Optional[date] = None) -> list[dict]:
    """€ wasted per ISO week, oldest first, with empty weeks filled in.

    Gaps matter: a week with no waste and a week with no logging look identical
    in the raw data, so the series is zero-filled and read alongside compliance.
    """
    today = today or date.today()
    since = today - timedelta(weeks=weeks)

    rows = conn.execute(
        """
        SELECT strftime('%Y-%W', e.happened_on) AS wk,
               SUM(e.qty * COALESCE(l.unit_price_eur, 0)) AS eur,
               SUM(e.qty) AS qty
        FROM events e JOIN lots l ON l.id = e.lot_id
        WHERE e.kind IN ('wasted', 'already_bad') AND e.happened_on >= ?
        GROUP BY wk
        """,
        (since.isoformat(),),
    ).fetchall()
    found = {r["wk"]: r for r in rows}

    series = []
    cursor = since
    while cursor <= today:
        key = cursor.strftime("%Y-%W")
        if not series or series[-1]["week"] != key:
            r = found.get(key)
            series.append({
                "week": key,
                "label": cursor.strftime("%d/%m"),
                "eur": _euro(r["eur"]) if r else 0.0,
                "qty": (r["qty"] if r else 0) or 0,
            })
        cursor += timedelta(days=7)
    return series


def waste_by_category(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.category,
               SUM(e.qty) AS qty,
               SUM(e.qty * COALESCE(l.unit_price_eur, 0)) AS eur
        FROM events e
        JOIN lots l     ON l.id = e.lot_id
        JOIN products p ON p.id = l.product_id
        WHERE e.kind IN ('wasted', 'already_bad')
        GROUP BY p.category
        ORDER BY eur DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [{"category": r["category"], "qty": r["qty"] or 0, "eur": _euro(r["eur"])}
            for r in rows]


def logging_compliance(conn: sqlite3.Connection, days: int = 30,
                       today: Optional[date] = None) -> dict:
    """How many of the last N days have at least one logged event.

    This is the health metric for the whole system. If it drops, every other
    number becomes decoration.
    """
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    row = conn.execute(
        "SELECT COUNT(DISTINCT happened_on) d FROM events "
        "WHERE happened_on >= ? AND kind != 'adjust'",
        (since,),
    ).fetchone()
    logged_days = row["d"] or 0
    return {
        "days_window": days,
        "days_logged": logged_days,
        "rate": round(logged_days / days, 2),
        "streak": current_streak(conn, today),
    }


def current_streak(conn: sqlite3.Connection, today: Optional[date] = None) -> int:
    """Consecutive days with at least one logged event, counting back.

    Today not being logged yet does not break the streak — the day is not over.
    """
    today = today or date.today()
    rows = conn.execute(
        "SELECT DISTINCT happened_on FROM events WHERE kind != 'adjust' "
        "ORDER BY happened_on DESC LIMIT 400"
    ).fetchall()
    logged = {r["happened_on"] for r in rows}

    streak, cursor = 0, today
    if today.isoformat() not in logged:
        cursor = today - timedelta(days=1)
    while cursor.isoformat() in logged:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def full(conn: sqlite3.Connection, today: Optional[date] = None) -> dict:
    return {
        **summary(conn, today),
        "weekly": weekly_waste(conn, today=today),
        "by_category": waste_by_category(conn),
        "compliance": logging_compliance(conn, today=today),
    }
