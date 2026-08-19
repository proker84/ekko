"""Storage — sqlite3 (stdlib) di default; il layer è un repository sottile,
pensato per essere sostituito da Postgres/SQLAlchemy in Fase 1 senza toccare
la pipeline (stesse funzioni pubbliche).

Il FeedbackObject completo è salvato come JSON (riprocessabilità);
le colonne indicizzate servono a query e scoring.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from ekko.core.models import BusinessRef, FeedbackObject


def _db_path() -> str:
    url = os.environ.get("EKKO_DATABASE_URL") or "sqlite:///ekko.db"
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    raise NotImplementedError(
        "Nello skeleton è supportato solo sqlite:///; Postgres arriva in Fase 1"
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL,
  owner_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_businesses_owner ON businesses(owner_id);
CREATE TABLE IF NOT EXISTS fb_pages (
  page_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  name TEXT,
  token TEXT NOT NULL,
  connected_at TEXT NOT NULL,
  PRIMARY KEY (page_id, owner_id)
);
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,          -- 'sub' Google (stabile)
  email TEXT,
  name TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback (
  id TEXT PRIMARY KEY,
  dedup_key TEXT NOT NULL UNIQUE,
  business_id TEXT NOT NULL,
  source TEXT NOT NULL,
  rating REAL,
  published_at TEXT NOT NULL,
  text TEXT,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_feedback_business ON feedback(business_id);
CREATE INDEX IF NOT EXISTS ix_feedback_src ON feedback(business_id, source, published_at);
CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  score REAL NOT NULL,
  breakdown TEXT NOT NULL
);
-- Google Business Profile: token OAuth per agenzia/cliente (per provider)
CREATE TABLE IF NOT EXISTS oauth_tokens (
  owner_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  access_token TEXT,
  refresh_token TEXT,
  expires_at TEXT,
  scopes TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (owner_id, provider)
);
-- collega un business Ekko a una location Google Business Profile
CREATE TABLE IF NOT EXISTS gbp_links (
  business_id TEXT PRIMARY KEY,
  account_name TEXT NOT NULL,
  location_name TEXT NOT NULL,
  location_title TEXT,
  updated_at TEXT NOT NULL
);
-- bozze di risposta alle recensioni (approvate/modificate dall'utente,
-- MAI inviate automaticamente)
CREATE TABLE IF NOT EXISTS reply_drafts (
  business_id TEXT NOT NULL,
  review_id TEXT NOT NULL,
  review_snapshot_json TEXT,
  draft_text TEXT,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent')),
  created_at TEXT NOT NULL,
  sent_at TEXT,
  PRIMARY KEY (business_id, review_id)
);
-- preferenze di risposta per business (tono, lingua, firma, template)
CREATE TABLE IF NOT EXISTS gbp_settings (
  business_id TEXT PRIMARY KEY,
  tone TEXT,
  language TEXT,
  signature TEXT,
  template TEXT,
  auto_draft INTEGER DEFAULT 0
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    Path(_db_path()).parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as c:
        c.executescript(SCHEMA)
        # migrazione difensiva: aggiunge owner_id ai DB creati prima del multi-tenant
        cols = {r["name"] for r in c.execute("PRAGMA table_info(businesses)")}
        if "owner_id" not in cols:
            c.execute("ALTER TABLE businesses ADD COLUMN owner_id TEXT")


def upsert_business(business: BusinessRef, owner_id: str | None = None) -> None:
    """Salva/aggiorna l'azienda. owner_id=None non sovrascrive il proprietario
    esistente (COALESCE): così i re-upsert interni alla pipeline conservano
    l'agenzia impostata alla prima ricerca."""
    with get_conn() as c:
        c.execute(
            "INSERT INTO businesses(id,name,payload,owner_id) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "payload=excluded.payload, "
            "owner_id=COALESCE(excluded.owner_id, businesses.owner_id)",
            (business.id, business.name, business.model_dump_json(), owner_id),
        )


def get_business_owner(business_id: str) -> str | None:
    init_db()
    with get_conn() as c:
        row = c.execute("SELECT owner_id FROM businesses WHERE id=?",
                        (business_id,)).fetchone()
    return row["owner_id"] if row else None


def upsert_fb_pages(owner_id: str, pages: list[dict], now: datetime) -> int:
    """Salva le pagine Facebook collegate da un'agenzia. Ritorna quante."""
    init_db()
    with get_conn() as c:
        for p in pages:
            c.execute(
                "INSERT INTO fb_pages(page_id,owner_id,name,token,connected_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(page_id,owner_id) DO UPDATE SET "
                "name=excluded.name, token=excluded.token",
                (p["id"], owner_id, p.get("name"), p["token"], now.isoformat()))
    return len(pages)


def list_fb_pages(owner_id: str) -> list[dict]:
    init_db()
    with get_conn() as c:
        rows = c.execute(
            "SELECT page_id, name, token FROM fb_pages WHERE owner_id=? "
            "ORDER BY name", (owner_id,)).fetchall()
    return [{"id": r["page_id"], "name": r["name"], "token": r["token"]}
            for r in rows]


