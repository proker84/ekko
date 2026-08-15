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
 "findings": [
   {"sev": "critical|serious|warning|good",
    "title": "titolo breve e concreto",
    "detail": "evidenza con numeri presi dai dati",
    "action": "azione operativa consigliata"}
 ],
 "suggestions": [
   {"q": "suggerimento espresso dall'utente, citato o parafrasato fedelmente",
    "label": "fonte · data · stelle"}
 ],
 "summary": "2-3 frasi di sintesi esecutiva in italiano"
}
Massimo 5 findings ordinati per severità, massimo 8 suggestions.
I suggestions sono SOLO richieste/proposte espresse dagli utenti nelle recensioni,
non tue idee. Se non ce ne sono, lista vuota. Tono: diretto, operativo, italiano."""


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
    def _call(self, user_prompt: str) -> str:
        if self.provider == "anthropic":
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                         "anthropic-version": "2023-06-01"},
                json={"model": self.model, "max_tokens": 1800,
                      "system": SYSTEM_PROMPT,
                      "messages": [{"role": "user", "content": user_prompt}]},
                timeout=60)
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        if self.provider == "openai":
            resp = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json={"model": self.model, "max_tokens": 1800, "temperature": 0.2,
                      "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": user_prompt}]},
                timeout=60)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        if self.provider == "gemini":
            resp = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{self.model}:generateContent",
                params={"key": os.environ["GEMINI_API_KEY"]},
                json={"systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                      "contents": [{"parts": [{"text": user_prompt}]}],
                      "generationConfig": {"temperature": 0.2,
                                           "maxOutputTokens": 1800,
                                           "responseMimeType": "application/json"}},
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
        data.setdefault("findings", [])
        data.setdefault("suggestions", [])
        allowed = {"critical", "serious", "warning", "good"}
        data["findings"] = [f for f in data["findings"]
                            if isinstance(f, dict) and f.get("sev") in allowed][:5]
        data["suggestions"] = [s for s in data["suggestions"]
                               if isinstance(s, dict) and s.get("q")][:8]
        return data
