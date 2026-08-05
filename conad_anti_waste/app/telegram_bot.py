"""Telegram bot — the way this app is actually used.

Long-polling, so it needs no public URL, no ingress and no TLS certificate: it
behaves identically on a WSL laptop and inside the cluster.

Three logging mechanisms, layered from lowest to highest friction:

  1. log by meal        one tap deducts every ingredient
  2. daily shortlist    <=8 likely items, escalating nag if ignored
  3. weekly reckoning   settles whatever silence left behind, so the inventory
                        converges even for a week nobody touched the bot

All user-facing text is Italian; it is a family tool.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from app import auth_auto, db, importer, inventory, meals, stats, velocity
from app.config import get_settings
from app.scheduler import Scheduler

log = logging.getLogger(__name__)

MAX_SHORTLIST = 8
KIND_LABEL = {"consumed": "Mangiato", "wasted": "Buttato", "already_bad": "Era già andato"}


# --- helpers ---------------------------------------------------------------

def _authorised(update: Update) -> bool:
    allowed = get_settings().allowed_chat_ids
    chat = update.effective_chat
    return bool(chat and (not allowed or chat.id in allowed))


def _zone_icon(zone: str) -> str:
    return {"frigo": "🧊", "freezer": "❄️", "dispensa": "🥫", "non_food": "🧴"}.get(zone, "•")


def _expiry_badge(days_left: Optional[int]) -> str:
    if days_left is None:
        return ""
    if days_left < 0:
        return f"⛔ scaduto da {abs(days_left)}g"
    if days_left == 0:
        return "🔴 scade oggi"
    if days_left <= 2:
        return f"🔴 {days_left}g"
    if days_left <= 7:
        return f"🟡 {days_left}g"
    return f"🟢 {days_left}g"


def _short(name: str, n: int = 34) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def _lot_buttons(lot_id: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("✅ Mangiato", callback_data=f"ev:{lot_id}:consumed"),
        InlineKeyboardButton("🗑 Buttato", callback_data=f"ev:{lot_id}:wasted"),
    ]


def _mark_logged_today() -> None:
    """Streak bookkeeping: a reply today resets the nag."""
    with db.session() as conn:
        db.set_state(conn, "last_log_date", date.today().isoformat())
        db.set_state(conn, "nag_level", "0")


# --- commands --------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    chat_id = update.effective_chat.id
    with db.session() as conn:
        db.set_state(conn, "chat_id", str(chat_id))
    await update.message.reply_text(
        f"Ciao! Sono il tuo assistente anti-spreco.\n"
        f"Il tuo chat id è <code>{chat_id}</code> — mettilo in "
        f"<code>TELEGRAM_ALLOWED_CHAT_IDS</code>.\n\n"
        f"Scrivi /aiuto per la lista dei comandi.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    await update.message.reply_text(
        "<b>Cosa so fare</b>\n\n"
        "/frigo — cosa c'è, ordinato per scadenza\n"
        "/scadenze — cosa sta per scadere\n"
        "/ho &lt;prodotto&gt; — controlla se ce l'hai\n"
        "/serve — lista della spesa (e cosa NON ricomprare)\n"
        "/usato — segna cosa hai consumato oggi\n"
        "/piatti — i tuoi piatti ricorrenti\n"
        "/stats — sprechi e streak\n"
        "/scarica — importa l'ultimo ordine Conad",
        parse_mode=ParseMode.HTML,
    )


async def cmd_fridge(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    with db.session() as conn:
        items = inventory.current_stock(conn, include_non_food=False)
    if not items:
        await update.message.reply_text("Inventario vuoto. Usa /scarica per importare un ordine.")
        return

    lines, current_zone = [], None
    for it in items[:60]:
        if it["storage_zone"] != current_zone:
            current_zone = it["storage_zone"]
            lines.append(f"\n{_zone_icon(current_zone)} <b>{current_zone.upper()}</b>")
        qty = f" ×{it['qty_remaining']:g}" if it["qty_remaining"] != 1 else ""
        lines.append(f"  {_short(it['display_name'])}{qty}  {_expiry_badge(it['days_left'])}")

    extra = f"\n\n<i>… e altri {len(items) - 60}</i>" if len(items) > 60 else ""
    await update.message.reply_text("\n".join(lines) + extra, parse_mode=ParseMode.HTML)


async def cmd_expiring(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    with db.session() as conn:
        soon = inventory.expiring_soon(conn, within_days=7)
        late = inventory.overdue(conn)

    if not soon and not late:
        await update.message.reply_text("Niente in scadenza. 👌")
        return

    for it in (late + soon)[:MAX_SHORTLIST]:
        await update.message.reply_text(
            f"{_expiry_badge(it['days_left'])}  <b>{_short(it['display_name'], 44)}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([_lot_buttons(it["lot_id"])]),
        )


async def cmd_have(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    query = " ".join(ctx.args).strip()
    if not query:
        await update.message.reply_text("Uso: /ho tonno")
        return
    await _answer_have(update, query)


async def _answer_have(update: Update, query: str) -> None:
    with db.session() as conn:
        rows = conn.execute(
            """
            SELECT p.display_name, p.storage_zone,
                   COALESCE(SUM(CASE WHEN l.status='in_stock' THEN l.qty_remaining END),0) qty,
                   MIN(CASE WHEN l.status='in_stock' AND l.qty_remaining>0
                            THEN l.expiry_date END) next_expiry
            FROM products p LEFT JOIN lots l ON l.product_id = p.id
            WHERE p.norm_name LIKE ? OR p.category LIKE ?
            GROUP BY p.id HAVING qty > 0 ORDER BY qty DESC LIMIT 15
            """,
            (f"%{query.lower()}%", f"%{query.lower()}%"),
        ).fetchall()

    if not rows:
        await update.message.reply_text(f"Non hai <b>{query}</b>. 🛒", parse_mode=ParseMode.HTML)
        return

    lines = [f"Hai <b>{query}</b>:"]
    for r in rows:
        days = None
        if r["next_expiry"]:
            try:
                days = (date.fromisoformat(r["next_expiry"]) - date.today()).days
            except ValueError:
                pass
        lines.append(f"  {_zone_icon(r['storage_zone'])} {_short(r['display_name'])} "
                     f"×{r['qty']:g}  {_expiry_badge(days)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_shopping(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    with db.session() as conn:
        result = velocity.shopping_list(conn)

    lines = [f"🛒 <b>SERVE</b>  <i>(copertura {result['horizon_days']} giorni)</i>"]
    if result["serve"]:
        lines += [f"  • {_short(e['display_name'])} — <i>{e['reason']}</i>"
                  for e in result["serve"][:20]]
    else:
        lines.append("  <i>niente di urgente</i>")

    if result["hai_gia"]:
        lines.append("\n✋ <b>HAI GIÀ — non ricomprare</b>")
        lines += [f"  • {_short(e['display_name'])} — <i>{e['reason']}</i>"
                  for e in result["hai_gia"][:20]]

    if result["finiti_senza_storico"]:
        lines.append("\n<i>Finiti, ma senza storico per stimare:</i>")
        lines += [f"  • {_short(e['display_name'])}"
                  for e in result["finiti_senza_storico"][:10]]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    with db.session() as conn:
        s = stats.full(conn)

    rate = f"{s['waste_rate'] * 100:.0f}%" if s["waste_rate"] is not None else "—"
    c = s["compliance"]
    lines = [
        "📊 <b>Sprechi</b>",
        f"  Buttato: <b>{s['wasted_eur']:.2f} €</b> ({s['wasted_qty']:g} prodotti)",
        f"  Tasso di spreco: <b>{rate}</b>",
        f"  Valore in casa: {s['stock_value_eur']:.2f} €",
        f"  A rischio nei prossimi 3 giorni: <b>{s['at_risk_eur']:.2f} €</b>",
        "",
        f"🔥 Streak: <b>{c['streak']} giorni</b>",
        f"  Hai segnato {c['days_logged']}/{c['days_window']} giorni",
    ]
    if c["rate"] < 0.3:
        lines.append("\n<i>⚠️ Con pochi dati registrati questi numeri valgono poco.</i>")
    if s["by_category"]:
        lines.append("\n<b>Dove sprechi di più</b>")
        lines += [f"  • {b['category']}: {b['eur']:.2f} €" for b in s["by_category"][:5]]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_meals(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    with db.session() as conn:
        cookable = meals.cookable_now(conn, limit=10)
    if not cookable:
        await update.message.reply_text(
            "Non hai ancora piatti salvati.\n"
            "Creali dalla dashboard: un piatto = una lista di ingredienti, "
            "così segnarlo scala tutto in un tap."
        )
        return

    rows = [[InlineKeyboardButton(
        f"{m['name']}" + (" 🔥" if m["uses_expiring"] else ""),
        callback_data=f"meal:{m['meal_id']}")] for m in cookable]
    await update.message.reply_text(
        "🍽 <b>Cosa avete mangiato?</b>\n<i>🔥 = usa prodotti in scadenza</i>",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_used(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    await _send_shortlist(ctx.bot, update.effective_chat.id, nag_level=0)


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    await update.message.reply_text("Scarico l'ultimo ordine…")
    text = await asyncio.to_thread(_do_import)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Free text: treat a bare question as a stock lookup.

    Deliberately narrow while there is no LLM — it answers the one question that
    can be answered from data, and says so plainly otherwise.
    """
    if not _authorised(update) or not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    lowered = text.lower()

    if any(w in lowered for w in ("cosa compro", "cosa serve", "spesa", "lista")):
        await cmd_shopping(update, ctx)
        return
    if lowered.startswith(("ho ", "abbiamo ", "c'è ", "ce ")) or lowered.endswith("?"):
        cleaned = lowered.rstrip("?").removeprefix("ho ").removeprefix("abbiamo ")
        cleaned = cleaned.removeprefix("c'è ").removeprefix("ce ").strip()
        if cleaned:
            await _answer_have(update, cleaned)
            return
    await update.message.reply_text(
        "Per ora capisco solo i comandi — prova /aiuto.\n"
        "<i>La chat intelligente arriva con l'integrazione AI.</i>",
        parse_mode=ParseMode.HTML,
    )


