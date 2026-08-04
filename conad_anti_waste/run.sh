#!/usr/bin/env bash
# Entrypoint dell'add-on.
#
# Il Supervisor scrive le opzioni dell'utente in /data/options.json. Le
# traduciamo in variabili d'ambiente, che è l'unico canale di configurazione
# che l'app conosce (app/config.py, 12-factor).
#
# Niente bashio: la base image è quella di Playwright, non quella HA.
set -euo pipefail

OPTIONS=/data/options.json

opt() {
    # opt <chiave> <default>
    jq -r --arg k "$1" --arg d "${2-}" '.[$k] // $d' "$OPTIONS" 2>/dev/null || echo "${2-}"
}

if [[ ! -f "$OPTIONS" ]]; then
    echo "FATAL: /data/options.json assente — l'add-on non è stato avviato dal Supervisor." >&2
    exit 1
fi

export CONAD_EMAIL="$(opt conad_email)"
export CONAD_PASSWORD="$(opt conad_password)"
export TELEGRAM_BOT_TOKEN="$(opt telegram_bot_token)"
export TELEGRAM_ALLOWED_CHAT_IDS="$(opt telegram_allowed_chat_ids)"
export DAILY_PROMPT_HOUR="$(opt daily_prompt_hour 20:30)"
export WEEKLY_RECKONING_DAY="$(opt weekly_reckoning_day sun)"
export WEEKLY_RECKONING_HOUR="$(opt weekly_reckoning_hour 18:00)"
export EXPIRY_WARN_DAYS="$(opt expiry_warn_days 2)"
export REFRESH_INTERVAL_HOURS="$(opt refresh_interval_hours 24)"
export LOGIN_COOLDOWN_HOURS="$(opt login_cooldown_hours 6)"

LOG_LEVEL="$(opt log_level info)"
ENABLE_BOT="$(opt enable_bot false)"

# Il fuso lo detta Home Assistant, non l'immagine.
if [[ -n "${TZ:-}" ]]; then
    ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime 2>/dev/null || true
fi

mkdir -p /data/sessions

if [[ -z "$CONAD_EMAIL" || -z "$CONAD_PASSWORD" ]]; then
    echo "ATTENZIONE: email o password Conad non configurate."
    echo "            L'inventario funziona lo stesso, ma l'import degli ordini"
    echo "            resterà fermo finché non le inserisci nelle opzioni."
fi

# Il bot Telegram è il secondo processo dello stesso codice: opzionale, perché
# con l'integrazione Home Assistant molti non ne hanno più bisogno.
if [[ "$ENABLE_BOT" == "true" ]]; then
    if [[ -z "$TELEGRAM_BOT_TOKEN" ]]; then
        echo "ATTENZIONE: bot abilitato ma token assente — non lo avvio."
    else
        echo "Avvio del bot Telegram..."
        python -m app.telegram_bot &
        BOT_PID=$!
        trap 'kill "$BOT_PID" 2>/dev/null || true' EXIT
    fi
fi

echo "Avvio del server su :8000 (log level: ${LOG_LEVEL})..."
exec uvicorn app.server:app \
    --host 0.0.0.0 \
    --port 8000 \
    --log-level "$LOG_LEVEL" \
    --proxy-headers \
    --forwarded-allow-ips '*'
