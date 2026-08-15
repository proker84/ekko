"""Test della modalità reale: AI Gateway, route /search e /analyze, connettore
pubblico. Non chiamano rete reale: verificano config, routing e fallback."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

os.environ["EKKO_DATABASE_URL"] = "sqlite:///test_ekko_real.db"

from ekko.ai.gateway import AIGateway
from ekko.api.main import app
from ekko.connectors import trustpilot_public


class TestGateway(unittest.TestCase):
    def tearDown(self):
        for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
                  "EKKO_AI_PROVIDER", "EKKO_AI_MODEL"):
            os.environ.pop(k, None)

    def test_unavailable_without_keys(self):
        gw = AIGateway()
        self.assertFalse(gw.available())

    def test_detects_provider_and_default_model(self):
        os.environ["OPENAI_API_KEY"] = "sk-test"
        gw = AIGateway()
        self.assertTrue(gw.available())
        self.assertEqual(gw.provider, "openai")
        self.assertEqual(gw.model, "gpt-4o-mini")

    def test_priority_anthropic_first(self):
        os.environ["OPENAI_API_KEY"] = "x"
        os.environ["ANTHROPIC_API_KEY"] = "y"
        self.assertEqual(AIGateway().provider, "anthropic")

    def test_prompt_is_pii_free(self):
        os.environ["ANTHROPIC_API_KEY"] = "x"
        gw = AIGateway()
        rows = [{"d": "2026-08-01T00:00:00+00:00", "s": "google", "st": 5,
                 "rep": False, "topics": ["prezzo.valore"], "txt": "Ottimo",
                 "author_hash": "SHOULD_NOT_APPEAR"}]
        prompt = gw._build_prompt("Acme", rows)
        self.assertNotIn("author", prompt.lower())
        self.assertNotIn("SHOULD_NOT_APPEAR", prompt)
        self.assertIn("Acme", prompt)

    def test_parse_json_tolerates_fences(self):
        raw = '```json\n{"findings": [{"sev":"good","title":"t","detail":"d","action":"a"}], "suggestions": []}\n```'
        data = AIGateway._parse_json(raw)
        self.assertEqual(len(data["findings"]), 1)
        self.assertEqual(data["suggestions"], [])

    def test_parse_json_drops_bad_severity(self):
        raw = '{"findings":[{"sev":"weird","title":"t"},{"sev":"critical","title":"ok","detail":"d","action":"a"}]}'
        data = AIGateway._parse_json(raw)
        self.assertEqual([f["sev"] for f in data["findings"]], ["critical"])


class TestRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["EKKO_DATABASE_URL"] = "sqlite:///test_ekko_real.db"
        Path("test_ekko_real.db").unlink(missing_ok=True)
        app.config.update(TESTING=True)
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        Path("test_ekko_real.db").unlink(missing_ok=True)

    def test_home_renders_search(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Cerca", r.data.replace(b"cerca", b"Cerca") if False else r.data)
        self.assertIn(b"Analizza", r.data)

    def test_search_ingests_and_redirects_to_dashboard(self):
        r = self.client.post("/search", data={"name": "Bar Centrale Test",
                                               "city": "Milano"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/businesses/bar-centrale-test/dashboard", r.location)
        # la dashboard è ora disponibile (demo data ingeriti)
        d = self.client.get("/businesses/bar-centrale-test/dashboard")
        self.assertEqual(d.status_code, 200)
        self.assertIn(b"Bar Centrale Test", d.data)

    def test_analyze_without_ai_returns_graceful(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
        r = self.client.post("/businesses/x/analyze",
                             json={"rows": [{"d": "2026-01-01", "s": "google",
                                             "st": 5, "txt": "ok", "topics": []}]})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], "ai_not_configured")

    def test_analyze_uses_gateway_when_available(self):
        os.environ["ANTHROPIC_API_KEY"] = "x"
        fake = {"findings": [{"sev": "good", "title": "T", "detail": "D",
                              "action": "A"}], "suggestions": [], "summary": "ok"}
        with mock.patch.object(AIGateway, "_call", return_value=json.dumps(fake)):
            r = self.client.post("/businesses/x/analyze",
                                 json={"business_name": "X",
                                       "rows": [{"d": "2026-01-01", "s": "google",
                                                 "st": 5, "txt": "Manca il parcheggio",
                                                 "topics": []}]})
        os.environ.pop("ANTHROPIC_API_KEY", None)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["findings"][0]["title"], "T")
        self.assertTrue(body["mode"].startswith("anthropic"))


class TestPublicConnector(unittest.TestCase):
    def test_disabled_by_default(self):
        os.environ.pop("EKKO_ENABLE_PUBLIC_TRUSTPILOT", None)
        self.assertFalse(trustpilot_public.enabled())

    def test_enabled_with_flag(self):
        os.environ["EKKO_ENABLE_PUBLIC_TRUSTPILOT"] = "1"
        self.assertTrue(trustpilot_public.enabled())
        os.environ.pop("EKKO_ENABLE_PUBLIC_TRUSTPILOT", None)

    def test_extract_reviews_parses_next_data(self):
        from ekko.connectors.trustpilot_public import TrustpilotPublicConnector
        html = ('<script id="__NEXT_DATA__" type="application/json">'
                '{"props":{"pageProps":{"reviews":[{"id":"1","rating":5}]}}}'
                '</script>')
        revs = TrustpilotPublicConnector._extract_reviews(html)
        self.assertEqual(len(revs), 1)
        self.assertEqual(revs[0]["rating"], 5)


if __name__ == "__main__":
    unittest.main()
