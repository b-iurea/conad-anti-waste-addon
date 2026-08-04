# Add-on Home Assistant per conad-anti-waste.
#
# Non parte da una base image HA: il login a my.conad.it richiede un Chrome
# reale su display virtuale (HTTP puro risponde 403 INVALID_PROTECTION_TOKEN e
# l'headless viene rifiutato dal captcha), quindi la base è la stessa immagine
# Playwright usata dal deploy Docker/Kubernetes. Il Supervisor non impone una
# base propria: bastano `init: false` e un entrypoint che legge
# /data/options.json.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    CONAD_COOKIES_PATH=/data/sessions/cookies.json \
    CHROME_PROFILE_DIR=/data/sessions/chrome-profile \
    ORDERS_CSV_PATH=/data/orders.csv \
    TZ=Europe/Rome

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb curl tzdata jq \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chrome vero: ottiene un punteggio migliore di Chromium su reCAPTCHA
# Enterprise, e il flusso di login lo preferisce quando presente.
RUN playwright install --with-deps chrome

COPY app/ ./app/
COPY cli.py .
COPY run.sh /run.sh
RUN chmod +x /run.sh

# /data è il volume persistente dell'add-on: sopravvive a riavvii e
# aggiornamenti. Ci vivono il database, i cookie e il profilo Chrome "caldo",
# che è l'asset che fa passare il captcha.
RUN mkdir -p /data/sessions

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["/run.sh"]
