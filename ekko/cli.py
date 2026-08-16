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


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if cmd == "ingest":
        run_ingest(" ".join(sys.argv[2:]) or "demo")
    elif cmd == "as24test":
        run_as24test(" ".join(sys.argv[2:]) or "autosport-snc")
    else:
        run_demo()
