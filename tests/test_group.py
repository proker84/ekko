"""Test viste di gruppo: più sedi analizzate insieme (catene)."""
import json
import os
import tempfile
import unittest


class GroupSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        os.environ["EKKO_DATABASE_URL"] = f"sqlite:///{self.tmp.name}"
        for k in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
                  "TRUSTPILOT_API_KEY", "FACEBOOK_PAGE_TOKEN",
                  "EKKO_ENABLE_FEEDATY", "EKKO_ENABLE_RECENSIONI_VERIFICATE",
                  "EKKO_ENABLE_PUBLIC_TRUSTPILOT"):
            os.environ.pop(k, None)
        os.environ["DATAFORSEO_AUTH"] = "x"
        os.environ["EKKO_ENABLE_TRIPADVISOR"] = "0"
        import importlib
        from ekko.storage import db
        importlib.reload(db)
        db.init_db()
        from ekko.api import main
        importlib.reload(main)
        self.main = main
        main.default_connectors = lambda owner_id=None: []
        self.posted = []
        from ekko.connectors import dataforseo as dfs
        self.dfs = dfs
        # salva gli originali: vanno ripristinati o "sporcano" gli altri test
        self._orig_post, self._orig_collect = dfs.post_task, dfs.collect
        dfs.post_task = lambda biz, keyword_override=None, place_id=None: (
            self.posted.append(keyword_override) or f"task{len(self.posted)}")

    def tearDown(self):
        self.dfs.post_task, self.dfs.collect = self._orig_post, self._orig_collect
        os.unlink(self.tmp.name)
        os.environ.pop("DATAFORSEO_AUTH", None)
        os.environ.pop("EKKO_ENABLE_TRIPADVISOR", None)

    def test_one_task_per_location(self):
        c = self.main.app.test_client()
        labels = ["Pasquarelli Auto - Volkswagen",
                  "Pasquarelli Auto - Toyota",
                  "Pasquarelli Auto - Kia e BYD"]
        r = c.post("/search", data={
            "name": "Pasquarelli Auto", "city": "San Giovanni Teatino",
            "google_labels": json.dumps(labels)},
            headers={"X-Requested-With": "fetch"})
        j = r.get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(self.posted, labels)      # un task per sede
        from ekko.storage import db
        payload = db.get_business_payload(j["id"])
        self.assertEqual(len(payload["dfs_tasks"]), 3)
        self.assertTrue(all(t["pending"] for t in payload["dfs_tasks"]))
        self.assertEqual([t["label"] for t in payload["dfs_tasks"]], labels)

    def test_single_location_has_no_label(self):
        c = self.main.app.test_client()
        r = c.post("/search", data={"name": "Bar Sport", "city": "Milano",
                                    "google_labels": json.dumps(["Bar Sport"])},
                   headers={"X-Requested-With": "fetch"})
        from ekko.storage import db
        payload = db.get_business_payload(r.get_json()["id"])
        self.assertEqual(len(payload["dfs_tasks"]), 1)
        self.assertIsNone(payload["dfs_tasks"][0]["label"])   # no multi-sede

    def test_collect_tags_reviews_with_location(self):
        """Le recensioni di ogni sede sono etichettate e non collidono."""
        from datetime import datetime, timezone
        from ekko.core.models import BusinessRef
        from ekko.storage import db
        biz = BusinessRef(id="grp", name="Gruppo X", city="Roma")
        biz.dfs_tasks = [
            {"id": "t1", "label": "Sede A", "pending": True, "total": None,
             "retried": False},
            {"id": "t2", "label": "Sede B", "pending": True, "total": None,
             "retried": False}]
        db.upsert_business(biz)
        item = {"review_id": "same-id", "rating": {"value": 5},
                "timestamp": "2026-05-01 10:00:00 +00:00",
                "review_text": "ottimo", "profile_name": "Mario"}
        calls = {"n": 0}

        def fake_collect(task_id, expect_name=None):
            calls["n"] += 1
            return [dict(item)], 100

        self.dfs.collect = fake_collect
        payload = self.main._collect_dfs_if_ready("grp")
        self.assertEqual(calls["n"], 2)
        self.assertFalse(payload["dfs_pending"])
        self.assertEqual(payload["total_reviews_google"], 200)  # 100+100
        rows = db.load_feedback("grp")
        # stesso review_id nelle due sedi -> 2 righe distinte, etichettate
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r.location for r in rows), ["Sede A", "Sede B"])


if __name__ == "__main__":
    unittest.main()