def upsert_user(sub: str, email: str | None, name: str | None,
                created_at: datetime) -> None:
    with get_conn() as c:
        c.execute(
            "INSERT INTO users(id,email,name,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET email=excluded.email, name=excluded.name",
            (sub, email, name, created_at.isoformat()),
        )


def insert_feedback(fo: FeedbackObject) -> bool:
    """True se inserito, False se duplicato (dedup su chiave)."""
    with get_conn() as c:
        try:
            c.execute(
                "INSERT INTO feedback(id,dedup_key,business_id,source,rating,"
                "published_at,text,payload) VALUES(?,?,?,?,?,?,?,?)",
                (fo.id, fo.dedup_key(), fo.business_id, fo.source.value,
                 fo.rating, fo.published_at.isoformat(), fo.text,
                 fo.model_dump_json()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def count_by_source(business_id: str) -> dict:
    """Numero di recensioni salvate per fonte (per le progress bar)."""
    init_db()
    with get_conn() as c:
        rows = c.execute(
            "SELECT source, COUNT(*) n FROM feedback WHERE business_id=? GROUP BY source",
            (business_id,)).fetchall()
    return {r["source"]: r["n"] for r in rows}


def max_published(business_id: str, source: str) -> datetime | None:
    with get_conn() as c:
        row = c.execute(
            "SELECT MAX(published_at) m FROM feedback WHERE business_id=? AND source=?",
            (business_id, source),
        ).fetchone()
    return datetime.fromisoformat(row["m"]) if row and row["m"] else None


def get_business_payload(business_id: str) -> dict | None:
    init_db()
    with get_conn() as c:
        row = c.execute("SELECT payload FROM businesses WHERE id=?",
                        (business_id,)).fetchone()
    import json as _j
    return _j.loads(row["payload"]) if row else None


def get_business_name(business_id: str) -> str | None:
    init_db()
    with get_conn() as c:
        row = c.execute("SELECT name FROM businesses WHERE id=?",
                        (business_id,)).fetchone()
    return row["name"] if row else None


def list_businesses(limit: int = 8, owner_id: str | None = None) -> list[dict]:
    """Aziende con almeno una recensione, più recenti prima (per la home).
    Se owner_id è passato, mostra SOLO le aziende di quell'agenzia (isolamento)."""
    init_db()
    where = "WHERE b.owner_id=?" if owner_id is not None else ""
    args: tuple = (owner_id, limit) if owner_id is not None else (limit,)
    with get_conn() as c:
        rows = c.execute(
            "SELECT b.id, b.name, COUNT(f.id) cnt, MAX(f.published_at) last "
            "FROM businesses b JOIN feedback f ON f.business_id=b.id "
            f"{where} "
            "GROUP BY b.id, b.name ORDER BY last DESC LIMIT ?", args
        ).fetchall()
    return [{"id": r["id"], "name": r["name"], "count": r["cnt"]} for r in rows]


def load_feedback(business_id: str) -> list[FeedbackObject]:
    with get_conn() as c:
        rows = c.execute(
            "SELECT payload FROM feedback WHERE business_id=?", (business_id,)
        ).fetchall()
    return [FeedbackObject.model_validate(json.loads(r["payload"])) for r in rows]


# --------------------------------------------------------------------------
# Google Business Profile (Fase 2): token OAuth, link business→location,
# bozze di risposta e preferenze.
# --------------------------------------------------------------------------
def upsert_oauth_token(owner_id: str, provider: str, access_token: str | None,
                       refresh_token: str | None, expires_at: str | None,
                       scopes: str | None, now: datetime) -> None:
    """Salva/aggiorna i token OAuth di un'agenzia per un provider.

    refresh_token=None NON sovrascrive quello esistente (COALESCE): Google
    lo restituisce solo al consenso, i refresh successivi non lo includono."""
    init_db()
    with get_conn() as c:
        c.execute(
            "INSERT INTO oauth_tokens(owner_id,provider,access_token,"
            "refresh_token,expires_at,scopes,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(owner_id,provider) DO UPDATE SET "
            "access_token=excluded.access_token, "
            "refresh_token=COALESCE(excluded.refresh_token, oauth_tokens.refresh_token), "
            "expires_at=excluded.expires_at, scopes=excluded.scopes, "
            "updated_at=excluded.updated_at",
            (owner_id, provider, access_token, refresh_token, expires_at,
             scopes, now.isoformat()),
        )


def get_oauth_token(owner_id: str, provider: str) -> dict | None:
    init_db()
    with get_conn() as c:
        row = c.execute(
            "SELECT access_token, refresh_token, expires_at, scopes "
            "FROM oauth_tokens WHERE owner_id=? AND provider=?",
            (owner_id, provider)).fetchone()
    return dict(row) if row else None


def upsert_gbp_link(business_id: str, account_name: str, location_name: str,
                    location_title: str | None, now: datetime) -> None:
    """Collega (o ricollega) un business Ekko a una location GBP."""
    init_db()
    with get_conn() as c:
        c.execute(
            "INSERT INTO gbp_links(business_id,account_name,location_name,"
            "location_title,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(business_id) DO UPDATE SET "
            "account_name=excluded.account_name, "
            "location_name=excluded.location_name, "
            "location_title=excluded.location_title, "
            "updated_at=excluded.updated_at",
            (business_id, account_name, location_name, location_title,
             now.isoformat()),
        )


def get_gbp_link(business_id: str) -> dict | None:
    init_db()
    with get_conn() as c:
        row = c.execute(
            "SELECT account_name, location_name, location_title "
            "FROM gbp_links WHERE business_id=?", (business_id,)).fetchone()
    return dict(row) if row else None


def upsert_reply_draft(business_id: str, review_id: str,
                       review_snapshot_json: str | None, draft_text: str,
                       now: datetime) -> None:
    """Salva/aggiorna una bozza di risposta (stato torna a 'draft').

    snapshot=None non sovrascrive lo snapshot esistente (COALESCE): il
    salvataggio di una bozza modificata non deve perdere il contesto."""
    init_db()
    with get_conn() as c:
        c.execute(
            "INSERT INTO reply_drafts(business_id,review_id,"
            "review_snapshot_json,draft_text,status,created_at) "
            "VALUES(?,?,?,?,'draft',?) "
            "ON CONFLICT(business_id,review_id) DO UPDATE SET "
            "review_snapshot_json=COALESCE(excluded.review_snapshot_json, "
            "reply_drafts.review_snapshot_json), "
            "draft_text=excluded.draft_text, status='draft', sent_at=NULL",
            (business_id, review_id, review_snapshot_json, draft_text,
             now.isoformat()),
        )


def get_reply_draft(business_id: str, review_id: str) -> dict | None:
    init_db()
    with get_conn() as c:
        row = c.execute(
            "SELECT draft_text, status, review_snapshot_json, sent_at "
            "FROM reply_drafts WHERE business_id=? AND review_id=?",
            (business_id, review_id)).fetchone()
    return dict(row) if row else None


def list_reply_drafts(business_id: str) -> dict[str, dict]:
    """Bozze per business: {review_id: {"text","status"}} (per la UI)."""
    init_db()
    with get_conn() as c:
        rows = c.execute(
            "SELECT review_id, draft_text, status FROM reply_drafts "
            "WHERE business_id=?", (business_id,)).fetchall()
    return {r["review_id"]: {"text": r["draft_text"], "status": r["status"]}
            for r in rows}


def mark_reply_sent(business_id: str, review_id: str, text: str,
                    now: datetime) -> None:
    """Marca la risposta come inviata (upsert: anche senza bozza precedente)."""
    init_db()
    with get_conn() as c:
        c.execute(
            "INSERT INTO reply_drafts(business_id,review_id,draft_text,"
            "status,created_at,sent_at) VALUES(?,?,?,'sent',?,?) "
            "ON CONFLICT(business_id,review_id) DO UPDATE SET "
            "draft_text=excluded.draft_text, status='sent', "
            "sent_at=excluded.sent_at",
            (business_id, review_id, text, now.isoformat(), now.isoformat()),
        )


GBP_SETTINGS_DEFAULTS = {
    "tone": "professionale e cordiale",
    "language": "it",
    "signature": "",
    "template": "",
    "auto_draft": 0,
}


def get_gbp_settings(business_id: str) -> dict:
    """Preferenze di risposta del business (con default se mai salvate)."""
    init_db()
    with get_conn() as c:
        row = c.execute(
            "SELECT tone, language, signature, template, auto_draft "
            "FROM gbp_settings WHERE business_id=?", (business_id,)).fetchone()
    if not row:
        return dict(GBP_SETTINGS_DEFAULTS)
    out = dict(GBP_SETTINGS_DEFAULTS)
    for k in out:
        if row[k] is not None:
            out[k] = row[k]
    return out


def upsert_gbp_settings(business_id: str, settings: dict) -> None:
    init_db()
    merged = dict(GBP_SETTINGS_DEFAULTS)
    merged.update({k: v for k, v in (settings or {}).items()
                   if k in GBP_SETTINGS_DEFAULTS and v is not None})
    with get_conn() as c:
        c.execute(
            "INSERT INTO gbp_settings(business_id,tone,language,signature,"
            "template,auto_draft) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(business_id) DO UPDATE SET tone=excluded.tone, "
            "language=excluded.language, signature=excluded.signature, "
            "template=excluded.template, auto_draft=excluded.auto_draft",
            (business_id, merged["tone"], merged["language"],
             merged["signature"], merged["template"],
             int(bool(merged["auto_draft"]))),
        )


def save_score(business_id: str, computed_at: datetime, score: float,
               breakdown_json: str) -> None:
    with get_conn() as c:
        c.execute(
            "INSERT INTO scores(business_id,computed_at,score,breakdown) VALUES(?,?,?,?)",
            (business_id, computed_at.isoformat(), score, breakdown_json),
        )
