"""Price drops, filtered through what you actually need.

This module exists to answer one question — "is this worth buying *now*?" — and
it is deliberately as willing to say no as yes.

A plain discount feed would work against the rest of this app. Buying more is
what creates waste, so a price drop is only good news when three things hold at
once: you are due to rebuy it, you are not already stocked, and you can get
through it before it goes bad. Anything failing the last two lands in
`da_ignorare` with the reason spelled out, because "you have three already" is
more valuable than a coupon.

The discount is measured against your own history, not a promo badge. Conad
shows no promotion data without a delivery address selected, so `catalog_price`
accumulates observations and a drop means "cheaper than we have recorded", with
what you last paid as the second reference point.
"""

import sqlite3
from datetime import date
from typing import Optional

from app import velocity

MIN_DROP_PCT = 5.0        # below this it is noise, not an offer
MIN_OBSERVATIONS = 2      # one sighting establishes no reference price
STOCKPILE_CAP = 4         # never suggest more than this many units at once


def _reference_price(conn: sqlite3.Connection, code: str,
                     today: Optional[date] = None) -> Optional[dict]:
    """The price to compare today's against: the usual (median-ish) recent one.

    Uses the highest price seen in the trailing history rather than the mean,
    so a long-running low price does not slowly redefine itself as a discount.
    """
    today = (today or date.today()).isoformat()
    rows = conn.execute(
        "SELECT observed_on, price_eur FROM catalog_price "
        "WHERE code = ? ORDER BY observed_on DESC LIMIT 30",
        (code,),
    ).fetchall()
    if len(rows) < MIN_OBSERVATIONS:
        return None
    current = next((r["price_eur"] for r in rows if r["observed_on"] == today), None)
    if current is None:
        current = rows[0]["price_eur"]
    prior = [r["price_eur"] for r in rows if r["observed_on"] != today and r["price_eur"]]
    if not prior:
        return None
    return {"current": current, "usual": max(prior)}


def _drop_pct(current: Optional[float], usual: Optional[float]) -> float:
    if not current or not usual or usual <= 0:
        return 0.0
    return round((usual - current) / usual * 100, 1)


def find(conn: sqlite3.Connection, today: Optional[date] = None) -> dict:
    """Split every priced, linked product into buy / stock up / ignore."""
    today = today or date.today()
    horizon = velocity.order_gap_days(conn)
    rates = velocity.consumption_rates(conn, today)
    stock = velocity.product_stock(conn)

    rows = conn.execute(
        """
        SELECT p.id, p.display_name, p.category, p.is_food, p.shelf_life_days,
               p.last_price_eur, c.code, c.price_eur, c.bassi_fissi, c.brand
        FROM products p
        JOIN catalog c ON c.code = p.catalog_code
        WHERE c.price_eur > 0
        """
    ).fetchall()

    occasioni, scorta, ignora = [], [], []

    for r in rows:
        ref = _reference_price(conn, r["code"], today)
        current = ref["current"] if ref else r["price_eur"]
        drop = _drop_pct(current, ref["usual"]) if ref else 0.0

        # Second opinion: cheaper than you last actually paid for it.
        vs_paid = _drop_pct(current, r["last_price_eur"])

        if drop < MIN_DROP_PCT and vs_paid < MIN_DROP_PCT:
            continue

        pid = r["id"]
        rate = rates.get(pid, {})
        per_week = rate.get("per_week", 0.0)
        trusted = rate.get("trusted", False)
        qty = (stock.get(pid, {}) or {}).get("qty", 0) or 0
        days_cover = (qty / (per_week / 7)) if (trusted and per_week > 0) else None

        entry = {
            "product_id": pid,
            "display_name": r["display_name"],
            "category": r["category"],
            "brand": r["brand"],
            "price_eur": current,
            "usual_eur": ref["usual"] if ref else None,
            "last_paid_eur": r["last_price_eur"],
            "drop_pct": max(drop, vs_paid),
            "bassi_fissi": bool(r["bassi_fissi"]),
            "qty_in_stock": qty,
            "days_cover": round(days_cover) if days_cover is not None else None,
        }

        # Gate 1 — anti-rebuy. Having it beats any discount.
        if days_cover is not None and days_cover >= horizon:
            entry["reason"] = (f"ne hai {qty:g}, ti bastano ~{round(days_cover)}g "
                               f"— non serve, anche se costa meno")
            ignora.append(entry)
            continue
        if days_cover is None and qty >= 2:
            entry["reason"] = f"ne hai già {qty:g} in casa"
            ignora.append(entry)
            continue

        # Gate 2 — no measured habit, no advice. Same rule as the shopping list:
        # a confident wrong suggestion costs more trust than a missing one.
        if not trusted:
            entry["reason"] = "storico insufficiente per consigliare una scorta"
            ignora.append(entry)
            continue

        needed = max(1, round(per_week / 7 * horizon - qty))

        # Gate 3 — the anti-waste one. Buying ahead only makes sense for as much
        # as you can finish before it turns. Non-food does not spoil, so it is
        # capped by sense rather than by shelf life.
        if r["is_food"]:
            shelf = r["shelf_life_days"] or 0
            consumable = int(shelf * per_week / 7) if shelf else 0
            if consumable < 1:
                entry["reason"] = (f"scade in ~{shelf}g e ne usi ~{per_week:g}/sett "
                                   f"— fare scorta significa buttarla")
                ignora.append(entry)
                continue
            suggested = min(needed, consumable, STOCKPILE_CAP)
        else:
            suggested = min(max(needed, 2), STOCKPILE_CAP)

        entry["suggested_qty"] = suggested
        entry["saving_eur"] = round((entry["usual_eur"] or r["last_price_eur"] or current)
                                    * suggested - current * suggested, 2)

        if qty <= 0 or (days_cover is not None and days_cover < horizon):
            entry["reason"] = (f"-{entry['drop_pct']:g}% · ne usi ~{per_week:g}/sett · "
                               + (f"finito" if qty <= 0 else f"ti resta ~{round(days_cover)}g"))
            occasioni.append(entry)
        else:
            entry["reason"] = f"-{entry['drop_pct']:g}% · scorta sensata, non scade"
            scorta.append(entry)

    occasioni.sort(key=lambda e: -e["drop_pct"])
    scorta.sort(key=lambda e: -e["drop_pct"])
    ignora.sort(key=lambda e: -e["drop_pct"])
    return {
        "horizon_days": horizon,
        "occasioni": occasioni,
        "scorta": scorta,
        "da_ignorare": ignora,
        "totale_risparmio_eur": round(sum(e.get("saving_eur") or 0 for e in occasioni), 2),
    }


def price_shopping_list(conn: sqlite3.Connection, today: Optional[date] = None) -> dict:
    """The existing shopping list, with current prices attached.

    Prices go where you already act, rather than in a separate tab you have to
    remember to open.
    """
    sl = velocity.shopping_list(conn, today)
    prices = {
        r["id"]: {"price_eur": r["price_eur"], "code": r["code"]}
        for r in conn.execute(
            "SELECT p.id, c.code, c.price_eur FROM products p "
            "JOIN catalog c ON c.code = p.catalog_code WHERE c.price_eur IS NOT NULL"
        )
    }
    total = 0.0
    for entry in sl.get("serve", []):
        hit = prices.get(entry["product_id"])
        if not hit:
            continue
        entry["price_eur"] = hit["price_eur"]
        total += hit["price_eur"]
    sl["stima_totale_eur"] = round(total, 2)
    return sl
