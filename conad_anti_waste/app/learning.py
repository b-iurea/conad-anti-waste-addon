"""Asymmetric shelf-life learning.

The naive version of this loop records `consumed_date - delivery_date` for every
event and averages it. That learns how fast you eat, not how long food lasts:
eating yogurt on day 2 of a 20-day life would teach the system that yogurt lasts
2 days, and the estimate collapses toward your consumption speed.

The evidence is genuinely asymmetric:

  wasted at day N       the food WAS spoiled by N -> upper bound. The only
                        signal allowed to shorten an estimate.
  already_bad at day N  same, but observed directly rather than inferred. The
                        highest-quality signal we get, so the bot asks for it.
  consumed at day N     the food was STILL FINE at N -> lower bound. Can only
                        ever lengthen an estimate, and only if N exceeds it.

    effective = (upper_sum + prior * PRIOR_WEIGHT) / (upper_count + PRIOR_WEIGHT)
    effective = max(effective, lower_max)      # never claim shorter than proven
    effective = clamp(effective, 1, 730)

A category that has never been wasted keeps its prior forever, which is correct:
nothing has gone wrong, so there is nothing to learn.
"""

import sqlite3
from typing import Optional

PRIOR_WEIGHT = 3  # three real spoilage observations outweigh the initial guess
MIN_DAYS = 1
MAX_DAYS = 730

# Events that prove the food had already gone off.
UPPER_BOUND_KINDS = ("wasted", "already_bad")
# Events that prove it was still good. "still_good" is the explicit
# "ancora buono" affirmation; "consumed" is inferred and therefore weaker.
LOWER_BOUND_KINDS = ("consumed", "still_good")

# How far past the estimate an inferred "consumed" can go and still be believed.
#
# Clearing a backlog is not an observation. Tapping "Mangiato" on a salad from an
# order delivered 36 days ago means "this is resolved", not "we ate fresh salad
# on day 36" — and taking it literally taught every fresh category a 36-day
# shelf life at once, which would silence the expiry warnings entirely. Beyond a
# modest overshoot the date says nothing about the food's condition.
# The explicit "ancora buono" button bypasses this: there the user IS asserting
# the food is fine, which is exactly the observation we want.
MAX_LOWER_BOUND_FACTOR = 1.5


def ensure_category(conn: sqlite3.Connection, category: str, prior_days: Optional[int]) -> None:
    if prior_days is None:
        return
    conn.execute(
        "INSERT INTO shelf_life(category, prior_days) VALUES(?, ?) "
        "ON CONFLICT(category) DO UPDATE SET prior_days = COALESCE(shelf_life.prior_days, excluded.prior_days)",
        (category, prior_days),
    )


def effective_days(conn: sqlite3.Connection, category: str,
                   fallback: Optional[int] = None) -> Optional[int]:
    """Blend the prior with observed evidence. Returns None for non-food."""
    row = conn.execute(
        "SELECT prior_days, upper_sum, upper_count, lower_max FROM shelf_life WHERE category = ?",
        (category,),
    ).fetchone()

    if row is None or row["prior_days"] is None:
        return fallback
    prior = row["prior_days"]

    blended = (row["upper_sum"] + prior * PRIOR_WEIGHT) / (row["upper_count"] + PRIOR_WEIGHT)
    blended = max(blended, row["lower_max"] or 0)
    return int(round(max(MIN_DAYS, min(MAX_DAYS, blended))))


def record_observation(conn: sqlite3.Connection, category: str, kind: str,
                       observed_days: int) -> None:
    """Feed one event into the learning table.

    `observed_days` is always delivery_date -> event date, computed in code.
    The LLM never produces dates, only day counts, which is what keeps this
    arithmetic trustworthy.

    Only *surprises* are recorded — an observation that contradicts the current
    estimate. Confirmations carry no information and would bias the average:

      wasted at day 36 when we predicted 12
          The food was bad when you found it. It does not follow that it lasted
          36 days: you simply did not look sooner. Discovery time is arbitrary
          once expiry has passed, so averaging it in would drag the estimate
          UPWARD on evidence of spoilage — exactly backwards.
      wasted at day 4 when we predicted 12
          A genuine surprise. It really did spoil early. Learn from it.
      consumed at day 3 when we predicted 12
          Tells us nothing; we already believed it was fine on day 3.
      consumed at day 14 when we predicted 12
          A genuine surprise in the other direction. Learn from it.

    So both directions learn only where reality contradicted the model —
    symmetric in form, asymmetric in which way each kind of evidence can move
    the estimate.
    """
    if not category or observed_days is None or observed_days < 0:
        return
    observed_days = min(observed_days, MAX_DAYS)

    current = effective_days(conn, category)
    if current is None:
        return

    if kind in UPPER_BOUND_KINDS:
        if observed_days > current:
            return  # spoilage found after the predicted expiry: uninformative
        conn.execute(
            "UPDATE shelf_life SET upper_sum = upper_sum + ?, upper_count = upper_count + 1 "
            "WHERE category = ?",
            (observed_days, category),
        )
    elif kind in LOWER_BOUND_KINDS:
        if observed_days <= current:
            return  # we already believed it was fine that long
        if kind == "consumed" and observed_days > current * MAX_LOWER_BOUND_FACTOR:
            return  # backlog cleanup, not evidence the food was still good
        conn.execute(
            "UPDATE shelf_life SET lower_max = MAX(COALESCE(lower_max, 0), ?) WHERE category = ?",
            (observed_days, category),
        )


def repair_implausible_lower_bounds(conn: sqlite3.Connection) -> list[dict]:
    """Drop lower bounds that a backlog clear-out wrote before the cap existed.

    Keeps the event log intact — those taps really happened and the stock
    accounting from them is correct. Only the shelf-life inference is undone.
    """
    rows = conn.execute(
        "SELECT category, prior_days, lower_max FROM shelf_life "
        "WHERE lower_max > 0 AND prior_days IS NOT NULL"
    ).fetchall()

    repaired = []
    for row in rows:
        limit = row["prior_days"] * MAX_LOWER_BOUND_FACTOR
        if row["lower_max"] <= limit:
            continue
        conn.execute("UPDATE shelf_life SET lower_max = 0 WHERE category = ?", (row["category"],))
        repaired.append({
            "category": row["category"],
            "was": row["lower_max"],
            "now": effective_days(conn, row["category"]),
            "prior": row["prior_days"],
        })
        refresh_product_shelf_life(conn, row["category"])
    return repaired


def refresh_product_shelf_life(conn: sqlite3.Connection, category: str) -> None:
    """Push a re-learned estimate onto every product in the category.

    Only future lots pick up the new number; existing expiry dates stay put so
    the fridge view does not silently shift under the user.
    """
    days = effective_days(conn, category)
    if days is None:
        return
    conn.execute(
        "UPDATE products SET shelf_life_days = ? WHERE category = ? AND classified_by != 'user'",
        (days, category),
    )
