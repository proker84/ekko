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

    def test_dataforseo_uses_exact_name_not_place_id(self):
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
                            google_place_id="ChIJ123",
                            google_match_name="Acme - Concessionaria Milano")
            tid = self.dfs.post_task(b)
        finally:
            httpx.post = orig
            os.environ.pop("DATAFORSEO_AUTH", None)
        self.assertEqual(tid, "t1")
        # il place_id di Places NON è compatibile con DataForSEO: si usa il
        # nome esatto della scheda confermata come keyword
        self.assertNotIn("place_id", captured["task"])
        self.assertIn("Acme", captured["task"]["keyword"])


if __name__ == "__main__":
    unittest.main()


class SerpMatchersTest(unittest.TestCase):
    """I match vengono TROVATI dal sistema (SERP), non chiesti all'utente."""

    def setUp(self):
        import importlib
        from ekko.api import main
        importlib.reload(main)
        self.main = main
        self.main._serp_urls = lambda q, limit=8: self._serp(q)
        self._serp_map = {}

    def _serp(self, q):
        for key, hits in self._serp_map.items():
            if key in q:
                return hits
        return []

    def test_tripadvisor_found_by_serp(self):
        self._serp_map["tripadvisor"] = [
            {"url": "https://www.tripadvisor.it/Restaurant_Review-g123-d456-Reviews-Da_Mario-Milano.html",
             "title": "Da Mario - Recensioni | TripAdvisor"},
            {"url": "https://www.tripadvisor.it/Restaurants-g123-Milano.html",
             "title": "I migliori ristoranti a Milano"}]
        c = self.main._match_tripadvisor("Da Mario", "Milano")
        self.assertEqual(len(c), 1)  # la pagina-lista viene esclusa
        self.assertTrue(c[0]["token"].startswith("Restaurant_Review-"))
        self.assertGreaterEqual(c[0]["conf"], 90)

    def test_autoscout24_found_by_serp(self):
        self.main._page_title = lambda url: None   # slug diretto fallisce
        self._serp_map["autoscout24"] = [
            {"url": "https://www.autoscout24.it/concessionari/pasquale-auto-srl",
             "title": "Pasquale Auto Srl | Impressioni e valutazioni"}]
        c = self.main._match_autoscout24("Pasquale Auto", None, "Napoli")
        self.assertEqual(len(c), 1)
        self.assertIn("/concessionari/pasquale-auto-srl", c[0]["token"])

    def test_trustpilot_without_domain_uses_serp(self):
        self._serp_map["trustpilot"] = [
            {"url": "https://it.trustpilot.com/review/pasqualeauto.it",
             "title": "Pasquale Auto | Leggi le recensioni"}]
        c = self.main._match_trustpilot("Pasquale Auto", None, None)
        self.assertEqual(c[0]["token"], "pasqualeauto.it")
        self.assertGreaterEqual(c[0]["conf"], 90)

    def test_ta_post_task_uses_url_path(self):
        import os as _os
        from ekko.core.models import BusinessRef
        from ekko.connectors import tripadvisor_dfs as ta
        captured = {}

        class FR:
            def raise_for_status(self): pass
            def json(self): return {"tasks": [{"id": "t9", "status_code": 20100}]}

        import httpx
        orig = httpx.post
        httpx.post = lambda url, headers=None, json=None, timeout=None: (
            captured.update(task=json[0]) or FR())
        _os.environ["DATAFORSEO_AUTH"] = "x"
        try:
            b = BusinessRef(id="b", name="Da Mario",
                            tripadvisor_url_path="Restaurant_Review-g1-d2-Reviews-x.html")
            tid = ta.post_task(b)
        finally:
            httpx.post = orig
            _os.environ.pop("DATAFORSEO_AUTH", None)
        self.assertEqual(tid, "t9")
        self.assertEqual(captured["task"]["url_path"],
                         "Restaurant_Review-g1-d2-Reviews-x.html")
        self.assertNotIn("keyword", captured["task"])


class GoogleKeywordTest(unittest.TestCase):
    """La keyword DataForSEO usa il nome ESATTO della scheda confermata."""

    def _post_capture(self, biz):
        import os as _os
        import httpx
        from ekko.connectors import dataforseo as dfs
        cap = {}

        class FR:
            def raise_for_status(self): pass
            def json(self): return {"tasks": [{"id": "t", "status_code": 20100}]}

        orig = httpx.post
        httpx.post = lambda url, headers=None, json=None, timeout=None: (
            cap.update(task=json[0]) or FR())
        _os.environ["DATAFORSEO_AUTH"] = "x"
        try:
            dfs.post_task(biz)
        finally:
            httpx.post = orig
            _os.environ.pop("DATAFORSEO_AUTH", None)
        return cap["task"]

    def test_uses_match_name_with_city(self):
        from ekko.core.models import BusinessRef
        t = self._post_capture(BusinessRef(
            id="b", name="Pasquarelli Auto", city="San Giovanni Teatino",
            google_match_name="Pasquarelli Auto - Concessionaria Volkswagen"))
        self.assertIn("Volkswagen", t["keyword"])
        self.assertIn("San Giovanni Teatino", t["keyword"])
        self.assertEqual(t["location_name"], "Italy")

    def test_city_not_duplicated(self):
        from ekko.core.models import BusinessRef
        t = self._post_capture(BusinessRef(
            id="b", name="X", city="Milano",
            google_match_name="Bar Sport Milano"))
        self.assertEqual(t["keyword"].lower().count("milano"), 1)

    def test_falls_back_to_plain_name(self):
        from ekko.core.models import BusinessRef
        t = self._post_capture(BusinessRef(id="b", name="Eataly", city="Roma"))
        self.assertEqual(t["keyword"], "Eataly Roma")
