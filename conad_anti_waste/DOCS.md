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
funziona, portane sessione e profilo qui dentro. È la strada che funziona
quando A continua a fallire.

Sul PC, dopo un login riuscito:

```bash
python cli.py login --force      # usa il display vero, non Xvfb: punteggio migliore
python cli.py pack-session       # -> conad-session.tar.gz
```

`pack-session` si rifiuta di impacchettare cookie scaduti (`--no-check` per
forzare) e produce un archivio con una cartella `sessions/` al primo livello:

```
sessions/
  cookies.json
  chrome-profile/
```

Poi apri **Anti Waste** nella sidebar e usa il pulsante 🔑 in alto a destra:
scegli il file e basta. L'add-on scompatta sul volume, butta via i lock
`Singleton*` (puntano all'hostname della macchina di origine e confondono il
Chrome di qui) e azzera il cooldown, così il primo import riparte subito senza
aspettare `login_cooldown_hours`.

Le cache rigenerabili (`*Cache*`, `optimization_guide_model_store`, `Crashpad`)
le esclude già `pack-session`: sono la gran parte degli ~80 MB di un profilo e
non contano nulla per il punteggio. Quello che conta è `Default/History`, cioè
la cronologia. Un profilo così sta sotto il mezzo megabyte.

Se preferisci la riga di comando, l'equivalente dal terminale di Home
Assistant è:

```bash
# Il nome del container NON è sempre lo stesso: da repository git il prefisso
# è l'hash del repository (es. addon_cde624e8_conad_anti_waste), non "local".
ADDON=$(docker ps --format '{{.Names}}' | grep conad_anti_waste)
docker cp sessions "$ADDON:/data/sessions"
```

In entrambi i casi **riavvia l'add-on**: il profilo si legge all'avvio del
browser, non viene ricontrollato a caldo.

Da lì in poi l'add-on rinnova i cookie da solo — e spesso senza rifare il
login vero: con un profilo caldo il redirect a `refreshToken.json` si completa
dentro il browser, che è molto più economico di un login con captcha.

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
