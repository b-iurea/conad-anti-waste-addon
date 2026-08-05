"""Turning Conad orders into products and lots.

Two entry points:

  import_live()    fetch from my.conad.it using the saved browser-login cookies
  backfill_csv()   seed from the parent project's orders.csv, no network needed

Both are idempotent: orders are keyed by their Conad code and lots by
(order_code, product_id), so re-running an import is a no-op rather than a
duplicate inventory.

Historical orders are imported with is_historical=1 and their lots go straight
to `archived`. They still feed velocity, prices and category priors, but they
never appear in the fridge view or the Sunday reckoning — otherwise going live
would open with a 200-item interrogation about groceries eaten months ago.
"""

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from app import conad_api, learning
from app.classify import classify, normalize_name, parse_unit_size
from app.conad_api import Order, Product, parse_price, parse_qty

log = logging.getLogger(__name__)


@dataclass
class ImportResult:
    orders_added: int = 0
    orders_skipped: int = 0
    lots_added: int = 0
    products_created: int = 0
    codes: list[str] = None

    def __post_init__(self):
        if self.codes is None:
            self.codes = []


def upsert_product(conn, name: str, unit_price: Optional[float] = None) -> tuple[int, bool]:
    """Find or create the product identity for a raw Conad name.

    Returns (product_id, created). Classification runs only on creation, so a
    later user correction is never overwritten by a re-import.
    """
    norm = normalize_name(name)
    row = conn.execute("SELECT id FROM products WHERE norm_name = ?", (norm,)).fetchone()
    if row:
        if unit_price is not None:
            conn.execute("UPDATE products SET last_price_eur = ? WHERE id = ?",
                         (unit_price, row["id"]))
        return row["id"], False

    c = classify(name)
    learning.ensure_category(conn, c.category, c.shelf_life_days)
    shelf_life = learning.effective_days(conn, c.category, c.shelf_life_days)

    cur = conn.execute(
        "INSERT INTO products(norm_name, display_name, category, storage_zone, is_food, "
        "shelf_life_days, unit_size, last_price_eur, classified_by) "
        "VALUES(?,?,?,?,?,?,?,?,'rules')",
        (norm, name.strip(), c.category, c.storage_zone, int(c.is_food),
         shelf_life, parse_unit_size(name), unit_price),
    )
    return cur.lastrowid, True


def _expiry_for(conn, product_id: int, delivery_date: str) -> Optional[str]:
    """expiry = delivery_date + effective shelf life. Pure arithmetic, always."""
    row = conn.execute(
        "SELECT category, shelf_life_days, is_food FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if not row or not row["is_food"]:
        return None
    days = learning.effective_days(conn, row["category"], row["shelf_life_days"])
    if not days or not delivery_date:
        return None
    try:
        base = date.fromisoformat(delivery_date)
    except ValueError:
        return None
    return (base + timedelta(days=int(days))).isoformat()


def store_order(conn, order: Order, delivery_date: str, products: Iterable[Product],
                is_historical: bool = False, result: Optional[ImportResult] = None) -> ImportResult:
    result = result or ImportResult()

    existing = conn.execute("SELECT code FROM orders WHERE code = ?", (order.code,)).fetchone()
    if existing:
        result.orders_skipped += 1
        return result

    delivery = delivery_date or order.order_date
    products = list(products)
    total = sum(p.total_price_eur or 0 for p in products)

    conn.execute(
        "INSERT INTO orders(code, service, order_date, delivery_date, total_eur, "
        "fetched_at, is_historical) VALUES(?,?,?,?,?,?,?)",
        (order.code, order.service, order.order_date, delivery, round(total, 2),
         datetime.now().isoformat(timespec="seconds"), int(is_historical)),
    )
    result.orders_added += 1
    result.codes.append(order.code)

    status = "archived" if is_historical else "in_stock"
    for p in products:
        product_id, created = upsert_product(conn, p.name, p.unit_price_eur)
        result.products_created += int(created)

        # Same product twice in one order: merge quantities rather than fail the
        # (order_code, product_id) uniqueness constraint.
        row = conn.execute(
            "SELECT id, qty_initial FROM lots WHERE order_code = ? AND product_id = ?",
            (order.code, product_id),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE lots SET qty_initial = qty_initial + ?, qty_remaining = qty_remaining + ? "
                "WHERE id = ?",
                (p.qty, p.qty, row["id"]),
            )
            continue

        conn.execute(
            "INSERT INTO lots(product_id, order_code, qty_initial, qty_remaining, "
            "unit_price_eur, delivery_date, expiry_date, status) VALUES(?,?,?,?,?,?,?,?)",
            (product_id, order.code, p.qty, p.qty if status == "in_stock" else 0,
             p.unit_price_eur, delivery, _expiry_for(conn, product_id, delivery), status),
        )
        result.lots_added += 1

    return result


def backfill_csv(conn, csv_path: Path, live_from: Optional[str] = None) -> ImportResult:
    """Seed from the parent project's orders.csv.

    Orders delivered before `live_from` are archived history. With no cutoff
    given, every order in the file is treated as history — the file is an export
    of the past, and inventory starts accumulating from the next live import.
    """
    result = ImportResult()
    if not csv_path.exists():
        log.warning("no history file at %s", csv_path)
        return result

    grouped: dict[str, dict] = {}
    with csv_path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = row["order_code"]
            g = grouped.setdefault(code, {
                "order": Order(code=code, service="history", order_date=row["order_date"]),
                "delivery": row["delivery_date"],
                "products": [],
            })
            g["products"].append(Product(
                name=row["product"],
                qty=parse_qty(row["qty"]),
                unit_price_eur=parse_price(row["unit_price"]),
                total_price_eur=parse_price(row["total_price"]),
            ))

    for code, g in sorted(grouped.items(), key=lambda kv: kv[1]["delivery"]):
        delivery = g["delivery"] or g["order"].order_date
        historical = True if live_from is None else (delivery < live_from)
        store_order(conn, g["order"], delivery, g["products"],
                    is_historical=historical, result=result)
    return result


def import_live(conn, session, only_last: bool = True) -> ImportResult:
    """Fetch from my.conad.it. By default only the most recent order."""
    result = ImportResult()
    orders = conad_api.fetch_orders(session)
    if not orders:
        log.warning("no orders returned by Conad")
        return result

    for order in (orders[:1] if only_last else orders):
        if conn.execute("SELECT 1 FROM orders WHERE code = ?", (order.code,)).fetchone():
            result.orders_skipped += 1
            continue
        delivery, products = conad_api.fetch_products(session, order.code)
        store_order(conn, order, delivery, products, is_historical=False, result=result)
        log.info("imported order %s: %d products", order.code, len(products))

    return result


def reclassify_unmatched(conn) -> int:
    """Re-run the rule table over rule-classified products.

    Used after the rules are edited. `user` corrections are never touched.
    """
    rows = conn.execute(
        "SELECT id, display_name, category FROM products WHERE classified_by = 'rules'"
    ).fetchall()
    changed = 0
    for row in rows:
        c = classify(row["display_name"])
        if c.category == row["category"]:
            continue
        learning.ensure_category(conn, c.category, c.shelf_life_days)
        conn.execute(
            "UPDATE products SET category = ?, storage_zone = ?, is_food = ?, shelf_life_days = ? "
            "WHERE id = ?",
            (c.category, c.storage_zone, int(c.is_food),
             learning.effective_days(conn, c.category, c.shelf_life_days), row["id"]),
        )
        changed += 1
    return changed