# --- inline button callbacks -----------------------------------------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        return
    q = update.callback_query
    await q.answer()
    parts = (q.data or "").split(":")
    action = parts[0]

    if action == "ev":
        lot_id, kind = int(parts[1]), parts[2]
        with db.session() as conn:
            lot = inventory.get_lot(conn, lot_id)
            if lot is None:
                await q.edit_message_text("Prodotto non trovato.")
                return
            inventory.log_event(conn, lot_id, kind, qty=1.0, source="telegram")
        _mark_logged_today()

        icon = "✅" if kind == "consumed" else "🗑"
        suffix = ""
        if kind in ("wasted", "already_bad"):
            cost = (lot.get("unit_price_eur") or 0)
            if cost:
                suffix = f" · {cost:.2f} € persi"
        await q.edit_message_text(
            f"{icon} <b>{_short(lot['display_name'], 44)}</b> — {KIND_LABEL[kind]}{suffix}",
            parse_mode=ParseMode.HTML,
        )

    elif action == "bad":
        lot_id = int(parts[1])
        with db.session() as conn:
            inventory.log_event(conn, lot_id, "already_bad", qty=1.0, source="telegram")
        _mark_logged_today()
        await q.edit_message_text("🗑 Segnato come già andato a male. Aggiusto le stime.")

    elif action == "keep":
        lot_id, days = int(parts[1]), int(parts[2])
        with db.session() as conn:
            lot = inventory.extend_expiry(conn, lot_id, days)
        _mark_logged_today()
        await q.edit_message_text(
            f"👍 <b>{_short(lot['display_name'], 40)}</b> ancora buono — "
            f"scadenza spostata al {lot['expiry_date']}.",
            parse_mode=ParseMode.HTML,
        )

    elif action == "meal":
        meal_id = int(parts[1])
        with db.session() as conn:
            result = meals.log_meal(conn, meal_id)
        _mark_logged_today()
        lines = [f"🍽 <b>{result['meal']}</b> registrato."]
        if result["deducted"]:
            lines.append("Scalati: " + ", ".join(_short(d["display_name"], 22)
                                                 for d in result["deducted"]))
        if result["missing"]:
            lines.append("⚠️ Non in inventario: " + ", ".join(
                _short(m["display_name"], 22) for m in result["missing"]))
        await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML)

    elif action == "none":
        _mark_logged_today()
        await q.edit_message_text("👍 Segnato: oggi niente da scalare.")

    elif action == "later":
        await q.edit_message_text("Ok, te lo richiedo domani.")


