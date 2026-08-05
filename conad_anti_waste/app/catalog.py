"""spesaonline.conad.it catalogue and price tracking.

The same SSO as my.conad.it: the cookie file is a flat name/value map, so the
saved session works on this host too — verified against the live site, which
returned this account's wishlist over an authenticated POST.

Each product card carries its whole record in a `data-product` attribute as
JSON, so this is a stable attribute read rather than markup scraping:

    {"code":"311695","nome":"Bocconi con Manzo 1250 g Conad","marchio":"CONAD",
     "basePrice":1.79,"netQuantity":1.25,"netQuantityUm":"KG","bassiFissi":true,
     "categoriaPrimoLivello":"Animali domestici", ...}

What is deliberately NOT here: Conad's promotion badges. Without a delivery
address and service selected, every `badges-potential-discount` slot on the
page is empty and the payload carries only `basePrice` — measured, not assumed.
Selecting a store means driving a Google Places autocomplete and writing
session state on the real account, so instead we record prices over time and
call a drop a drop. That comparison is also the more useful one: it is against
what *you* paid, not against a shelf price you never saw.
"""

import html
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from typing import Iterator, Optional

import requests

from app.classify import normalize_name

BASE = "https://spesaonline.conad.it"
SEARCH_URL = BASE + "/search?query={query}&page={page}"
LISTING_URL = BASE + "/{path}?page={page}"

PAGE_SIZE_HINT = 30       # listings return 30/page, search 40
MAX_PAGES = 40

# Be a polite guest; this is someone else's server. 1.2s was measurably too
# fast — a 158-search run was answered with 429 partway through. This pace plus
# the backoff below keeps a full refresh under the limit, at the cost of it
# being a nightly job rather than something to run interactively.
REQUEST_PAUSE = 3.0
BACKOFF_SECONDS = (30, 90, 240)   # then give up and leave the rest for tomorrow
QUERY_TOKENS = 3                  # broader query => more candidates per request

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9",
    "Referer": BASE + "/",
}

_CARD_RE = re.compile(r'data-product="([^"]+)"')


class CatalogError(RuntimeError):
    """The page loaded but held no product cards — markup or auth changed."""


class RateLimited(RuntimeError):
    """Conad asked us to slow down and we have already backed off enough.

    Raised rather than swallowed so callers stop instead of hammering: a
    partial refresh that resumes tomorrow is fine, a banned session is not.
    """


@dataclass
class CatalogItem:
    code: str
    display_name: str
    norm_name: str
    brand: str
    cat1: str
    cat2: str
    cat3: str
    net_qty: Optional[float]
    net_qty_um: str
    bassi_fissi: bool
    price_eur: Optional[float]


def build_session(cookies_path) -> requests.Session:
    """A requests session carrying the saved Conad cookies.

    Not reusing auth.ConadHttpSession because that one pins Referer/Origin to
    my.conad.it, which is the wrong host for these requests.
    """
    s = requests.Session()
    s.headers.update(HEADERS)
    with open(cookies_path, "r", encoding="utf-8") as f:
        for k, v in json.load(f).items():
            s.cookies.set(k, v, domain=".conad.it")
    return s


def parse_cards(page_html: str) -> list[CatalogItem]:
    """Pull every product card out of a listing page."""
    items = []
    for m in _CARD_RE.finditer(page_html):
        try:
            d = json.loads(html.unescape(m.group(1)))
        except json.JSONDecodeError:
            continue
        name = (d.get("nome") or "").strip()
        code = str(d.get("code") or "").strip()
        if not name or not code:
            continue
        items.append(CatalogItem(
            code=code,
            display_name=name,
            norm_name=normalize_name(name),
            brand=(d.get("marchio") or "").strip(),
            cat1=(d.get("categoriaPrimoLivello") or "").strip(),
            cat2=(d.get("categoriaSecondoLivello") or "").strip(),
            cat3=(d.get("categoriaTerzoLivello") or "").strip(),
            net_qty=d.get("netQuantity"),
            net_qty_um=(d.get("netQuantityUm") or "").strip(),
            bassi_fissi=bool(d.get("bassiFissi")),
            # basePrice is absent on some cards and 0 on most of them when no
            # store is selected. Both mean "unknown", and both must stay None:
            # a stored 0.00 would read as free and compute a 100% discount.
            price_eur=(d.get("basePrice") or None),
        ))
    return items


