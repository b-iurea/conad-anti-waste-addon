# Conad Anti Waste — Add-on

Inventario domestico della spesa Conad dentro Home Assistant: frigo, freezer e
dispensa, con due obiettivi di pari peso — non sprecare e non ricomprare.

L'add-on contiene il servizio completo: import degli ordini da my.conad.it,
database SQLite, dashboard web (visibile nella sidebar), API, e — se lo vuoi —
il bot Telegram.

## Configurazione

| Opzione | Cosa fa |
|---|---|
| `conad_email` | Account my.conad.it. Serve per importare gli ordini. |
| `conad_password` | Password dello stesso account. |
| `enable_bot` | Avvia anche il bot Telegram. Spento di default: con l'integrazione Home Assistant spesso non serve più. |
| `telegram_bot_token` | Token di BotFather. Solo se `enable_bot` è attivo. |
| `telegram_allowed_chat_ids` | Whitelist di chat, separate da virgola. Il bot ignora tutte le altre. |
| `daily_prompt_hour` | Ora del promemoria serale (`HH:MM`). |
| `weekly_reckoning_day` / `_hour` | Quando fare il resoconto settimanale. |
| `expiry_warn_days` | Con quanti giorni di anticipo segnalare una scadenza. |
| `refresh_interval_hours` | Ogni quanto reimportare gli ordini. |
| `login_cooldown_hours` | Attesa dopo un login fallito. Non abbassarlo: vedi sotto. |

Le credenziali finiscono in `/data/options.json`, gestito dal Supervisor, e non
vengono mai scritte nei log.

## Il primo login è la parte delicata

my.conad.it non si lascia autenticare via HTTP: risponde `403
WEB:INVALID_PROTECTION_TOKEN`, perché il token lo genera solo il JavaScript del
sito. E rifiuta i browser headless. L'unica cosa che funziona è un Chrome vero
su display virtuale — che è il motivo per cui questo add-on pesa quanto pesa.

Il captcha è inoltre **reputazionale**: valuta il profilo del browser. Un
profilo appena creato viene rifiutato con buona probabilità, uno con cronologia
reale passa. Misurato sulla stessa macchina: profilo caldo → login riuscito,
profilo vergine → fallito ripetutamente.

Conseguenza pratica: **al primo avvio il login può fallire**, e non è un bug.
Hai due strade.

**A — Lascia che ci provi.** Dopo un fallimento l'add-on aspetta
`login_cooldown_hours` (6 di default) invece di insistere, perché tentativi
ravvicinati abbassano ulteriormente il punteggio. Nel frattempo tutto il resto
dell'inventario funziona: puoi registrare consumi e sprechi, mancano solo gli
ordini nuovi.

**B — Semina un profilo già caldo.** Se hai una macchina dove il login
funziona, copia sessione e profilo nel volume dell'add-on:

```bash
# dal terminale di Home Assistant (add-on SSH o Terminal)
docker cp cookies.json     addon_local_conad_anti_waste:/data/sessions/cookies.json
docker cp chrome-profile/  addon_local_conad_anti_waste:/data/sessions/chrome-profile
```

Da lì in poi l'add-on rinnova i cookie da solo.

`/data` è persistente: sopravvive a riavvii, aggiornamenti dell'add-on e
aggiornamenti di Home Assistant. Il profilo caldo si semina una volta sola.

## Rete e sicurezza

L'add-on **non pubblica porte sull'host**. La dashboard passa dall'ingress di
Home Assistant, quindi eredita la sua autenticazione, e l'API è raggiungibile
solo dalla rete interna degli add-on — dove vive anche Home Assistant Core, che
è ciò che serve all'integrazione.

Questo è deliberato: gli endpoint `POST` dell'API (registrazione eventi,
import, login) non hanno autenticazione propria. Se decommenti la sezione
`ports` in `config.yaml` li esponi in chiaro sulla LAN.

## Dati

Tutto vive in `/data`:

```
/data/inventory.db              database SQLite
/data/sessions/cookies.json     sessione Conad
/data/sessions/chrome-profile/  profilo Chrome (l'asset che passa il captcha)
/data/orders.csv                export storico opzionale, per il primo seed
```

I backup di Home Assistant includono `/data`, quindi l'inventario finisce negli
snapshot insieme al resto.