# --- scheduled jobs --------------------------------------------------------

def _target_chats() -> list[int]:
    s = get_settings()
    if s.allowed_chat_ids:
        return sorted(s.allowed_chat_ids)
    with db.session() as conn:
        saved = db.get_state(conn, "chat_id")
    return [int(saved)] if saved else []


def _shortlist_items() -> list[dict]:
    """The <=8 items you most plausibly used today.

    Soonest expiry first, then the products you get through fastest. Long lists
    get ignored; short ones get answered.
    """
    with db.session() as conn:
        stock = inventory.current_stock(conn, include_non_food=False)
        rates = velocity.consumption_rates(conn)

    def sort_key(item):
        days = item["days_left"]
        urgency = days if days is not None else 999
        rate = rates.get(item["product_id"], {}).get("per_week", 0)
        return (urgency, -rate)

    return sorted(stock, key=sort_key)[:MAX_SHORTLIST]


async def _send_shortlist(bot, chat_id: int, nag_level: int = 0) -> None:
    items = _shortlist_items()
    if not items:
        return

    headers = {
        0: "🍽 <b>Cosa avete usato oggi?</b>",
        1: "🍽 <b>Ieri non hai segnato niente.</b> Cosa avete usato?",
        2: "⚠️ <b>Due giorni senza segnare.</b> L'inventario inizia a sbagliare.",
        3: "⚠️ <b>L'inventario non è più affidabile.</b> Bastano 30 secondi.",
    }
    await bot.send_message(chat_id, headers.get(min(nag_level, 3), headers[3]),
                           parse_mode=ParseMode.HTML)

    with db.session() as conn:
        cookable = meals.cookable_now(conn, limit=4)
    if cookable:
        rows = [[InlineKeyboardButton(m["name"] + (" 🔥" if m["uses_expiring"] else ""),
                                      callback_data=f"meal:{m['meal_id']}")]
                for m in cookable]
        await bot.send_message(chat_id, "Un piatto scala tutti i suoi ingredienti:",
                               reply_markup=InlineKeyboardMarkup(rows))

    for it in items:
        await bot.send_message(
            chat_id,
            f"{_expiry_badge(it['days_left'])}  {_short(it['display_name'], 44)}",
            reply_markup=InlineKeyboardMarkup([_lot_buttons(it["lot_id"])]),
        )

    await bot.send_message(
        chat_id, "—",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Niente oggi", callback_data="none"),
            InlineKeyboardButton("Non ora", callback_data="later"),
        ]]),
    )