def _get(session: requests.Session, url: str) -> requests.Response:
    """GET with backoff on 429, because the site does throttle and we comply."""
    for wait in BACKOFF_SECONDS:
        r = session.get(url, timeout=30)
        if r.status_code != 429:
            r.raise_for_status()
            return r
        time.sleep(wait)
    raise RateLimited(f"still rate-limited after {len(BACKOFF_SECONDS)} backoffs: {url}")


def short_query(name: str) -> str:
    """The first few identifying words of a product name.

    Searching the *whole* name returns one or two results, which is too narrow
    to match against — the catalogue's wording drifts from the order history's.
    A three-token query returns a proper candidate list, and several products
    collapse onto the same query, so it is also fewer requests.
    """
    toks = [t for t in re.split(r"[^\w]+", name.lower()) if t and t not in _STOPWORDS]
    return " ".join(toks[:QUERY_TOKENS]) or name


def search(session: requests.Session, query: str, page: int = 1) -> list[CatalogItem]:
    r = _get(session, SEARCH_URL.format(query=requests.utils.quote(query), page=page))
    return parse_cards(r.text)


def iter_listing(session: requests.Session, path: str,
                 max_pages: int = MAX_PAGES) -> Iterator[CatalogItem]:
    """Walk a paginated listing such as `bassi-e-fissi`, stopping at repeats.

    The site answers an out-of-range page with the *first* page rather than an
    empty one, so "same codes as last time" is the only reliable end signal.
    """
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        r = _get(session, LISTING_URL.format(path=path.strip("/"), page=page))
        items = parse_cards(r.text)
        if not items:
            if page == 1:
                raise CatalogError(f"no product cards on /{path} — markup or auth changed")
            return
        fresh = [i for i in items if i.code not in seen]
        if not fresh:
            return
        for i in fresh:
            seen.add(i.code)
            yield i
        time.sleep(REQUEST_PAUSE)


# --- persistence -----------------------------------------------------------

def upsert(conn: sqlite3.Connection, items: list[CatalogItem],
           today: Optional[date] = None) -> int:
    """Store catalogue rows and append today's price observation."""
    today = (today or date.today()).isoformat()
    n = 0
    for it in items:
        conn.execute(
            """
            INSERT INTO catalog(code, norm_name, display_name, brand, cat1, cat2, cat3,
                                net_qty, net_qty_um, bassi_fissi, price_eur,
                                first_seen, last_seen)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                norm_name=excluded.norm_name, display_name=excluded.display_name,
                brand=excluded.brand, cat1=excluded.cat1, cat2=excluded.cat2,
                cat3=excluded.cat3, net_qty=excluded.net_qty,
                net_qty_um=excluded.net_qty_um, bassi_fissi=excluded.bassi_fissi,
                price_eur=excluded.price_eur, last_seen=excluded.last_seen
            """,
            (it.code, it.norm_name, it.display_name, it.brand, it.cat1, it.cat2, it.cat3,
             it.net_qty, it.net_qty_um, int(it.bassi_fissi), it.price_eur, today, today),
        )
        if it.price_eur is not None:
            # One observation per product per day: re-running the scrape must
            # not manufacture extra history.
            conn.execute(
                "INSERT INTO catalog_price(code, observed_on, price_eur) VALUES(?,?,?) "
                "ON CONFLICT(code, observed_on) DO UPDATE SET price_eur=excluded.price_eur",
                (it.code, today, it.price_eur),
            )
        n += 1
    return n


# Words that appear everywhere and so carry no identifying signal. Matching on
# them alone would pair any two Conad products.
_STOPWORDS = {
    "conad", "di", "e", "con", "al", "alla", "il", "la", "lo", "i", "gli", "le",
    "da", "del", "della", "dei", "in", "a", "per", "cat", "cal", "origine",
    "percorso", "qualita", "sapori", "dintorni", "italia", "g", "kg", "ml", "l",
    "cl", "pz", "x",
}

_SIZE_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(g|kg|ml|cl|l)\b")

MIN_FUZZY_SCORE = 0.8


def _tokens(norm: str) -> set[str]:
    return {t for t in re.split(r"[^\w%]+", norm) if t and t not in _STOPWORDS}


def _size(norm: str) -> Optional[tuple[float, str]]:
    m = _SIZE_RE.search(norm)
    if not m:
        return None
    return float(m.group(1).replace(",", ".")), m.group(2)


