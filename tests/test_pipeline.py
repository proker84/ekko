import os
import unittest
from pathlib import Path

os.environ["EKKO_DATABASE_URL"] = "sqlite:///test_ekko.db"

from ekko.connectors.google import GoogleConnector
from ekko.connectors.trustpilot import TrustpilotConnector
from ekko.core.models import BusinessRef
from ekko.ingestion.pipeline import ingest, load_feedback, score_business


def connectors():
    # senza API key i connettori vanno in modalità demo
    return [GoogleConnector(api_key=None), TrustpilotConnector(api_key=None)]


class TestPipeline(unittest.TestCase):
    def setUp(self):
        os.environ["EKKO_DATABASE_URL"] = "sqlite:///test_ekko.db"
        Path("test_ekko.db").unlink(missing_ok=True)
        self.business = BusinessRef(id="test-biz", name="Test Biz", city="Milano")

    def tearDown(self):
        Path("test_ekko.db").unlink(missing_ok=True)
        os.environ.pop("EKKO_DISABLED_SOURCES", None)

    def test_ingest_demo_end_to_end(self):
        report = ingest(self.business, connectors())
        self.assertGreater(report.stored, 100)
        self.assertEqual({r["source"] for r in report.runs},
                         {"google", "trustpilot"})
        feedback = load_feedback(self.business.id)
        self.assertEqual(len(feedback), report.stored)
        # stadio 0 applicato
        self.assertTrue(all(f.enrichment.sentiment is not None
                            for f in feedback if f.rating))
        self.assertTrue(any(f.enrichment.topics for f in feedback))
        # pseudonimizzazione: nessun autore in chiaro
        self.assertTrue(all(len(f.author_hash) == 32 for f in feedback))

    def test_reingest_is_idempotent(self):
        first = ingest(self.business, connectors())
        second = ingest(self.business, connectors())
        self.assertEqual(second.stored, 0)
        self.assertEqual(second.duplicates, first.stored)

    def test_score_after_ingest(self):
        ingest(self.business, connectors())
        b = score_business(self.business.id)
        self.assertTrue(0 <= b.score <= 100)
        self.assertGreater(b.n_feedback, 100)
        self.assertTrue(b.by_source and b.explanations)

    def test_kill_switch(self):
        os.environ["EKKO_DISABLED_SOURCES"] = "google"
        report = ingest(self.business, connectors())
        google_run = next(r for r in report.runs if r["source"] == "google")
        self.assertTrue(google_run["status"].startswith("disabled"))
        tp_run = next(r for r in report.runs if r["source"] == "trustpilot")
        self.assertEqual(tp_run["status"], "ok")
        self.assertGreater(tp_run["fetched"], 0)


if __name__ == "__main__":
    unittest.main()