async def job_daily_prompt(bot) -> None:
    """The daily shortlist, with an escalating nag that caps out.

    Past `max_nag_level` it stops asking daily and leans on the weekly
    reckoning instead: a bot that shouts every day gets muted, and a muted bot
    is a dead app.
    """
    with db.session() as conn:
        last = db.get_state(conn, "last_log_date")
        nag = int(db.get_state(conn, "nag_level", "0") or 0)

    today = date.today()
    if last == today.isoformat():
        return  # already logged today, nothing to ask

    days_silent = 0
    if last:
        try:
            days_silent = (today - date.fromisoformat(last)).days
        except ValueError:
            days_silent = 1

    nag = min(days_silent, get_settings().max_nag_level)
    with db.session() as conn:
        db.set_state(conn, "nag_level", str(nag))

    if days_silent > get_settings().max_nag_level:
        return  # back off; the Sunday reckoning will collect the backlog

    for chat_id in _target_chats():
        await _send_shortlist(bot, chat_id, nag_level=nag)


async def job_weekly_reckoning(bot) -> None:
    """Settle everything expired that nobody logged — the safety net.

    This is what makes silence survivable: even an ignored week reconciles in
    one pass, and every answer is high-quality learning evidence.
    """
    with db.session() as conn:
        items = inventory.overdue(conn)

    for chat_id in _target_chats():
        if not items:
            await bot.send_message(
                chat_id, "🎉 <b>Nessun prodotto scaduto in sospeso.</b> Settimana pulita!",
                parse_mode=ParseMode.HTML)
            continue

        lost = sum((i.get("unit_price_eur") or 0) * (i.get("qty_remaining") or 0) for i in items)
        await bot.send_message(
            chat_id,
            f"🧾 <b>Resoconto settimanale</b>\n"
            f"{len(items)} prodotti scaduti da sistemare — {lost:.2f} € in ballo.\n"
            f"<i>Rispondi e le stime di scadenza migliorano.</i>",
            parse_mode=ParseMode.HTML,
        )
        for it in items[:15]:
            await bot.send_message(
                chat_id,
                f"{_expiry_badge(it['days_left'])}  {_short(it['display_name'], 42)}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Mangiato", callback_data=f"ev:{it['lot_id']}:consumed"),
                     InlineKeyboardButton("🗑 Buttato", callback_data=f"ev:{it['lot_id']}:wasted")],
                    [InlineKeyboardButton("👍 Ancora buono (+5g)",
                                          callback_data=f"keep:{it['lot_id']}:5")],
                ]),
            )


