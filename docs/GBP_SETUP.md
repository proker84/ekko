# Ekko · Collegamento Google Business Profile (Fase 2)

Il cliente che **possiede** profili Google Business collega il proprio account
Google a Ekko (OAuth). Da lì Ekko:

1. scarica **gratuitamente tutte** le recensioni delle sue sedi tramite la
   Google Business Profile API (corsia A, `official_api`, costo 0 €);
2. propone **bozze di risposta generate dall'AI** che l'utente
   **approva o modifica prima dell'invio** — l'invio automatico non esiste,
   per scelta di prodotto.

---

## ⚠️ AVVERTENZA IMPORTANTE: serve l'approvazione di Google

L'accesso alla **Google Business Profile API NON è immediato**:

- le API GBP (`My Business Account Management`, `My Business Business
  Information`, `Google My Business API v4` per recensioni/risposte) partono
  con **quota iniziale 0**: anche abilitandole in Cloud Console, ogni
  chiamata risponde `429 RESOURCE_EXHAUSTED` finché Google non approva la
  richiesta di quota;
- la richiesta si fa tramite il **modulo dedicato di accesso alle GBP API**
  ("Request access to the Business Profile APIs"), reperibile nella
  documentazione ufficiale: https://developers.google.com/my-business/content/prereqs
- l'app OAuth deve superare la **verifica di Google** per lo scope sensibile
  `https://www.googleapis.com/auth/business.manage` (finché l'app è in
  modalità "Testing" funziona solo per gli utenti di test, max 100).

### Passi operativi per richiedere l'accesso

1. In **Google Cloud Console** crea (o riusa) il progetto dell'app Ekko.
2. Abilita le API: *My Business Account Management API*, *My Business
   Business Information API* e *Google My Business API* (v4, recensioni).
3. Compila il **modulo di richiesta accesso GBP** con: email del progetto,
   project id/number, caso d'uso ("gestione recensioni per conto dei propri
   clienti proprietari dei profili"). Serve un **account Business Profile
   attivo** associato all'organizzazione che fa richiesta.
4. Attendi l'email di approvazione di Google (giorni/settimane); dopo
   l'approvazione la quota delle API passa da 0 a un valore utilizzabile
   (tipicamente 300 QPM), aumentabile da "Quotas" in Cloud Console.
5. Nella **schermata consenso OAuth** aggiungi lo scope
   `https://www.googleapis.com/auth/business.manage` e avvia la **verifica
   dell'app** per pubblicarla in produzione.
6. In **Credenziali → ID client OAuth** aggiungi l'URI di redirect
   `https://<dominio>/gbp/callback` (oltre a `/auth/callback` già usato dal
   login).

---

## Variabili d'ambiente

Il collegamento GBP **riusa le credenziali OAuth del login** "Accedi con
Google" (stesso client, scope aggiuntivo richiesto solo al momento del
collegamento):

| Variabile | Obbligatoria | Descrizione |
|---|---|---|
| `GOOGLE_OAUTH_CLIENT_ID` | sì | ID client OAuth 2.0 (lo stesso del login) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | sì | Secret del client OAuth (lo stesso del login) |
| `EKKO_BASE_URL` | consigliata in produzione | Base URL pubblica (es. `https://ekko.example.com`) usata per costruire l'URI di redirect `/gbp/callback` |
| `EKKO_SECRET_KEY` | consigliata | Firma dei cookie di sessione (lo `state` OAuth vive in sessione) |
| `EKKO_AUTHOR_HMAC_KEY` | consigliata | Chiave di pseudonimizzazione autori (già usata dal resto della pipeline) |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | no | Una qualsiasi abilita le bozze AI; senza chiavi le bozze usano il template deterministico |
| `EKKO_DISABLED_SOURCES` | no | Kill-switch: aggiungere `gbp` per disattivare il connettore |
| `EKKO_GOOGLE_REVIEWS_SINCE` | no | Cut-off recensioni Google (default `2025-01-01`): le recensioni precedenti vengono scartate sia dal connettore GBP sia da DataForSEO — vale per sync, elenco in dashboard e bozze |

Note sul flusso OAuth GBP (`/gbp/connect?business_id=...` → `/gbp/callback`):

- scope richiesto: `https://www.googleapis.com/auth/business.manage`;
- `access_type=offline` + `prompt=consent` per ottenere **sempre** il
  `refresh_token`, salvato in `oauth_tokens` e usato per rinnovare
  automaticamente l'access token scaduto;
- i token sono salvati **per agenzia (owner)**: ogni tenant collega il
  proprio account e vede solo le proprie sedi.

## Endpoint esposti (per la dashboard)

| Metodo e path | Effetto |
|---|---|
| `GET /gbp/connect?business_id=...` | redirect a Google (OAuth) |
| `GET /gbp/callback` | salva i token, redirect a `.../dashboard?gbp=connected` |
| `GET /api/gbp/status/<business_id>` | stato collegamento + location disponibili |
| `POST /api/gbp/link/<business_id>` | collega il business a una location |
| `POST /api/gbp/sync/<business_id>` | scarica le recensioni nel feedback store |
| `GET /api/gbp/reviews/<business_id>?only=unanswered` | recensioni live + bozze |
| `POST /api/gbp/draft/<business_id>/<review_id>` | genera bozza AI |
| `POST /api/gbp/draft/<business_id>/<review_id>/save` | salva bozza modificata |
| `POST /api/gbp/reply/<business_id>/<review_id>` | invia la risposta approvata |
| `GET/POST /api/gbp/settings/<business_id>` | tono, lingua, firma, template |

Gli endpoint che richiedono il collegamento rispondono
`{"ok": false, "error": "gbp_not_connected"}` (HTTP 409) se l'account non è
collegato, e `{"ok": false, "error": "gbp_not_linked"}` (HTTP 409) se il
business non è ancora associato a una location.
