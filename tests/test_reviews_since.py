"""Cut-off recensioni Google: si parte dal 2025 (EKKO_GOOGLE_REVIEWS_SINCE)."""
import os
import unittest
from unittest import mock

from ekko.connectors import dataforseo
from ekko.connectors.base import ConnectorRun, google_reviews_since
from ekko.connectors.gbp import GbpConnector
from ekko.core.models import BusinessRef


def _biz():
    return BusinessRef(id="biz1", name="Rossi Auto", city="Roma")


class TestCutoff(unittest.TestCase):
    def test_default_2025(self):
        os.environ.pop("EKKO_GOOGLE_REVIEWS_SINCE", None)
        self.assertEqual(google_reviews_since().year, 2025)
        self.assertIsNotNone(google_reviews_since().tzinfo)

    def test_override_env(self):
        with mock.patch.dict(os.environ,
                             {"EKKO_GOOGLE_REVIEWS_SINCE": "2024-06-01"}):
            self.assertEqual(google_reviews_since().year, 2024)

    def test_env_invalida_ricade_sul_default(self):
        with mock.patch.dict(os.environ,
                             {"EKKO_GOOGLE_REVIEWS_SINCE": "boh"}):
            self.assertEqual(google_reviews_since().year, 2025)


class TestGbpFetchFiltra(unittest.TestCase):
    def test_scarta_recensioni_pre_2025(self):
        pages = {"reviews": [
            {"reviewId": "old", "starRating": "FIVE",
             "createTime": "2024-11-30T10:00:00Z"},
            {"reviewId": "new", "starRating": "FOUR",
             "createTime": "2025-03-01T10:00:00Z"},
        ]}
        conn = GbpConnector(owner_id="own1")
        with mock.patch.object(GbpConnector, "_headers", return_value={}), \
             mock.patch("ekko.connectors.gbp.httpx.get") as g:
            g.return_value = mock.Mock(
                status_code=200, json=lambda: pages,
                raise_for_status=lambda: None)
            out = conn.fetch_reviews("own1", "accounts/1", "locations/2")
        self.assertEqual([r["reviewId"] for r in out], ["new"])


class TestDataForSeoFiltra(unittest.TestCase):
    def test_normalize_items_scarta_pre_2025(self):
        items = [
            {"review_id": "a", "timestamp": "2024-05-01 12:00:00 +00:00",
             "rating": {"value": 5}, "review_text": "vecchia"},
            {"review_id": "b", "timestamp": "2026-05-01 12:00:00 +00:00",
             "rating": {"value": 4}, "review_text": "nuova"},
        ]
        run = ConnectorRun()
        out = list(dataforseo.normalize_items(items, _biz(), run))
        self.assertEqual(len(out), 1)
        self.assertEqual(run.fetched, 1)
        self.assertIn("b", out[0].source_native_id)


if __name__ == "__main__":
    unittest.main()
