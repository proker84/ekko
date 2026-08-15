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
  id TEXT PRIMARY KEY, name TEXT NOT NULL, payload TEXT NOT NULL
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


def upsert_business(business: BusinessRef) -> None:
    with get_conn() as c:
        c.execute(
            "INSERT INTO businesses(id,name,payload) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, payload=excluded.payload",
            (business.id, business.name, business.model_dump_json()),
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


def list_businesses(limit: int = 8) -> list[dict]:
    """Aziende con almeno una recensione, più recenti prima (per la home)."""
    init_db()
    with get_conn() as c:
        rows = c.execute(
            "SELECT b.id, b.name, COUNT(f.id) cnt, MAX(f.published_at) last "
            "FROM businesses b JOIN feedback f ON f.business_id=b.id "
            "GROUP BY b.id, b.name ORDER BY last DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"id": r["id"], "name": r["name"], "count": r["cnt"]} for r in rows]


def load_feedback(business_id: str) -> list[FeedbackObject]:
    with get_conn() as c:
        rows = c.execute(
            "SELECT payload FROM feedback WHERE business_id=?", (business_id,)
        ).fetchall()
    return [FeedbackObject.model_validate(json.loads(r["payload"])) for r in rows]


def save_score(business_id: str, computed_at: datetime, score: float,
               breakdown_json: str) -> None:
    with get_conn() as c:
        c.execute(
            "INSERT INTO scores(business_id,computed_at,score,breakdown) VALUES(?,?,?,?)",
            (business_id, computed_at.isoformat(), score, breakdown_json),
        )