def _do_import() -> str:
    """Blocking import. Returns the message to show. Errors are made loud."""
    try:
        # Logs in by itself if the cookies died — no human, no browser needed.
        session = auth_auto.ensure_session()
        with db.session() as conn:
            result = importer.import_live(conn, session, only_last=True)
            flagged = velocity.overbought(conn, result.codes[0]) if result.codes else []
    except auth_auto.SessionUnavailable as e:
        log.error("session unavailable: %s", e)
        return ("🔑 <b>Non riesco ad accedere a Conad.</b>\n"
                f"<code>{e}</code>\n\n"
                "Controlla CONAD_EMAIL / CONAD_PASSWORD, "
                "oppure la password del sito è cambiata.")
    except Exception as e:  # noqa: BLE001
        log.exception("import failed")
        return f"❌ <b>Import fallito.</b>\n<code>{e}</code>"

    if not result.orders_added:
        return "Nessun ordine nuovo."

    msg = [f"📦 <b>Nuovo ordine importato</b> ({result.lots_added} prodotti)."]
    if flagged:
        msg.append("\n✋ <b>Avevi già in casa:</b>")
        msg += [f"  • {_short(f['display_name'])} — <i>{f['reason']}</i>" for f in flagged]
    return "\n".join(msg)


def _check_session() -> Optional[str]:
    """Refresh the session proactively. Returns a message only on failure."""
    try:
        auth_auto.ensure_session()
        return None
    except auth_auto.SessionUnavailable as e:
        return ("🔑 <b>Sessione Conad non disponibile.</b>\n"
                f"<code>{e}</code>\n\n"
                "<i>Finché non si risolve, gli ordini nuovi non entrano "
                "nell'inventario.</i>")


async def job_session_keepalive(bot) -> None:
    """Renew the Conad session before an import actually needs it.

    A session that dies silently is the top operational risk of this app: the
    inventory just stops updating while still looking authoritative. Checking
    on a schedule turns that into a message you receive.
    """
    problem = await asyncio.to_thread(_check_session)
    if not problem:
        return
    for chat_id in _target_chats():
        await bot.send_message(chat_id, problem, parse_mode=ParseMode.HTML)


async def job_import(bot) -> None:
    text = await asyncio.to_thread(_do_import)
    if text == "Nessun ordine nuovo.":
        return  # routine no-op, not worth a notification
    for chat_id in _target_chats():
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


# --- wiring ----------------------------------------------------------------

def build_application() -> Application:
    s = get_settings()
    if not s.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set — the bot cannot start")

    app = Application.builder().token(s.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["aiuto", "help"], cmd_help))
    app.add_handler(CommandHandler(["frigo", "inventario"], cmd_fridge))
    app.add_handler(CommandHandler(["scadenze", "scadenza"], cmd_expiring))
    app.add_handler(CommandHandler("ho", cmd_have))
    app.add_handler(CommandHandler(["serve", "spesa"], cmd_shopping))
    app.add_handler(CommandHandler(["stats", "statistiche"], cmd_stats))
    app.add_handler(CommandHandler(["piatti", "piatto"], cmd_meals))
    app.add_handler(CommandHandler(["usato", "segna"], cmd_used))
    app.add_handler(CommandHandler(["scarica", "importa"], cmd_scan))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


async def _post_init(app: Application) -> None:
    s = get_settings()
    sched = Scheduler()
    sched.add_daily("daily_prompt", s.daily_prompt_hour,
                    lambda: job_daily_prompt(app.bot))
    sched.add_weekly("weekly_reckoning", s.weekly_reckoning_day, s.weekly_reckoning_hour,
                     lambda: job_weekly_reckoning(app.bot))
    sched.add_interval("import_fallback", s.refresh_interval_hours,
                       lambda: job_import(app.bot))
    # Renewed well ahead of the daily import, so a dead session is discovered
    # by a background job rather than by a failing order import.
    sched.add_daily("session_keepalive", "06:00", lambda: job_session_keepalive(app.bot))

    ran = await sched.run_catchup()
    if ran:
        log.info("catch-up ran: %s", ", ".join(ran))
    sched.start()
    app.bot_data["scheduler"] = sched


async def _post_shutdown(app: Application) -> None:
    sched = app.bot_data.get("scheduler")
    if sched:
        sched.shutdown()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init()
    app = build_application()
    app.post_init = _post_init
    app.post_shutdown = _post_shutdown
    log.info("bot starting (long polling)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
