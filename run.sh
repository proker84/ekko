#!/usr/bin/env bash
# Avvio Ekko: installa le dipendenze se mancano, carica .env, lancia il server.
set -e
cd "$(dirname "$0")"

# 1) dipendenze Python (solo la prima volta)
if ! python3 -c "import flask, pydantic, jinja2, httpx" 2>/dev/null; then
  echo "→ Installo le librerie necessarie (solo la prima volta)…"
  python3 -m pip install --user --quiet --break-system-packages \
      pydantic flask jinja2 httpx 2>/dev/null \
    || python3 -m pip install --user --quiet pydantic flask jinja2 httpx
fi

# 2) carica le chiavi dal .env
if [ -f .env ]; then set -a; . ./.env; set +a; fi

# 3) dati demo di fallback (se una fonte non risponde)
if [ ! -f data/demo_reviews_google.json ]; then python3 scripts/gen_demo_data.py; fi

echo
echo "  Ekko avviato → apri nel browser:  http://127.0.0.1:8000"
echo "  (Ctrl+C per fermare)"
echo
PYTHONPATH=. python3 -m ekko.api.main