def match_score(a_norm: str, b_norm: str) -> float:
    """How confident are we that two names are the same product?

    Symmetric coverage: the overlap must explain most of BOTH names. One-sided
    containment looks appealing (catalogue names carry extra marketing words)
    but is badly wrong in the other direction — "Prosciutto cotto di alta
    qualità" is entirely contained in "Teneroni Tortino di Patate con
    Prosciutto Cotto di Alta Qualità e Grana Padano", which scored a perfect
    1.0 and priced a potato bake as a pack of ham. Requiring both sides to be
    explained costs some recall and removes that whole class of error.

    A differing pack size vetoes the match outright: 500 ml and 1 l of the same
    thing are different products at different prices, and pricing one as the
    other is precisely the silent error worth refusing.
    """
    ta, tb = _tokens(a_norm), _tokens(b_norm)
    if not ta or not tb:
        return 0.0
    sa, sb = _size(a_norm), _size(b_norm)
    if sa and sb and sa != sb:
        return 0.0
    shared = len(ta & tb)
    return min(shared / len(ta), shared / len(tb))


def link_products(conn: sqlite3.Connection, fuzzy: bool = True) -> dict:
    """Attach catalogue codes to the products you have bought.

    Exact normalised-name match first — that is the only one treated as fact.
    Your order history and today's catalogue drift apart (products get delisted,
    renamed, or re-graded), so an exact-only pass leaves most fresh produce and
    third-party brands unpriced. The fuzzy pass closes that gap but records
    itself as a guess, and a size mismatch always vetoes it.
    """
    exact = fuzzed = 0
    rows = conn.execute(
        "SELECT id, norm_name FROM products WHERE catalog_code IS NULL"
    ).fetchall()

    for r in rows:
        hit = conn.execute(
            "SELECT code FROM catalog WHERE norm_name = ? LIMIT 1", (r["norm_name"],)
        ).fetchone()
        if hit:
            conn.execute(
                "UPDATE products SET catalog_code = ?, catalog_match = 'exact' WHERE id = ?",
                (hit["code"], r["id"]))
            exact += 1
            continue

        if not fuzzy:
            continue

        best, best_score = None, 0.0
        for cand in conn.execute("SELECT code, norm_name FROM catalog"):
            score = match_score(r["norm_name"], cand["norm_name"])
            if score > best_score:
                best, best_score = cand["code"], score
        if best and best_score >= MIN_FUZZY_SCORE:
            conn.execute(
                "UPDATE products SET catalog_code = ?, catalog_match = 'fuzzy' WHERE id = ?",
                (best, r["id"]))
            fuzzed += 1

    return {"exact": exact, "fuzzy": fuzzed}


def refresh_for_products(conn: sqlite3.Connection, session: requests.Session,
                         limit: Optional[int] = None,
                         today: Optional[date] = None) -> dict:
    """Look up current prices for the things you actually buy.

    Targeted rather than crawling the whole shop: one search per product is a
    few dozen requests, where a full catalogue walk is thousands. It also keeps
    the price history focused on rows we will ever ask a question about.
    """
    rows = conn.execute(
        "SELECT id, display_name, norm_name FROM products ORDER BY id"
    ).fetchall()

    # Several products share a query once it is cut to its first few words
    # ("pomodoro cuore bue" covers every grade of the same tomato), so dedupe
    # before hitting the network.
    queries: dict[str, list] = {}
    for r in rows:
        queries.setdefault(short_query(r["display_name"]), []).append(r)
    ordered = list(queries)
    if limit:
        ordered = ordered[:limit]

    stored = 0
    stopped_early = False
    for q in ordered:
        try:
            items = search(session, q)
        except RateLimited:
            # Keep what we have. The rest is picked up on the next run rather
            # than pushed through a limit the site has already enforced once.
            stopped_early = True
            break
        except requests.RequestException:
            continue
        stored += upsert(conn, items, today=today)
        conn.commit()      # a later 429 must not discard the pages already paid for
        time.sleep(REQUEST_PAUSE)

    linked = link_products(conn)
    return {"products": len(rows), "queries": len(ordered), "stored": stored,
            "linked_exact": linked["exact"], "linked_fuzzy": linked["fuzzy"],
            "stopped_early": stopped_early}
