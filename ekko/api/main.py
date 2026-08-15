"""API Ekko — walking skeleton (Flask; migrazione a FastAPI quando il
registro pacchetti è di nuovo raggiungibile — stessa forma delle route).

  GET  /                             pagina di ricerca (motore "cerca un'azienda")
  POST /search                       nome azienda -> ingestion -> dashboard
  POST /businesses                   registra un'azienda target (JSON)
  POST /businesses/<id>/ingest       esegue l'ingestion
  POST /businesses/<id>/analyze      analisi AI (stadio 3) sull'insieme filtrato
  GET  /businesses/<id>/score        Ekko Score con breakdown
  GET  /businesses/<id>/dashboard    dashboard interattiva
  GET  /businesses/<id>/report       report statico
  GET  /health                       stato connettori + AI

Avvio:  python -m ekko.api.main   (porta 8000)
"""
from __future__ import annotations

import os
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from flask import Flask, jsonify, redirect, request
from jinja2 import Environment, FileSystemLoader

from ekko.ai.gateway import AIGateway
from ekko.connectors.google import GoogleConnector
from ekko.connectors.trustpilot import TrustpilotConnector
from ekko.connectors import trustpilot_public
from ekko.core.models import BusinessRef
from ekko.ingestion.pipeline import (ingest, load_feedback, save_business,
                                     score_business)
from ekko.storage import db

app = Flask("ekko")
_templates = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
    autoescape=True,
)


def default_connectors() -> list:
    from ekko.connectors import dataforseo
    conns = []
    if dataforseo.enabled():
        conns.append(dataforseo.DataForSeoGoogleConnector())  # recensioni Google complete
    else:
        conns.append(GoogleConnector())                       # API ufficiale (max ~5)
    if os.environ.get("TRUSTPILOT_API_KEY"):
        conns.append(TrustpilotConnector())
    if trustpilot_public.enabled():
        conns.append(trustpilot_public.TrustpilotPublicConnector())
    return conns


def _resolve_place_id(business):
    """Risolve il Google place_id (match esatto) da passare a DataForSEO."""
    try:
        if not business.google_place_id and os.environ.get("GOOGLE_MAPS_API_KEY"):
            pid = GoogleConnector().resolve_place_id(business)
            if pid:
                business.google_place_id = pid
    except Exception:
        pass
    return business


