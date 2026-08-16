"""Test nuove fonti: TripAdvisor (DFS), AutoScout24/Feedaty/RV (scraper), Facebook."""
import os
import tempfile
import unittest
from datetime import datetime, timezone

from ekko.connectors import pubscrape
from ekko.connectors.base import ConnectorRun
from ekko.core.models import BusinessRef


BIZ = BusinessRef(id="t-acme", name="Acme Motors", city="Milano",
                  domain="acmemotors.it")


class PubScrapeTest(unittest.TestCase):
    def test_jsonld_strategy(self):
        html = """<html><script type="application/ld+json">
        {"@type":"LocalBusiness","review":[
          {"@type":"Review","author":{"name":"Mario"},"datePublished":"2026-05-01",
           "reviewBody":"Ottimo concessionario","reviewRating":{"ratingValue":"5","bestRating":"5"}},
          {"@type":"Review","author":"Lucia","datePublished":"2026-04-11",
           "reviewBody":"Consegna lenta","reviewRating":{"ratingValue":"2","bestRating":"5"}}
        ]}</script></html>"""
        got = list(pubscrape.iter_jsonld_reviews(html))
        self.assertEqual(len(got), 2)
        stars = sorted(r["stars"] for r in got)
        self.assertEqual(stars, [2.0, 5.0])

    def test_deep_harvest_nextdata(self):
        html = """<script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"reviews":[
          {"author":"Gino","rating":4,"text":"Personale gentile","date":"2026-03-10"},
          {"author":"Pina","rating":1,"comment":"Auto danneggiata","created":"2026-02-01"}
        ]}}}</script>"""
        got = list(pubscrape.deep_harvest(html))
        self.assertEqual(len(got), 2)

    def test_parse_date_formats(self):
        for s in ("2026-05-01", "28.08.2025", "01/03/2026", "2026-05-01T10:00:00Z"):
            self.assertIsNotNone(pubscrape.parse_date(s), s)


class TripadvisorNormalizeTest(unittest.TestCase):
    def test_normalize_items(self):
        from ekko.connectors import tripadvisor_dfs as ta
        items = [
            {"review_id": "r1", "rating": {"value": 5, "rating_max": 5},
             "timestamp": "2026-06-01 10:00:00 +00:00".replace(" +00:00", "+00:00"),
             "review_text": "Cena fantastica", "user_profile": {"name": "Ugo"}},
            {"rating": 3, "date_of_visit": "2026-05-20",
             "title": "Nella media", "user_profile": {}},
            {"rating": None, "timestamp": "2026-01-01"},  # scartata
        ]
        run = ConnectorRun()
        got = list(ta.normalize_items(items, BIZ, run))
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0].source.value, "tripadvisor")
        self.assertAlmostEqual(got[0].rating, 1.0)
        self.assertAlmostEqual(got[1].rating, 0.6)
        # id namespaced per business (no collisioni cross-tenant)
        self.assertTrue(all(f.business_id == "t-acme" for f in got))


class ScraperConnectorsTest(unittest.TestCase):
    def test_autoscout24_url_resolution(self):
        from ekko.connectors.autoscout24 import resolve_url
        b = BusinessRef(id="x", name="Autosport Snc")
        self.assertEqual(resolve_url(b),
                         "https://www.autoscout24.it/concessionari/autosport-snc/recensioni")
        b2 = BusinessRef(id="x", name="X",
                         autoscout24_url="https://www.autoscout24.it/concessionari/rossi-auto")
        self.assertEqual(resolve_url(b2),
                         "https://www.autoscout24.it/concessionari/rossi-auto/recensioni")

    def test_certified_urls(self):
        from ekko.connectors.certified import (FeedatyConnector,
                                               RecensioniVerificateConnector)
        self.assertEqual(FeedatyConnector()._url(BIZ),
                         "https://www.feedaty.com/feedaty/reviews/acmemotors")
        self.assertEqual(RecensioniVerificateConnector()._url(BIZ),
                         "https://www.recensioni-verificate.com/recensioni-clienti/acmemotors.it.html")

    def test_default_flags(self):
        for k in ("EKKO_ENABLE_AUTOSCOUT24", "EKKO_ENABLE_FEEDATY",
                  "EKKO_ENABLE_RECENSIONI_VERIFICATE", "FACEBOOK_PAGE_TOKEN"):
            os.environ.pop(k, None)
        from ekko.connectors import autoscout24, certified, facebook
        self.assertTrue(autoscout24.enabled())     # ON di default (automotive)
        os.environ["EKKO_ENABLE_AUTOSCOUT24"] = "0"
        self.assertFalse(autoscout24.enabled())    # spegnibile con =0
        os.environ.pop("EKKO_ENABLE_AUTOSCOUT24", None)
        self.assertFalse(certified.feedaty_enabled())
        self.assertFalse(certified.rv_enabled())
        self.assertFalse(facebook.enabled())


class SourceStatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        os.environ["EKKO_DATABASE_URL"] = f"sqlite:///{self.tmp.name}"
        for k in ("DATAFORSEO_AUTH", "GOOGLE_MAPS_API_KEY", "TRUSTPILOT_API_KEY",
                  "EKKO_ENABLE_PUBLIC_TRUSTPILOT", "EKKO_ENABLE_AUTOSCOUT24",
                  "EKKO_ENABLE_FEEDATY", "EKKO_ENABLE_RECENSIONI_VERIFICATE",
                  "FACEBOOK_PAGE_TOKEN", "GOOGLE_OAUTH_CLIENT_ID",
                  "GOOGLE_OAUTH_CLIENT_SECRET"):
            os.environ.pop(k, None)
        import importlib
        from ekko.storage import db
        importlib.reload(db)
        db.init_db()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_states_reflect_flags(self):
        os.environ["EKKO_ENABLE_AUTOSCOUT24"] = "1"
        os.environ["EKKO_ENABLE_PUBLIC_TRUSTPILOT"] = "1"
        import importlib
        from ekko.api import main
        importlib.reload(main)
        from ekko.storage import db
        db.upsert_business(BusinessRef(id="s1", name="S1"))
        states = main._source_states("s1")
        keys = [s["key"] for s in states]
        self.assertIn("autoscout24", keys)
        self.assertIn("trustpilot", keys)
        self.assertNotIn("google", keys)       # nessuna chiave google
        self.assertNotIn("tripadvisor", keys)  # DATAFORSEO_AUTH assente
        self.assertTrue(all(s["state"] == "done" for s in states))
        os.environ.pop("EKKO_ENABLE_AUTOSCOUT24", None)
        os.environ.pop("EKKO_ENABLE_PUBLIC_TRUSTPILOT", None)


if __name__ == "__main__":
    unittest.main()
