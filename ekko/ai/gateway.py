"""AI Gateway — punto unico di accesso ai modelli (stadio 3 della cascata).

Provider supportati (basta UNA chiave in .env, in ordine di priorità):
  ANTHROPIC_API_KEY   → Claude
  OPENAI_API_KEY      → ChatGPT
  GEMINI_API_KEY      → Google Gemini
Override: EKKO_AI_PROVIDER=anthropic|openai|gemini, EKKO_AI_MODEL=<nome modello>.

Regole del gateway (piano §8.2):
- ai provider esterni non arriva MAI l'autore: solo testo, rating, data, fonte;
- una sola responsabilità: recensioni filtrate → JSON {findings, suggestions};
- ogni errore degrada con grazia: il chiamante fa fallback al motore locale.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.5-flash",
}

SYSTEM_PROMPT = """Sei il motore di analisi di Ekko, piattaforma di reputation intelligence.
Ricevi recensioni filtrate di un'azienda. Rispondi SOLO con JSON valido, schema:
{
 "summary": "2-3 frasi di sintesi esecutiva",
 "satisfactions": [
   {"title": "tema di soddisfazione", "pct": "43.8% of reviews",
    "points": ["evidenza 1", "evidenza 2"]}
 ],
 "dissatisfactions": [
   {"title": "tema di insoddisfazione", "pct": "12% of reviews",
    "points": ["evidenza 1", "evidenza 2"]}
 ],
 "voice": [
   {"title": "titoletto sezione", "text": "paragrafo discorsivo stile Voice of the Customer"}
 ],
 "recommendations": [
   {"priority": "HIGH|MEDIUM|LOW", "title": "azione", "detail": "perché e come",
    "owner": "reparto suggerito", "timeline": "es. pilota in 30 giorni",
    "target": "obiettivo misurabile"}
 ],
 "suggestions": [
   {"q": "suggerimento espresso dall'utente, citato fedelmente", "label": "fonte · data · stelle"}
 ]
}
Massimo 4 satisfactions, 4 dissatisfactions, 3 sezioni voice, 4 recommendations
(ordinate per priorità), 8 suggestions. Le percentuali stimale dai dati ricevuti.
I suggestions sono SOLO richieste/proposte espresse dagli utenti nelle recensioni.
Tono: diretto, operativo, italiano."""

REPLY_SYSTEM_PROMPT = """Sei l'assistente risposte-recensioni di Ekko.
Scrivi la risposta pubblica del titolare a UNA recensione Google.
Regole tassative:
- rispondi SOLO con il testo della risposta: niente virgolette, preamboli o note;
- NON inventare MAI fatti, nomi, promozioni o dettagli non presenti nella recensione;
- niente dati personali, niente promesse legali o rimborsi non richiesti;
- rating basso (1-3): scuse sincere, presa in carico, invito al contatto diretto;
- rating alto (4-5): ringraziamento caloroso e specifico;
- lunghezza: 2-4 frasi, pronte da pubblicare."""

# Fallback deterministico quando l'AI non è configurata: template con
# placeholder {nome} (azienda), {autore} (recensore), {firma}.
TEMPLATE_REPLY_HIGH = (
    "Gentile {autore}, grazie di cuore per la recensione lasciata a {nome}! "
    "Siamo felici che l'esperienza sia stata positiva e speriamo di "
    "rivederla presto. {firma}")
TEMPLATE_REPLY_LOW = (
    "Gentile {autore}, grazie per il riscontro su {nome}. Ci dispiace che "
    "l'esperienza non sia stata all'altezza delle aspettative: ci piacerebbe "
    "capire meglio cosa è successo, la invitiamo a contattarci direttamente "
    "per trovare una soluzione. {firma}")


def template_reply(business_name: str, review_text: str | None,
                   rating: float | None, settings: dict | None) -> str:
    """Bozza deterministica senza AI: template + placeholder.

    `rating` in scala 1..5 (None = non specificato -> variante positiva).
    Usa `settings['template']` se il cliente ne ha definito uno; altrimenti
    varianti per rating alto/basso. Mai fatti inventati: solo cortesia."""
    s = settings or {}
    template = (s.get("template") or "").strip()
    if not template:
        template = (TEMPLATE_REPLY_LOW if rating is not None and rating <= 3
                    else TEMPLATE_REPLY_HIGH)
    autore = (s.get("author") or "").strip() or "cliente"
    firma = (s.get("signature") or "").strip() or f"Lo staff di {business_name}"
    out = (template.replace("{nome}", business_name)
                   .replace("{autore}", autore)
                   .replace("{firma}", firma))
    return re.sub(r"[ \t]{2,}", " ", out).strip()


class AIGateway:
    def __init__(self) -> None:
        self.provider = os.environ.get("EKKO_AI_PROVIDER") or self._detect()
        self.model = os.environ.get("EKKO_AI_MODEL") or DEFAULT_MODELS.get(
            self.provider or "", "")

    @staticmethod
    def _detect() -> str | None:
        for provider, env in (("anthropic", "ANTHROPIC_API_KEY"),
                              ("openai", "OPENAI_API_KEY"),
                              ("gemini", "GEMINI_API_KEY")):
            if os.environ.get(env):
                return provider
        return None

    def available(self) -> bool:
        return self.provider is not None

    # ------------------------------------------------------------------ #
    def analyze(self, business_name: str, rows: list[dict]) -> dict[str, Any]:
        """rows: dict PII-free {d, s, st, txt, topics, rep}. Ritorna il JSON
        dello schema + campo "mode" = provider. Solleva su errore."""
        if not self.available():
            raise RuntimeError("Nessuna chiave AI configurata")
        user_prompt = self._build_prompt(business_name, rows)
        raw = self._call(user_prompt)
        data = self._parse_json(raw)
        data["mode"] = f"{self.provider}:{self.model}"
        return data

    @staticmethod
    def _build_prompt(business_name: str, rows: list[dict]) -> str:
        recent = sorted(rows, key=lambda r: r["d"], reverse=True)[:150]
        lines = []
        for r in recent:
            txt = (r.get("txt") or "").replace("\n", " ")[:300]
            lines.append(
                f'{r["d"][:10]} | {r["s"]} | {r["st"]}★ | '
                f'risposta:{"sì" if r.get("rep") else "no"} | '
                f'temi:{",".join(r.get("topics", [])) or "-"} | {txt}')
        return (f"Azienda: {business_name}\n"
                f"Recensioni nel filtro corrente ({len(rows)} totali, "
                f"le {len(recent)} più recenti qui sotto):\n" + "\n".join(lines))

    # ------------------------------------------------------------------ #
    def generate_review_reply(self, business_name: str,
                              review_text: str | None,
                              rating: float | None,
                              settings: dict | None) -> str:
        """Bozza di risposta a UNA recensione (rating in scala 1..5).

        Rispetta tone/language/signature/template dalle gbp_settings
        (+ chiave opzionale 'author' col nome pubblico del recensore).
        Senza chiave AI (o su errore) degrada al template deterministico.
        La bozza viene SEMPRE approvata/modificata dall'utente prima
        dell'invio: qui non si pubblica nulla."""
        if not self.available():
            return template_reply(business_name, review_text, rating, settings)
        s = settings or {}
        lines = [
            f"Azienda: {business_name}",
            f"Rating: {rating if rating is not None else 'non specificato'} su 5",
            f"Autore (nome pubblico): {s.get('author') or 'non indicato'}",
            f"Recensione: {(review_text or '(solo stelle, nessun testo)')[:1200]}",
            f"Tono richiesto: {s.get('tone') or 'professionale e cordiale'}",
            f"Lingua della risposta: {s.get('language') or 'it'}",
        ]
        if (s.get("signature") or "").strip():
            lines.append(f"Chiudi la risposta con questa firma: {s['signature']}")
        if (s.get("template") or "").strip():
            lines.append("Segui questa traccia/template del cliente "
                         "(placeholder {nome}={azienda}, {autore}, {firma}): "
                         + s["template"])
        lines.append("Scrivi ora la risposta pubblica del titolare.")
        try:
            raw = self._call("\n".join(lines), system=REPLY_SYSTEM_PROMPT,
                             max_tokens=500, json_mode=False)
            text = raw.strip().strip('"').strip()
            return text or template_reply(business_name, review_text,
                                          rating, settings)
        except Exception:  # degrada con grazia al template deterministico
            return template_reply(business_name, review_text, rating, settings)

    # ------------------------------------------------------------------ #
    def _call(self, user_prompt: str, system: str = SYSTEM_PROMPT,
              max_tokens: int = 3000, json_mode: bool = True) -> str:
        if self.provider == "anthropic":
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                         "anthropic-version": "2023-06-01"},
                json={"model": self.model, "max_tokens": max_tokens,
                      "system": system,
                      "messages": [{"role": "user", "content": user_prompt}]},
                timeout=60)
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        if self.provider == "openai":
            body = {"model": self.model, "max_tokens": max_tokens,
                    "temperature": 0.2,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user_prompt}]}
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            resp = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json=body,
                timeout=60)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        if self.provider == "gemini":
            gen_cfg = {"temperature": 0.2, "maxOutputTokens": max_tokens}
            if json_mode:
                gen_cfg["responseMimeType"] = "application/json"
            resp = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent",
                params={"key": os.environ["GEMINI_API_KEY"]},
                json={"systemInstruction": {"parts": [{"text": system}]},
                      "contents": [{"parts": [{"text": user_prompt}]}],
                      "generationConfig": gen_cfg},
                timeout=60)
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        raise RuntimeError(f"Provider sconosciuto: {self.provider}")

    @staticmethod
    def _parse_json(raw: str) -> dict:
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("Risposta AI senza JSON")
        data = json.loads(raw[start:end + 1])
        for k in ("findings", "suggestions", "satisfactions",
                  "dissatisfactions", "voice", "recommendations"):
            data.setdefault(k, [])
        allowed = {"critical", "serious", "warning", "good"}
        data["findings"] = [f for f in data["findings"]
                            if isinstance(f, dict) and f.get("sev") in allowed][:5]
        data["suggestions"] = [s for s in data["suggestions"]
                               if isinstance(s, dict) and s.get("q")][:8]
        PRIO_MAP = {"high": "HIGH", "alta": "HIGH", "alto": "HIGH",
                    "medium": "MEDIUM", "media": "MEDIUM", "medio": "MEDIUM",
                    "low": "LOW", "bassa": "LOW", "basso": "LOW"}
        data["recommendations"] = [r for r in data["recommendations"]
                                   if isinstance(r, dict) and r.get("title")][:4]
        for r in data["recommendations"]:
            r["priority"] = PRIO_MAP.get(str(r.get("priority", "")).strip().lower(), "MEDIUM")
        return data
