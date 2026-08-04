# Conad Anti Waste — repository add-on per Home Assistant

Questo repository contiene l'add-on che fa girare il servizio
[conad-anti-waste](https://github.com/USERNAME/conad-anti-waste) dentro Home
Assistant.

## Installazione

1. **Impostazioni → Add-on → Store**
2. Menu **⋮** in alto a destra → **Repositori**
3. Incolla l'URL di questo repository → **Aggiungi**
4. Chiudi, aggiorna la pagina: compare **Conad Anti Waste**
5. **Installa**, poi apri **Configurazione** e inserisci email e password Conad
6. **Avvia**

La dashboard compare nella sidebar come *Anti Waste*.

## E l'integrazione?

L'add-on è il servizio. Per avere **entità** in Home Assistant — sensori delle
scadenze, lista della spesa come `todo`, calendario delle scadenze, pulsanti di
import — installa anche l'integrazione, separata perché HACS e lo store degli
add-on sono due canali distinti:

- Add-on → *Impostazioni → Add-on → Store → Repositori* (questo repository)
- Integrazione → HACS → *Integrazioni → Repositori personalizzati*

L'integrazione si collega da sola all'add-on: nella configurazione trova già
compilato l'host `local-conad-anti-waste`.

## Struttura

```
repository.yaml              metadati del repository
conad_anti_waste/
  config.yaml                opzioni, schema, ingress
  build.yaml                 base image per architettura
  Dockerfile                 immagine (Playwright + Chrome + Xvfb)
  run.sh                     entrypoint: options.json → variabili d'ambiente
  DOCS.md                    documentazione mostrata nella scheda dell'add-on
```

## Note per chi pubblica

Prima di pubblicare, sostituisci `USERNAME` in `repository.yaml`,
`config.yaml` e `build.yaml`.

Il campo `image:` in `config.yaml` fa sì che il Supervisor scarichi
un'immagine già compilata da GHCR invece di ricompilarla sull'hardware
dell'utente — consigliato, perché `playwright install chrome` su un Raspberry
richiede parecchio tempo. Se preferisci la build locale, rimuovi quella riga.
