"""Facebook senza login del cliente: raccolta via provider dati."""
import os
import unittest

from ekko.connectors import facebook_public as fbp
from ekko.connectors.base import ConnectorRun
from ekko.core.models import BusinessRef

BIZ = BusinessRef(id="b", name="Pasquarelli Auto",
                  facebook_url="https://www.facebook.com/pasquarelliauto")


class FacebookPublicTest(unittest.TestCase):
    def setUp(self):
        os.environ["BRIGHTDATA_TOKEN"] = "tok"
        os.environ["BRIGHTDATA_FB_REVIEWS_DATASET"] = "gd_test"

    def tearDown(self):
        for k in ("BRIGHTDATA_TOKEN", "BRIGHTDATA_FB_REVIEWS_DATASET"):
            os.environ.pop(k, None)

    def test_disabled_without_config(self):
        os.environ.pop("BRIGHTDATA_TOKEN", None)
        self.assertFalse(fbp.enabled())

    def test_post_task_sends_page_url(self):
        import httpx
        cap = {}

        class FR:
            def raise_for_status(self): pass
            def json(self): return {"snapshot_id": "s1"}

        orig = httpx.post
        httpx.post = lambda url, headers=None, params=None, json=None, timeout=None: (
            cap.update(url=url, params=params, body=json) or FR())
        try:
            sid = fbp.post_task(BIZ)
        finally:
            httpx.post = orig
        self.assertEqual(sid, "s1")
        self.assertIn("datasets/v3/trigger", cap["url"])
        self.assertEqual(cap["params"]["dataset_id"], "gd_test")
        self.assertEqual(cap["body"][0]["url"], BIZ.facebook_url)

    def test_collect_waits_then_returns(self):
        import httpx
        seq = [{"status": "running"}, {"status": "ready"}]
        data = [{"review_id": "r1", "rating": 5, "date": "2026-05-01",
                 "review_text": "Ottimo", "author": "Mario"}]

        class FR:
            def __init__(self, payload): self.payload = payload
            def raise_for_status(self): pass
            def json(self): return self.payload

        orig = httpx.get
        state = {"i": 0}

        def fake_get(url, headers=None, params=None, timeout=None):
            if "/progress/" in url:
                p = seq[min(state["i"], len(seq) - 1)]
                state["i"] += 1
                return FR(p)
            return FR(data)

        httpx.get = fake_get
        try:
            items, total = fbp.collect("s1")
            self.assertIsNone(items)              # ancora in corso
            items, total = fbp.collect("s1")      # ora pronto
        finally:
            httpx.get = orig
        self.assertEqual(len(items), 1)
        run = ConnectorRun()
        norm = list(fbp.normalize_items(items, BIZ, run))
        self.assertEqual(len(norm), 1)
        self.assertEqual(norm[0].source.value, "meta")
        self.assertAlmostEqual(norm[0].rating, 1.0)
        self.assertEqual(norm[0].text, "Ottimo")

    def test_normalize_handles_recommendations(self):
        """Pagine nuove: niente stelle, solo consiglia/sconsiglia."""
        items = [{"id": "x", "is_recommended": True, "date": "2026-04-02",
                  "text": "Consigliato"},
                 {"id": "y", "is_recommended": False, "created": "2026-04-03",
                  "comment": "Da evitare"}]
        run = ConnectorRun()
        norm = list(fbp.normalize_items(items, BIZ, run))
        self.assertEqual(len(norm), 2)
        self.assertAlmostEqual(norm[0].rating, 1.0)
        self.assertAlmostEqual(norm[1].rating, 0.2)

    def test_failed_snapshot_unblocks(self):
        import httpx

        class FR:
            def raise_for_status(self): pass
            def json(self): return {"status": "failed"}

        orig = httpx.get
        httpx.get = lambda *a, **k: FR()
        try:
            items, total = fbp.collect("s2")
        finally:
            httpx.get = orig
        self.assertEqual(items, [])   # sblocca invece di restare pending


if __name__ == "__main__":
    unittest.main()
