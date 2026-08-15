#!/usr/bin/env bash
# Ekko — configurazione chiavi API (compatibile con il bash 3.2 di macOS).
# Le chiavi le digiti TU qui in terminale: non passano dalla chat, non finiscono
# nella cronologia della shell, si salvano solo nel file .env locale (chmod 600).
#
# Uso:   bash setup_keys.sh
set -eu

ENV_FILE=".env"
cd "$(dirname "$0")"

echo "──────────────────────────────────────────────"
echo "  Ekko · configurazione chiavi API"
echo "  Invio a vuoto = lascia il valore attuale / salta"
echo "──────────────────────────────────────────────"

# legge il valore attuale di una variabile dal .env esistente (se c'è)
current() {
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true
}

# ask VAR "Descrizione"  -> stampa "VAR=valore" (valore nuovo o quello attuale)
ask() {
  var="$1"; desc="$2"
  have="$(current "$var")"
  shown=""
  if [ -n "$have" ]; then
    tail4=$(printf '%s' "$have" | tail -c 4)
    shown=" [attuale: ****$tail4]"
  fi
  # i prompt vanno su stderr: su stdout esce SOLO la riga VAR=valore per il .env
  printf "  %s%s: " "$desc" "$shown" >&2
  stty -echo 2>/dev/null || true      # nasconde l'input (chiavi segrete)
  read val
  stty echo 2>/dev/null || true
  echo >&2
  [ -n "$val" ] && have="$val"
  printf '%s=%s\n' "$var" "$have"
}

TMP="$(mktemp)"
echo "# Ekko .env — generato da setup_keys.sh ($(date))" > "$TMP"

echo
echo "▶ Google (recensioni Google Maps)"
ask GOOGLE_MAPS_API_KEY "  Google Maps/Places API key" >> "$TMP"

echo
echo "▶ Trustpilot"
ask TRUSTPILOT_API_KEY    "  Trustpilot API key (Client ID)" >> "$TMP"
ask TRUSTPILOT_API_SECRET "  Trustpilot API secret (Client Secret, opz.)" >> "$TMP"

echo
echo "▶ Analisi AI (basta UNA)"
ask ANTHROPIC_API_KEY "  Anthropic / Claude API key" >> "$TMP"
ask OPENAI_API_KEY    "  OpenAI / ChatGPT API key" >> "$TMP"
ask GEMINI_API_KEY    "  Google Gemini API key" >> "$TMP"

echo
printf "  Attivare Trustpilot PUBBLICO (scraping, viola i ToS)? [y/N]: "
read tp_pub
case "$tp_pub" in
  y|Y) echo "EKKO_ENABLE_PUBLIC_TRUSTPILOT=1" >> "$TMP" ;;
  *)   echo "EKKO_ENABLE_PUBLIC_TRUSTPILOT=" >> "$TMP" ;;
esac

# chiave di pseudonimizzazione: mantieni quella esistente o generane una nuova
hmac="$(current EKKO_AUTHOR_HMAC_KEY)"
if [ -z "$hmac" ]; then
  hmac="$(head -c 32 /dev/urandom | base64)"
  echo "  ✓ Generata EKKO_AUTHOR_HMAC_KEY casuale"
fi
{
  echo "EKKO_TP_PUBLIC_MAX_PAGES=10"
  echo "EKKO_AI_PROVIDER="
  echo "EKKO_AI_MODEL="
  echo "EKKO_DATABASE_URL="
  echo "EKKO_AUTHOR_HMAC_KEY=$hmac"
  echo "EKKO_DISABLED_SOURCES="
} >> "$TMP"

mv "$TMP" "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo
echo "✓ Salvato in $ENV_FILE (permessi 600)."
echo "  Fonti attive:"
val_of() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }
non_empty() { [ -n "$(val_of "$1")" ]; }
if non_empty GOOGLE_MAPS_API_KEY; then echo "   Google      : ON"; else echo "   Google      : off (demo)"; fi
if non_empty TRUSTPILOT_API_KEY;  then echo "   Trustpilot  : ON"; else echo "   Trustpilot  : off (demo)"; fi
if non_empty TRUSTPILOT_API_SECRET; then echo "   TP privato  : ON (OAuth)"; else echo "   TP privato  : off"; fi
if non_empty ANTHROPIC_API_KEY || non_empty OPENAI_API_KEY || non_empty GEMINI_API_KEY; then
  echo "   Analisi AI  : ON"; else echo "   Analisi AI  : off (motore locale)"; fi

echo
echo "  Ora avvia Ekko con:   bash run.sh"
echo "  poi apri:             http://127.0.0.1:8000"
