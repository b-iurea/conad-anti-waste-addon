"""FastAPI app: JSON API + the static dashboard.

Routes are sync — FastAPI runs them in a threadpool, which suits SQLite fine and
keeps the code obvious. No CORS: the dashboard is served from the same origin.
"""

import logging
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import auth_auto, db, deals, importer, inventory, meals, stats, velocity
from app.config import get_settings

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="conad-anti-waste", docs_url="/api/docs", redoc_url=None)


def get_conn():
    conn = db.connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@app.on_event("startup")
def _startup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    log.info("database ready at %s", get_settings().db_path)


# --- models ---------------------------------------------------------------

class EventIn(BaseModel):
    kind: str = Field(pattern="^(consumed|wasted|already_bad|adjust)$")
    qty: float = 1.0
    happened_on: Optional[str] = None
    source: str = "dashboard"


class ExpiryIn(BaseModel):
    date: Optional[str] = None
    extend_days: Optional[int] = None


class ProductPatch(BaseModel):
    category: Optional[str] = None
    storage_zone: Optional[str] = Field(default=None, pattern="^(frigo|freezer|dispensa|non_food)$")
    shelf_life_days: Optional[int] = Field(default=None, ge=1, le=730)


class MealIn(BaseModel):
    name: str
    ingredients: list[dict] = []


# --- health & dashboard ----------------------------------------------------

@app.get("/api/health")
def health(conn=Depends(get_conn)):
    s = get_settings()
    counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
              for t in ("orders", "products", "lots", "events")}
    last_import = conn.execute("SELECT MAX(fetched_at) f FROM orders").fetchone()["f"]
    return {
        "ok": True,
        "db": counts,
        "last_import": last_import,
        "session": auth_auto.session_status(),
        "intelligence": s.intelligence,
        "compliance": stats.logging_compliance(conn),
    }


@app.post("/api/login")
def post_login(force: bool = True):
    """Re-authenticate against Conad now. Blocks for up to a minute or two."""
    try:
        auth_auto.ensure_session(force_login=force)
    except auth_auto.SessionUnavailable as e:
        raise HTTPException(502, str(e)) from e
    return {"ok": True, "session": auth_auto.session_status()}


@app.get("/")
def index():
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return JSONResponse({"detail": "dashboard not built"}, status_code=404)
    return FileResponse(page)


# --- inventory -------------------------------------------------------------

@app.get("/api/inventory")
def get_inventory(zone: Optional[str] = None, include_non_food: bool = True,
                  conn=Depends(get_conn)):
    items = inventory.current_stock(conn, zone=zone, include_non_food=include_non_food)
    return {
        "items": items,
        "counts": {
            "total": len(items),
            "expired": sum(1 for i in items if (i["days_left"] or 0) < 0 and i["days_left"] is not None),
            "expiring": sum(1 for i in items
                            if i["days_left"] is not None and 0 <= i["days_left"] <= 3),
        },
    }


@app.get("/api/inventory/expiring")
def get_expiring(days: int = Query(3, ge=0, le=60), conn=Depends(get_conn)):
    return {"items": inventory.expiring_soon(conn, days)}


@app.get("/api/inventory/overdue")
def get_overdue(conn=Depends(get_conn)):
    return {"items": inventory.overdue(conn)}


@app.post("/api/lots/{lot_id}/event")
def post_event(lot_id: int, payload: EventIn, conn=Depends(get_conn)):
    try:
        lot = inventory.log_event(conn, lot_id, payload.kind, payload.qty,
                                  payload.happened_on, payload.source)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    return {"lot": lot}


@app.post("/api/lots/{lot_id}/expiry")
def post_expiry(lot_id: int, payload: ExpiryIn, conn=Depends(get_conn)):
    try:
        if payload.extend_days is not None:
            lot = inventory.extend_expiry(conn, lot_id, payload.extend_days)
        elif payload.date:
            lot = inventory.set_expiry(conn, lot_id, payload.date)
        else:
            raise HTTPException(400, "provide either `date` or `extend_days`")
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, f"invalid date: {e}") from e
    return {"lot": lot}


@app.get("/api/review-queue")
def get_review_queue(conn=Depends(get_conn)):
    return {"items": inventory.review_queue(conn)}


@app.patch("/api/products/{product_id}")
def patch_product(product_id: int, payload: ProductPatch, conn=Depends(get_conn)):
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "nothing to update")
    # A human correction is authoritative and must survive every re-import and
    # every future LLM pass.
    fields["classified_by"] = "user"
    if "storage_zone" in fields:
        fields["is_food"] = int(fields["storage_zone"] != "non_food")
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE products SET {sets} WHERE id = ?", (*fields.values(), product_id))
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such product")
    return {"product": dict(row)}


# --- shopping list ---------------------------------------------------------

@app.get("/api/shopping-list")
def get_shopping_list(conn=Depends(get_conn)):
    # Priced when catalogue data exists, plain otherwise — the list must still
    # work on a database that has never scraped prices.
    return deals.price_shopping_list(conn)


# --- prices ----------------------------------------------------------------

@app.get("/api/deals")
def get_deals(conn=Depends(get_conn)):
    return deals.find(conn)


# --- meals -----------------------------------------------------------------

@app.get("/api/meals")
def get_meals(conn=Depends(get_conn)):
    return {"meals": meals.list_meals(conn), "cookable": meals.cookable_now(conn)}


@app.post("/api/meals")
def post_meal(payload: MealIn, conn=Depends(get_conn)):
    meal_id = meals.create_meal(conn, payload.name, payload.ingredients)
    return {"meal": meals.get_meal(conn, meal_id)}


@app.delete("/api/meals/{meal_id}")
def delete_meal(meal_id: int, conn=Depends(get_conn)):
    meals.delete_meal(conn, meal_id)
    return {"ok": True}


@app.post("/api/meals/{meal_id}/log")
def log_meal(meal_id: int, happened_on: Optional[str] = Body(None, embed=True),
             conn=Depends(get_conn)):
    try:
        return meals.log_meal(conn, meal_id, happened_on)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e


# --- stats -----------------------------------------------------------------

@app.get("/api/stats")
def get_stats(conn=Depends(get_conn)):
    return stats.full(conn)


# --- import ----------------------------------------------------------------

def _run_import(conn, only_last: bool = True) -> dict:
    # ensure_session logs in by itself if the cookies have died, so a cluster
    # deployment never needs a human with a browser.
    session = auth_auto.ensure_session()
    result = importer.import_live(conn, session, only_last=only_last)
    payload = {
        "orders_added": result.orders_added,
        "orders_skipped": result.orders_skipped,
        "lots_added": result.lots_added,
        "codes": result.codes,
    }
    if result.codes:
        payload["overbought"] = velocity.overbought(conn, result.codes[0])
    return payload


@app.post("/api/import")
def post_import(all_orders: bool = False, conn=Depends(get_conn)):
    try:
        return _run_import(conn, only_last=not all_orders)
    except Exception as e:  # noqa: BLE001
        # A dead Conad session must be loud, never a quietly empty import.
        log.exception("import failed")
        raise HTTPException(502, f"import failed: {e}") from e


@app.post("/api/webhook/scan")
def webhook_scan(x_scan_token: str = Header(default=""), conn=Depends(get_conn)):
    s = get_settings()
    if not s.scan_token or x_scan_token != s.scan_token:
        raise HTTPException(401, "invalid or missing X-Scan-Token")
    try:
        return _run_import(conn, only_last=True)
    except Exception as e:  # noqa: BLE001
        log.exception("webhook scan failed")
        raise HTTPException(502, f"scan failed: {e}") from e


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