def _slugify(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "azienda"


@app.get("/health")
def health():
    gw = AIGateway()
    return jsonify(status="ok",
                   ai={"available": gw.available(), "provider": gw.provider,
                       "model": gw.model},
                   connectors=[c.health() for c in default_connectors()])


@app.get("/")
def home():
    gw = AIGateway()
    tpl = _templates.get_template("search.html")
    return tpl.render(
        recent=db.list_businesses(),
        google_on=bool(os.environ.get("GOOGLE_MAPS_API_KEY")),
        tp_on=bool(os.environ.get("TRUSTPILOT_API_KEY")),
        tp_public_on=trustpilot_public.enabled(),
        ai_on=gw.available(),
        ai_label=f" · {gw.provider}" if gw.available() else "",
    )


@app.post("/search")
def search():
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect("/")
    business = BusinessRef(
        id=_slugify(name),
        name=name,
        city=(request.form.get("city") or "").strip() or None,
        domain=(request.form.get("domain") or "").strip() or None,
    )
    save_business(business)
    from ekko.connectors import dataforseo as _dfs
    # 1) task DataForSEO asincrono (recensioni Google complete): parte in background
    if _dfs.enabled():
        tid = _dfs.post_task(business)
        if tid:
            business.dfs_task_id = tid
            business.dfs_pending = True
            save_business(business)
    # 2) fonti veloci subito (Trustpilot pubblico / Google API), NON DataForSEO
    fast = [c for c in default_connectors()
            if type(c).__name__ != "DataForSeoGoogleConnector"]
    if fast:
        ingest(business, fast)
    return redirect(f"/businesses/{business.id}/dashboard")


@app.post("/businesses/<business_id>/analyze")
def analyze(business_id: str):
    """Endpoint AI stadio 3: recensioni filtrate (PII-free) -> findings+suggestions.
    Il body è la lista già filtrata lato client. Se l'AI non è configurata o
    fallisce, il client resta sul motore locale (degrada con grazia)."""
    gw = AIGateway()
    if not gw.available():
        return jsonify(ok=False, reason="ai_not_configured")
    payload = request.get_json(force=True) or {}
    rows = payload.get("rows", [])
    name = payload.get("business_name", business_id)
    if not rows:
        return jsonify(ok=False, reason="no_rows")
    try:
        result = gw.analyze(name, rows)
        return jsonify(ok=True, **result)
    except Exception as e:  # degrada con grazia -> il client usa il motore locale
        return jsonify(ok=False, reason="ai_error", detail=str(e)[:200])


@app.post("/businesses")
def create_business():
    business = BusinessRef.model_validate(request.get_json(force=True))
    save_business(business)
    return jsonify(created=business.id)


@app.post("/businesses/<business_id>/ingest")
def run_ingest(business_id: str):
    payload = request.get_json(silent=True)
    b = (BusinessRef.model_validate(payload) if payload
         else BusinessRef(id=business_id, name=business_id))
    if b.id != business_id:
        return jsonify(error="business.id non coincide con il path"), 400
    return jsonify(asdict(ingest(b, default_connectors())))


@app.get("/businesses/<business_id>/score")
def get_score(business_id: str):
    breakdown = score_business(business_id)
    if breakdown.n_feedback == 0:
        return jsonify(error="Nessun feedback: eseguire prima l'ingestion"), 404
    return app.response_class(breakdown.model_dump_json(),
                              mimetype="application/json")


@app.get("/businesses/<business_id>/dashboard")
def get_dashboard(business_id: str):
    payload = db.get_business_payload(business_id) or {}
    pending = False
    if payload.get("dfs_pending") and payload.get("dfs_task_id"):
        from ekko.connectors import dataforseo as _dfs
        from ekko.connectors.base import ConnectorRun
        from ekko.core.sentiment import enrich_stage0
        items, total = _dfs.collect(payload["dfs_task_id"])
        if items is not None:  # DataForSEO pronto -> ingerisci
            biz = BusinessRef.model_validate(payload)
            run = ConnectorRun()
            for fo in _dfs.normalize_items(items, biz, run):
                db.insert_feedback(enrich_stage0(fo))
            biz.dfs_pending = False
            if total:
                biz.total_reviews_google = int(total)
            db.upsert_business(biz)
            payload = db.get_business_payload(business_id) or payload
        else:
            pending = True
    feedback = load_feedback(business_id)
    if not feedback and not pending:
        return jsonify(error="Nessun feedback: eseguire prima l'ingestion"), 404
    breakdown = score_business(business_id)
    return render_dashboard(payload.get("name") or business_id, breakdown, feedback,
                            business_id=business_id,
                            total_reviews=payload.get("total_reviews_google"),
                            pending=pending)


def render_dashboard(business_name: str, breakdown, feedback,
                     business_id: str | None = None, total_reviews=None,
                     pending: bool = False) -> str:
    """Dashboard interattiva self-contained: dati embedded, zero dipendenze esterne."""
    import json
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    data = {
        "generated_ts": now.isoformat(),
        "business_id": business_id or _slugify(business_name),
        "business_name": business_name,
        "feedback": [
            {
                "d": f.published_at.isoformat(),
                "s": f.source.value,
                "st": round((f.rating or 0) * 5, 1),
                "sent": f.enrichment.sentiment if f.enrichment.sentiment is not None else 0,
                "topics": f.enrichment.topics,
                "rep": f.reply is not None,
                "txt": f.text,
            }
            for f in feedback
        ],
    }
    tpl = _templates.get_template("dashboard.html")
    return tpl.render(
        business_name=business_name,
        generated_at=now.strftime("%d/%m/%Y %H:%M UTC"),
        score=breakdown,
        total_reviews=total_reviews,
        analyzed_count=len(feedback),
        pending=pending,
        data_json=json.dumps(data, ensure_ascii=False).replace("</", "<\\/"),
    )


@app.get("/businesses/<business_id>/report")
def get_report(business_id: str):
    feedback = load_feedback(business_id)
    if not feedback:
        return jsonify(error="Nessun feedback: eseguire prima l'ingestion"), 404
    breakdown = score_business(business_id)
    return render_report(business_id, breakdown, feedback)


def render_report(business_id: str, breakdown, feedback) -> str:
    topics = Counter()
    neg_topics = Counter()
    for f in feedback:
        for t in f.enrichment.topics:
            topics[t] += 1
            if (f.enrichment.sentiment or 0) < -0.2:
                neg_topics[t] += 1
    recent = sorted(feedback, key=lambda f: f.published_at, reverse=True)[:12]
    tpl = _templates.get_template("report.html")
    return tpl.render(
        business_id=business_id, b=breakdown,
        top_topics=topics.most_common(8),
        neg_topics=neg_topics.most_common(5),
        recent=recent,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
