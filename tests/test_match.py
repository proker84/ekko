"""Test dello step di identificazione (matching + endpoint /match + skip)."""
import os
import tempfile
import unittest

from ekko.core import matching


class MatchingTest(unittest.TestCase):
    def test_exact_with_city_is_100(self):
        c = matching.confidence("Fratelli Di Muzio", "Fratelli Di Muzio",
                                city="Chieti",
                                cand_detail="Via Roma 1, Chieti CH, Italia")
        self.assertEqual(c, 100)

    def test_legal_forms_ignored(self):
        self.assertGreaterEqual(
            matching.similarity("Rossi Auto S.r.l.", "ROSSI AUTO"), 0.99)

    def test_containment_bonus(self):
        s = matching.similarity("Rossi Auto", "Rossi Auto di Mario Rossi & C.")
        self.assertGreaterEqual(s, 0.9)

    def test_different_business_low(self):
        c = matching.confidence("Rossi Auto", "Pizzeria Da Gennaro",
                                city="Milano", cand_detail="Napoli")
        self.assertLess(c, 50)

    def test_wrong_city_penalized(self):
        with_city = matching.confidence("Bar Sport", "Bar Sport",
                                        city="Torino", cand_detail="Via X, Roma")
        self.assertLess(with_city, matching.AUTO_THRESHOLD)


class MatchEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        os.environ["EKKO_DATABASE_URL"] = f"sqlite:///{self.tmp.name}"
        for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
                  "DATAFORSEO_AUTH", "TRUSTPILOT_API_KEY", "FACEBOOK_PAGE_TOKEN",
                  "EKKO_ENABLE_FEEDATY", "EKKO_ENABLE_RECENSIONI_VERIFICATE"):
            os.environ.pop(k, None)
        os.environ["GOOGLE_MAPS_API_KEY"] = "test-key"
        os.environ["EKKO_ENABLE_PUBLIC_TRUSTPILOT"] = "1"
        os.environ["EKKO_ENABLE_AUTOSCOUT24"] = "1"
        import importlib
        from ekko.storage import db
        importlib.reload(db)
        db.init_db()
        from ekko.api import main
        importlib.reload(main)
        self.main = main
        # stub dei cercatori: niente rete nei test
        main._match_google = lambda name, city: [
            {"token": "pid1", "label": name, "detail": f"Via Roma, {city}",
             "conf": 100},
            {"token": "pid2", "label": name + " 2", "detail": "altrove", "conf": 60}]
        main._match_trustpilot = lambda name, domain, city: (
            [{"token": domain, "label": name, "detail": "tp", "conf": 82}]
            if domain else [])
        main._match_autoscout24 = lambda name, url, city: []
        main.default_connectors = lambda: []
        import ekko.connectors.dataforseo as dfs
        self.dfs = dfs

    def tearDown(self):
        os.unlink(self.tmp.name)
        for k in ("GOOGLE_MAPS_API_KEY", "EKKO_ENABLE_PUBLIC_TRUSTPILOT",
                  "EKKO_ENABLE_AUTOSCOUT24"):
            os.environ.pop(k, None)

    def test_match_payload(self):
        c = self.main.app.test_client()
        r = c.post("/match", data={"name": "Acme", "city": "Milano",
                                   "domain": "acme.it"})
        j = r.get_json()
        self.assertTrue(j["ok"])
        by_key = {s["key"]: s for s in j["sources"]}
        # google: top 100 e distacco >=10 -> auto
        self.assertTrue(by_key["google"]["auto"])
        self.assertEqual(by_key["google"]["candidates"][0]["conf"], 100)
        # trustpilot: 82 -> scelta manuale
        self.assertFalse(by_key["trustpilot"]["auto"])
        # autoscout24: nessun candidato -> hint
        self.assertEqual(by_key["autoscout24"]["candidates"], [])
        self.assertTrue(by_key["autoscout24"]["none_hint"])

    def test_search_applies_choices_and_skips(self):
        c = self.main.app.test_client()
        r = c.post("/search", data={"name": "Acme", "city": "Milano",
                                    "google_place_id": "pid1",
                                    "skip_sources": "trustpilot,autoscout24"},
                   headers={"X-Requested-With": "fetch"})
        j = r.get_json()
        self.assertTrue(j["ok"])
        from ekko.storage import db
        payload = db.get_business_payload(j["id"])
        self.assertEqual(payload["google_place_id"], "pid1")
        self.assertEqual(sorted(payload["skipped_sources"]),
                         ["autoscout24", "trustpilot"])
        # le fonti saltate NON compaiono nelle progress bar
        keys = [s["key"] for s in j["sources"]]
        self.assertNotIn("trustpilot", keys)
        self.assertNotIn("autoscout24", keys)

    def test_dataforseo_uses_confirmed_place_id(self):
        from ekko.core.models import BusinessRef
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"tasks": [{"id": "t1", "status_code": 20100}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["task"] = json[0]
            return FakeResp()

        os.environ["DATAFORSEO_AUTH"] = "x"
        import httpx
        orig = httpx.post
        httpx.post = fake_post
        try:
            b = BusinessRef(id="b", name="Acme", city="Milano",
                            google_place_id="ChIJ123")
            tid = self.dfs.post_task(b)
        finally:
            httpx.post = orig
            os.environ.pop("DATAFORSEO_AUTH", None)
        self.assertEqual(tid, "t1")
        self.assertEqual(captured["task"].get("place_id"), "ChIJ123")
        self.assertNotIn("keyword", captured["task"])


if __name__ == "__main__":
    unittest.main()
