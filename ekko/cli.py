"""CLI Ekko — demo end-to-end senza API key.

  python -m ekko.cli demo            ingestion demo + score + report HTML
  python -m ekko.cli ingest <nome>   ingestion reale (richiede API key in .env)
"""
from __future__ import annotations

import sys
from pathlib import Path

from ekko.api.main import render_dashboard, render_report
from ekko.connectors.google import GoogleConnector
from ekko.connectors.trustpilot import TrustpilotConnector
from ekko.core.models import BusinessRef
from ekko.ingestion.pipeline import ingest, load_feedback, score_business


def run_demo() -> Path:
    business = BusinessRef(id="demo-ristorante-da-mario", name="Ristorante Da Mario",
                           city="Milano", vertical="ristorazione")
    report = ingest(business, [GoogleConnector(), TrustpilotConnector()])
    print(f"Ingestion: {report.stored} nuovi, {report.duplicates} duplicati, "
          f"costo €{report.total_cost_eur:.2f}")
    for r in report.runs:
        print(f"  - {r['source']}: {r['fetched']} fetch ({r['status']})")

    breakdown = score_business(business.id)
    print(f"\nEkko Score: {breakdown.score}/100")
    for e in breakdown.explanations:
        print(f"  · {e}")

    feedback = load_feedback(business.id)
    html = render_report(business.id, breakdown, feedback)
    out = Path("report_demo.html")
    out.write_text(html)
    print(f"\nReport statico: {out.resolve()}")

    dash = Path("dashboard_demo.html")
    dash.write_text(render_dashboard(business.name, breakdown, feedback))
    print(f"Dashboard interattiva: {dash.resolve()}")
    return out


def run_ingest(name: str) -> None:
    business = BusinessRef(id=name.lower().replace(" ", "-"), name=name)
    report = ingest(business, [GoogleConnector(), TrustpilotConnector()])
    print(report)
    print(score_business(business.id).model_dump_json(indent=2))


def run_as24test(url_or_name: str) -> None:
    """Diagnostica lo scraper AutoScout24 su un concessionario reale (no DB)."""
    import json as _json
    from ekko.connectors.autoscout24 import diagnose
    print(f"Test scraper AutoScout24 su: {url_or_name}\n")
    result = diagnose(url_or_name)
    print(_json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result.get("ok"):
        print(f"\n✓ Funziona: {result['reviews_total']} recensioni "
              f"(metodo: {result['method']}).")
    else:
        print("\n✗ Nessuna recensione estratta. Vedi 'hint'/'error' qui sopra.")


def run_tptest(domain: str) -> None:
    """Diagnostica lo scraper Trustpilot pubblico su un dominio reale (no DB)."""
    import json as _json
    from ekko.connectors.trustpilot_public import diagnose
    print(f"Test scraper Trustpilot pubblico su: {domain}\n")
    result = diagnose(domain)
    print(_json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result.get("ok"):
        print(f"\n✓ Funziona: {result['reviews_in_page']} recensioni nella "
              f"prima pagina (metodo: {result['method']}).")
    else:
        print("\n✗ Nessuna recensione estratta. Vedi 'hint'/'error' qui sopra.")


def run_fbtest(url: str) -> None:
    """Diagnostica il connettore Facebook via provider (nessun login cliente).
      python -m ekko.cli fbtest https://www.facebook.com/nomepagina
    """
    import json as _json
    from ekko.connectors.facebook_public import diagnose
    if not url:
        print("Uso: python -m ekko.cli fbtest <url pagina facebook>")
        return
    print(f"Test raccolta Facebook (provider dati) su: {url}\n")
    result = diagnose(url)
    print(_json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result.get("ok"):
        print(f"\n✓ Funziona: {result['normalizzate']} recensioni "
              f"in ~{result['secondi']}s.")
    else:
        print("\n✗ Non riuscito. Vedi 'error' qui sopra.")


def run_dfstest(keyword: str) -> None:
    """Diagnostica DataForSEO: posta un task Google Reviews e mostra la
    risposta esatta (status_code/status_message) + il risultato al polling.
      python -m ekko.cli dfstest "Pasquarelli Auto Volkswagen San Giovanni Teatino"
    """
    import json as _json
    import os
    import time
    import httpx
    auth = os.environ.get("DATAFORSEO_AUTH")
    if not auth:
        print("✗ DATAFORSEO_AUTH non impostata (esegui con: bash run.sh)")
        return
    base = "https://api.dataforseo.com/v3/business_data/google/reviews"
    hdr = {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}
    task = {"keyword": keyword, "location_name": "Italy",
            "language_code": "it", "depth": 20, "priority": 2}
    print(f"→ task_post: {_json.dumps(task, ensure_ascii=False)}\n")
    r = httpx.post(f"{base}/task_post", headers=hdr, json=[task], timeout=30)
    body = r.json()
    t = (body.get("tasks") or [{}])[0]
    print(f"HTTP {r.status_code} · task status: {t.get('status_code')} "
          f"{t.get('status_message')}")
    tid = t.get("id")
    if not tid or t.get("status_code") not in (20000, 20100):
        print("✗ Task rifiutato: vedi il messaggio qui sopra.")
        return
    print(f"task_id={tid}\nAttendo il risultato (max ~4 min)…")
    for i in range(24):
        time.sleep(10)
        g = httpx.get(f"{base}/task_get/{tid}", headers=hdr, timeout=30).json()
        gt = (g.get("tasks") or [{}])[0]
        sc = gt.get("status_code")
        if sc == 20000 and gt.get("result"):
            res = gt["result"][0] or {}
            items = res.get("items") or []
            print(f"\n✓ PRONTO dopo ~{(i+1)*10}s")
            print(f"  scheda trovata : {res.get('title')}")
            print(f"  indirizzo      : {res.get('address')}")
            print(f"  rating         : {(res.get('rating') or {}).get('value')}")
            print(f"  recensioni tot : {res.get('reviews_count')}")
            print(f"  recensioni scaricate: {len(items)}")
            if items:
                it = items[0]
                print(f"  esempio        : {(it.get('review_text') or '')[:80]}")
            return
        if sc not in (20100, 40601, 40602):
            print(f"\n✗ Errore terminale: {sc} {gt.get('status_message')}")
            return
        print(f"  …in coda ({(i+1)*10}s)")
    print("\n✗ Timeout: task ancora in coda dopo 4 minuti.")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if cmd == "ingest":
        run_ingest(" ".join(sys.argv[2:]) or "demo")
    elif cmd == "as24test":
        run_as24test(" ".join(sys.argv[2:]) or "autosport-snc")
    elif cmd == "tptest":
        run_tptest(sys.argv[2] if len(sys.argv) > 2 else "unieuro.it")
    elif cmd == "fbtest":
        run_fbtest(sys.argv[2] if len(sys.argv) > 2 else "")
    elif cmd == "dfstest":
        run_dfstest(" ".join(sys.argv[2:]) or "Eataly Milano")
    else:
        run_demo()
