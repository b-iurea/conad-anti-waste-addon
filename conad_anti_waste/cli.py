#!/usr/bin/env python3
"""Dev and ops CLI.

    python cli.py doctor                 # environment / session / db check
    python cli.py backfill               # seed from the parent orders.csv
    python cli.py import [--all]         # fetch from my.conad.it
    python cli.py classify-report        # rule coverage over the catalogue
    python cli.py reclassify             # re-apply rules after editing them
    python cli.py inventory [--zone X]   # what is in stock right now
    python cli.py shopping-list          # serve / hai gia
    python cli.py reset --yes            # drop the database
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import db, importer
from app.classify import classify
from app.config import get_settings


def _session():
    from app import auth_auto
    return auth_auto.ensure_session()


def cmd_login(args) -> int:
    from app import auth_auto
    from app.conad_login import LoginError
    try:
        auth_auto.ensure_session(force_login=args.force)
    except (auth_auto.SessionUnavailable, LoginError) as e:
        print(f"[!] {e}")
        return 1
    print("[+] session OK")
    for k, v in auth_auto.session_status().items():
        print(f"    {k}: {v}")
    return 0


def cmd_doctor(args) -> int:
    s = get_settings()
    ok = True
    print(f"data dir       : {s.data_dir}")
    print(f"database       : {s.db_path} {'(exists)' if s.db_path.exists() else '(will be created)'}")

    db.init()
    with db.session() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
                  for t in ("orders", "products", "lots", "events", "meals")}
    print(f"tables         : " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    print(f"history csv    : {s.orders_csv_path} {'OK' if s.orders_csv_path.exists() else 'MISSING'}")

    import shutil

    from app import auth_auto

    print(f"conad login    : {'auto (email+password set)' if s.can_auto_login else 'NOT CONFIGURED'}")
    print(f"chrome profile : {s.profile_dir} {'(warm)' if s.profile_dir.exists() else '(new)'}")
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    print(f"browser        : {chrome or 'bundled Chromium (Chrome scores better)'}")
    xvfb = shutil.which("Xvfb")
    print(f"Xvfb           : {xvfb or 'MISSING — needed when there is no DISPLAY'}")
    if not s.can_auto_login:
        print("                 -> set CONAD_EMAIL / CONAD_PASSWORD in .env for unattended login")
        ok = False

    status = auth_auto.session_status() if args.online else {
        "cookies_present": s.conad_cookies_path.exists()}
    for k, v in status.items():
        print(f"  {k:22}: {v}")
    if args.online:
        ok = ok and status.get("authenticated", False)

    print(f"telegram token : {'set' if s.telegram_bot_token else 'NOT SET'}")
    print(f"allowed chats  : {sorted(s.allowed_chat_ids) or 'NONE — the bot will ignore everyone'}")
    print(f"intelligence   : {s.intelligence}")
    return 0 if ok else 1


def cmd_backfill(args) -> int:
    db.init()
    with db.session() as conn:
        r = importer.backfill_csv(conn, get_settings().orders_csv_path, live_from=args.live_from)
    print(f"orders added   : {r.orders_added}   (skipped, already present: {r.orders_skipped})")
    print(f"lots added     : {r.lots_added}")
    print(f"products created: {r.products_created}")
    return 0


def cmd_import(args) -> int:
    db.init()
    with db.session() as conn:
        r = importer.import_live(conn, _session(), only_last=not args.all)
    print(f"orders added   : {r.orders_added}   (skipped: {r.orders_skipped})")
    print(f"lots added     : {r.lots_added}   products created: {r.products_created}")
    for c in r.codes:
        print(f"  + {c}")
    return 0


def cmd_classify_report(args) -> int:
    db.init()
    with db.session() as conn:
        rows = conn.execute(
            "SELECT display_name, category, storage_zone, shelf_life_days, classified_by "
            "FROM products ORDER BY storage_zone, category, display_name"
        ).fetchall()
    if not rows:
        print("no products yet — run `python cli.py backfill` first")
        return 1

    unmatched = [r for r in rows if r["category"] == "sconosciuto"]
    by_zone = Counter(r["storage_zone"] for r in rows)

    if args.verbose:
        current = None
        for r in rows:
            key = (r["storage_zone"], r["category"])
            if key != current:
                current = key
                print(f"\n== {key[0]:9} {key[1]}")
            print(f"   {str(r['shelf_life_days'] or '-'):>5}d  {r['display_name'][:70]}")
        print()

    print(f"products       : {len(rows)}")
    for zone, n in by_zone.most_common():
        print(f"  {zone:10} {n}")
    print(f"unmatched      : {len(unmatched)} ({len(unmatched)/len(rows)*100:.1f}%)")
    for r in unmatched:
        print(f"  ? {r['display_name']}")
    return 0


def cmd_repair_learning(args) -> int:
    from app import learning
    db.init()
    with db.session() as conn:
        repaired = learning.repair_implausible_lower_bounds(conn)
    if not repaired:
        print("nothing to repair — all shelf-life estimates look plausible")
        return 0
    print(f"reset {len(repaired)} implausible estimates:")
    for r in repaired:
        print(f"  {r['category']:26} {r['was']}g -> {r['now']}g  (stima iniziale {r['prior']}g)")
    return 0


def cmd_reclassify(args) -> int:
    db.init()
    with db.session() as conn:
        n = importer.reclassify_unmatched(conn)
    print(f"reclassified   : {n} products (user corrections untouched)")
    return 0


def cmd_inventory(args) -> int:
    from app import inventory
    db.init()
    with db.session() as conn:
        items = inventory.current_stock(conn, zone=args.zone)
    if not items:
        print("inventory empty")
        return 0
    for it in items:
        days = it["days_left"]
        flag = "!!" if days is not None and days < 0 else ("!" if days is not None and days <= 2 else " ")
        left = f"{days:>4}g" if days is not None else "   -"
        print(f"{flag} {it['storage_zone']:9} {left}  x{it['qty_remaining']:<4g} {it['display_name'][:60]}")
    print(f"\n{len(items)} lots in stock")
    return 0


def cmd_shopping_list(args) -> int:
    from app import velocity
    db.init()
    with db.session() as conn:
        result = velocity.shopping_list(conn)
    print("SERVE")
    for r in result["serve"]:
        print(f"  {r['display_name'][:55]:<57} {r['reason']}")
    if not result["serve"]:
        print("  (niente)")
    print("\nHAI GIA — non comprare")
    for r in result["hai_gia"]:
        print(f"  {r['display_name'][:55]:<57} {r['reason']}")
    if not result["hai_gia"]:
        print("  (niente)")
    return 0


def cmd_prices(args) -> int:
    """Refresh catalogue prices for the products you buy."""
    from app import auth_auto, catalog
    db.init()
    auth_auto.ensure_session()
    session = catalog.build_session(get_settings().conad_cookies_path)
    with db.session() as conn:
        result = catalog.refresh_for_products(conn, session, limit=args.limit)
    print(f"prodotti      : {result['products']}")
    print(f"ricerche      : {result['queries']}")
    print(f"schede salvate: {result['stored']}")
    print(f"collegati     : {result['linked_exact']} esatti, {result['linked_fuzzy']} probabili")
    if result["stopped_early"]:
        print("\nfermato: Conad ha chiesto di rallentare (429). "
              "Il resto riparte al prossimo giro.")
    return 0


def cmd_deals(args) -> int:
    from app import deals
    db.init()
    with db.session() as conn:
        result = deals.find(conn)

    def show(title, rows):
        print(title)
        for r in rows:
            price = f"{r['price_eur']:.2f}€" if r["price_eur"] is not None else "—"
            print(f"  {r['display_name'][:48]:<50} {price:>8}  {r['reason']}")
        if not rows:
            print("  (niente)")

    show("OCCASIONI — ti servono e costano meno", result["occasioni"])
    print()
    show("SCORTA — non scadono, conviene", result["scorta"])
    print()
    show("DA IGNORARE — in offerta ma non ti servono", result["da_ignorare"])
    if result["totale_risparmio_eur"]:
        print(f"\nrisparmio stimato: {result['totale_risparmio_eur']:.2f}€")
    return 0


def cmd_reset(args) -> int:
    p = get_settings().db_path
    if not args.yes:
        print(f"this deletes {p} — pass --yes to confirm")
        return 1
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            f.unlink()
    print(f"deleted {p}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="conad-anti-waste CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="environment and session check")
    p.add_argument("--online", action="store_true", help="also verify the Conad session")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("login", help="authenticate against Conad (automatic)")
    p.add_argument("--force", action="store_true", help="log in even if the session looks valid")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("backfill", help="seed from the parent orders.csv")
    p.add_argument("--live-from", metavar="YYYY-MM-DD",
                   help="orders delivered on/after this date enter as live stock")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("import", help="fetch from my.conad.it")
    p.add_argument("--all", action="store_true", help="import every order, not just the last")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("classify-report", help="rule coverage over the catalogue")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_classify_report)

    sub.add_parser("reclassify", help="re-apply rules").set_defaults(func=cmd_reclassify)
    sub.add_parser("repair-learning",
                   help="reset shelf-life estimates inflated by backlog clear-outs"
                   ).set_defaults(func=cmd_repair_learning)

    p = sub.add_parser("inventory", help="what is in stock")
    p.add_argument("--zone", choices=["frigo", "freezer", "dispensa", "non_food"])
    p.set_defaults(func=cmd_inventory)

    sub.add_parser("shopping-list", help="serve / hai gia").set_defaults(func=cmd_shopping_list)

    p = sub.add_parser("prices", help="refresh catalogue prices from spesaonline")
    p.add_argument("--limit", type=int, default=None,
                   help="only the first N products (for a quick check)")
    p.set_defaults(func=cmd_prices)

    sub.add_parser("deals", help="price drops worth acting on").set_defaults(func=cmd_deals)

    p = sub.add_parser("reset", help="drop the database")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_reset)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
