"""Meal catalogue and meal-level logging.

The friction reducer: you tap "Caprese" and every ingredient is deducted at once.
People remember what they cooked, not which SKUs left the fridge, so this is the
logging path that actually gets used day to day.

Meals are ranked by `times_used`, so within a couple of weeks your usual dinners
occupy the first row of buttons and logging costs one tap.
"""

import sqlite3
from datetime import date
from typing import Optional

from app import inventory


def list_meals(conn: sqlite3.Connection, limit: Optional[int] = None) -> list[dict]:
    sql = ("SELECT m.id, m.name, m.times_used, m.last_used, m.created_by, "
           "COUNT(mi.product_id) AS n_ingredients "
           "FROM meals m LEFT JOIN meal_ingredients mi ON mi.meal_id = m.id "
           "GROUP BY m.id ORDER BY m.times_used DESC, m.name")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def get_meal(conn: sqlite3.Connection, meal_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
    if row is None:
        return None
    meal = dict(row)
    meal["ingredients"] = [
        dict(r) for r in conn.execute(
            "SELECT mi.product_id, mi.qty, p.display_name, p.category, p.storage_zone "
            "FROM meal_ingredients mi JOIN products p ON p.id = mi.product_id "
            "WHERE mi.meal_id = ? ORDER BY p.display_name",
            (meal_id,),
        ).fetchall()
    ]
    return meal


def create_meal(conn: sqlite3.Connection, name: str,
                ingredients: list[dict], created_by: str = "user") -> int:
    """`ingredients` is [{product_id, qty}]."""
    cur = conn.execute(
        "INSERT INTO meals(name, created_by) VALUES(?, ?) "
        "ON CONFLICT(name) DO UPDATE SET name = excluded.name RETURNING id",
        (name.strip(), created_by),
    )
    meal_id = cur.fetchone()[0]
    set_ingredients(conn, meal_id, ingredients)
    return meal_id


def set_ingredients(conn: sqlite3.Connection, meal_id: int, ingredients: list[dict]) -> None:
    conn.execute("DELETE FROM meal_ingredients WHERE meal_id = ?", (meal_id,))
    for ing in ingredients:
        conn.execute(
            "INSERT INTO meal_ingredients(meal_id, product_id, qty) VALUES(?,?,?) "
            "ON CONFLICT(meal_id, product_id) DO UPDATE SET qty = excluded.qty",
            (meal_id, int(ing["product_id"]), float(ing.get("qty", 1))),
        )


def delete_meal(conn: sqlite3.Connection, meal_id: int) -> None:
    conn.execute("DELETE FROM meal_ingredients WHERE meal_id = ?", (meal_id,))
    conn.execute("DELETE FROM meals WHERE id = ?", (meal_id,))


def log_meal(conn: sqlite3.Connection, meal_id: int,
             happened_on: Optional[str] = None) -> dict:
    """Deduct every ingredient of a meal, FIFO within each product.

    Ingredients that are out of stock are reported rather than silently skipped:
    the inventory thought you had them and it was wrong, which is worth knowing.
    """
    meal = get_meal(conn, meal_id)
    if meal is None:
        raise LookupError(f"no meal {meal_id}")

    happened_on = happened_on or date.today().isoformat()
    deducted, missing = [], []

    for ing in meal["ingredients"]:
        touched = inventory.consume_product_fifo(
            conn, ing["product_id"], float(ing["qty"]),
            kind="consumed", happened_on=happened_on, source="meal", meal_id=meal_id,
        )
        if touched:
            deducted.append({"product_id": ing["product_id"],
                             "display_name": ing["display_name"],
                             "qty": ing["qty"]})
        else:
            missing.append({"product_id": ing["product_id"],
                            "display_name": ing["display_name"]})

    conn.execute(
        "UPDATE meals SET times_used = times_used + 1, last_used = ? WHERE id = ?",
        (happened_on, meal_id),
    )
    return {"meal": meal["name"], "deducted": deducted, "missing": missing}


def cookable_now(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    """Meals ranked by how much of them you can actually make right now.

    Used for the daily prompt's quick-pick row and, later, as the non-AI answer
    to "cosa cucino stasera?".
    """
    out = []
    for meal in list_meals(conn):
        full = get_meal(conn, meal["id"])
        ings = full["ingredients"]
        if not ings:
            continue
        available = 0
        expiring = 0
        for ing in ings:
            row = conn.execute(
                "SELECT COALESCE(SUM(qty_remaining), 0) q, MIN(expiry_date) e FROM lots "
                "WHERE product_id = ? AND status = 'in_stock' AND qty_remaining > 0",
                (ing["product_id"],),
            ).fetchone()
            if (row["q"] or 0) >= float(ing["qty"]):
                available += 1
                if row["e"]:
                    try:
                        if (date.fromisoformat(row["e"]) - date.today()).days <= 3:
                            expiring += 1
                    except ValueError:
                        pass
        out.append({
            "meal_id": meal["id"],
            "name": meal["name"],
            "coverage": round(available / len(ings), 2),
            "n_ingredients": len(ings),
            "uses_expiring": expiring,
            "times_used": meal["times_used"],
        })

    # Meals that rescue expiring food come first — that is the whole point.
    out.sort(key=lambda m: (-m["uses_expiring"], -m["coverage"], -m["times_used"]))
    return out[:limit]
