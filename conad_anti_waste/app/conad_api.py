"""my.conad.it order fetching.

The card parser is carried over from the parent project's export_orders_csv.py,
which is already proven against the real site. Two sources:

  order list   -> .../mf5_orders.jloader.loader-{svc}.json  (JSON wrapping HTML)
  order detail -> /dettaglio-ordine?code=...                (server-rendered HTML)

A parse that yields zero products from a non-empty page is treated as an error,
never as an empty order — a silent import is how an inventory quietly stops
being true.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Optional

ORDERS_LIST_URL = (
    "https://my.conad.it/i-miei-ordini/_jcr_content/root/ml2_navlayout/"
    "mf5_orders.jloader.loader-{svc}.json"
    "?filter=ALL&pageNumber={pn}&pageSize=100&currentSize={cs}"
)
DETAIL_URL = "https://my.conad.it/dettaglio-ordine?code={code}&bEcommerce=SAP"
SERVICES = ("homedelivery", "ordercollect")


class ConadParseError(RuntimeError):
    """The page loaded but did not look like what we expect — markup changed."""


@dataclass
class Order:
    code: str
    service: str
    order_date: str          # ISO date
    delivery_date: str = ""  # ISO date, filled in from the detail page


@dataclass
class Product:
    name: str
    qty: float
    unit_price_eur: Optional[float]
    total_price_eur: Optional[float]


class _CardParser(HTMLParser):
    """Extracts product cards from a /dettaglio-ordine page (stdlib only)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards: list[dict] = []
        self._cur = None
        self._depth = 0
        self._field = None
        self._buf = []

    @staticmethod
    def _cls(attrs) -> str:
        return dict(attrs).get("class", "")

    def handle_starttag(self, tag, attrs):
        if tag == "div" and "mt24-product-accordion-card" in self._cls(attrs) and self._cur is None:
            self._cur = {"name": None, "qty": "", "unit": "", "total": ""}
            self._depth = 1
            return
        if self._cur is not None:
            if tag == "div":
                self._depth += 1
            if tag == "img" and "assets/products" in (dict(attrs).get("src") or "") and not self._cur["name"]:
                self._cur["name"] = (dict(attrs).get("alt") or "").strip()
            cls = self._cls(attrs)
            for frag, key in (("__dQuantityFormat", "qty"), ("__dTotalPrice", "total"),
                              ("__dUpOriginal", "unit")):
                if frag in cls and self._field is None:
                    self._field, self._buf = key, []

    def handle_data(self, data):
        if self._field:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._cur is None:
            return
        if tag == "div":
            self._depth -= 1
            if self._depth == 0:
                self.cards.append(self._cur)
                self._cur, self._field = None, None
            elif self._field:
                self._flush()
        elif tag == "span" and self._field:
            self._flush()

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        if text:
            self._cur[self._field] = text
        self._field, self._buf = None, []


def parse_price(raw) -> Optional[float]:
    """'2,49 €' -> 2.49. Italian decimal comma, optional thousands dot."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d,.]", "", str(raw))
    if not cleaned:
        return None
    # 1.234,56 -> 1234.56 ; 2,49 -> 2.49
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def parse_qty(raw) -> float:
    digits = re.sub(r"\D", "", str(raw or ""))
    return float(digits) if digits else 1.0


def fetch_orders(session) -> list[Order]:
    """Every order across both services, newest first."""
    orders: list[Order] = []
    for svc in SERVICES:
        page = 0
        while True:
            url = ORDERS_LIST_URL.format(svc=svc, pn=page, cs=len(orders))
            resp = session.session.get(url)
            resp.raise_for_status()
            try:
                html = resp.json()["data"]["html"]
            except (ValueError, KeyError) as e:
                raise ConadParseError(f"unexpected order-list payload for {svc}: {e}") from e

            batch = []
            for m in re.findall(r"data-order='(\{.*?\})'", html, re.S):
                try:
                    raw = json.loads(m)
                except json.JSONDecodeError:
                    continue
                batch.append(Order(code=raw["code"], service=svc,
                                   order_date=_ts_to_date(raw.get("amendTimestamp"))))
            orders.extend(batch)
            if len(batch) < 100:
                break
            page += 1
    orders.sort(key=lambda o: o.order_date, reverse=True)
    return orders


def fetch_products(session, code: str) -> tuple[str, list[Product]]:
    """(delivery_date, products) for one order."""
    resp = session.session.get(DETAIL_URL.format(code=code))
    resp.raise_for_status()
    html = resp.text

    m = re.search(r'data-order-delivery-date="([^"]+)"', html)
    delivery = m.group(1) if m else ""

    parser = _CardParser()
    parser.feed(html)
    if not parser.cards:
        # An order with no products is not a thing. Either the session bounced us
        # to a login page or the markup changed; both must be loud.
        raise ConadParseError(
            f"no product cards found in order {code} "
            f"({len(html)} bytes) — session expired or page markup changed"
        )

    products = [
        Product(
            name=(c["name"] or "").strip(),
            qty=parse_qty(c["qty"]),
            unit_price_eur=parse_price(c["unit"]),
            total_price_eur=parse_price(c["total"]),
        )
        for c in parser.cards
        if (c["name"] or "").strip()
    ]
    return delivery, products


def get_last_order(session) -> Optional[Order]:
    orders = fetch_orders(session)
    return orders[0] if orders else None


def _ts_to_date(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts) / 1000, timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return ""
