# Ekko — Walking Skeleton (Fase 0)

Piattaforma di Reputation Intelligence. Questo repository è il *walking
skeleton* previsto dal piano di progetto (§5.2): la catena completa
**connettori → normalizzazione → arricchimento stadio 0 → storage → Ekko
Score → API → report HTML**, funzionante end-to-end, a costo zero
(nessun token AI, SQLite locale, dati demo inclusi).

## Avvio rapido (2 minuti, senza API key)

Dipendenze: `pydantic`, `flask`, `jinja2`, `httpx` (nessun'altra).

```bash
python scripts/gen_demo_data.py                  # genera i dataset demo
PYTHONPATH=. python -m ekko.cli demo             # ingestion + score + report_demo.html
PYTHONPATH=. python -m unittest discover tests   # 11 test
```

Poi apri `report_demo.html` nel browser.

## Modalità reale (motore di ricerca + fonti live + AI)

Modo più semplice — le chiavi le digiti in terminale, non finiscono in chat:

```bash
bash setup_keys.sh     # chiede le chiavi una a una, le salva in .env (chmod 600)
bash run.sh            # carica .env e avvia il server
# apri http://127.0.0.1:8000  → pagina "Cerca un'azienda"
```

In alternativa, a mano: `cp .env.example .env` e compila i valori.

Flusso: scrivi il nome dell'azienda (+ città per Google, + dominio per
Trustpilot) → Ekko raccoglie le recensioni dalle fonti attive → ti porta
sulla dashboard. Nella dashboard, "▶ Avvia analisi" chiama il modello AI se
una chiave è configurata, altrimenti usa il motore locale.

**Cosa serve perché sia "reale" e basta funzionare:**

| Vuoi… | Metti in `.env` | Note |
|---|---|---|
| Recensioni Google | `GOOGLE_MAPS_API_KEY` | Places API (New). ~5 recensioni/luogo dal profilo pubblico |
| Recensioni Trustpilot (API) | `TRUSTPILOT_API_KEY` + dominio azienda | API ufficiale gratuita |
| Trustpilot completo senza API | `EKKO_ENABLE_PUBLIC_TRUSTPILOT=1` | ⚠ scraping pagine pubbliche, viola i ToS, OFF di default |
| Analisi & suggerimenti con AI | una tra `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | auto-rilevata |

Senza nessuna chiave, tutto gira lo stesso sui **dati demo**.

### API server (endpoint)

```
GET  /                            pagina di ricerca
POST /search        (form)        nome → ingestion → redirect dashboard
POST /businesses/{id}/ingest      ingestion (JSON)
POST /businesses/{id}/analyze     analisi AI sull'insieme filtrato (PII-free)
GET  /businesses/{id}/score       Ekko Score
GET  /businesses/{id}/dashboard   dashboard interattiva
GET  /businesses/{id}/report      report statico
GET  /health                      stato connettori + AI
```

*Nota stack:* lo skeleton usa Flask + sqlite3 (stdlib) per girare ovunque a
zero setup; la migrazione a FastAPI + SQLAlchemy/Postgres è il primo item del
backlog di Fase 1 e i layer sono già separati per renderla indolore.

## Collegare le fonti reali

Copia `.env.example` in `.env` e imposta le chiavi:

| Variabile | Fonte | Note |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | Google Places API (New) | ⚠️ max ~5 recensioni/luogo: per la copertura completa serve OAuth Business Profile (profili propri) o provider corsia B |
| `TRUSTPILOT_API_KEY` | Trustpilot public API | recensioni pubbliche; risposte e profili rivendicati → API business in Fase 1 |
| `EKKO_DATABASE_URL` | Postgres | default: SQLite `ekko.db` |
| `EKKO_AUTHOR_HMAC_KEY` | — | chiave di pseudonimizzazione autori (obbligatoria in prod) |
| `EKKO_DISABLED_SOURCES` | — | kill-switch legale, es. `google,trustpilot` |

Senza chiavi, i connettori leggono i dataset demo: l'architettura è identica.

## Struttura

```
ekko/
  core/         FeedbackObject v1, tassonomia, Ekko Score v1, stadio 0
  connectors/   framework (base.py) + google.py + trustpilot.py
  ingestion/    pipeline: fetch → enrich → dedup → store
  storage/      SQLAlchemy (SQLite/Postgres)
  api/          FastAPI + report HTML
tests/          scoring, pipeline e2e, idempotenza, kill-switch
```

## Decisioni implementate (riferimenti al piano)

- **Feedback Object v1** con lineage e pseudonimizzazione HMAC alla frontiera (§2.3)
- **Connector framework** con corsie legali, cost meter e kill-switch per fonte (§2.5)
- **Ekko Score v1** deterministico: pesi fonte, recency half-life 6 mesi, correzione
  bayesiana, trend 90gg, tasso di risposta — con decomposizione spiegabile (§7.1)
- **Stadio 0** gratuito: sentiment da rating, topic da tassonomia, urgenza (§2.6)
- Dedup idempotente per re-ingestion sicura (§6.2)

## Prossimi passi (backlog Fase 0→1)

1. OAuth Google Business Profile (corsia A completa per i profili del cliente)
2. Integrazione provider corsia B (DataForSEO/Outscraper) dietro astrazione multi-provider
3. Business Graph v1: risoluzione P.IVA → profili (registro imprese)
4. Scheduler ingestion incrementale + Data Health dashboard
5. Migrazione storage a Postgres gestito quando si esce dal locale
