import json
import os
import re
import unittest
from pathlib import Path

os.environ["EKKO_DATABASE_URL"] = "sqlite:///test_ekko_dash.db"

from ekko.api.main import render_dashboard
from ekko.connectors.google import GoogleConnector
from ekko.connectors.trustpilot import TrustpilotConnector
from ekko.core.models import BusinessRef
from ekko.ingestion.pipeline import ingest, load_feedback, score_business


class TestDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["EKKO_DATABASE_URL"] = "sqlite:///test_ekko_dash.db"
        Path("test_ekko_dash.db").unlink(missing_ok=True)
        cls.business = BusinessRef(id="dash-biz", name="Dash Biz", city="Roma")
        ingest(cls.business, [GoogleConnector(api_key=None),
                              TrustpilotConnector(api_key=None)])
        cls.feedback = load_feedback(cls.business.id)
        cls.breakdown = score_business(cls.business.id)
        cls.html = render_dashboard(cls.business.name, cls.breakdown, cls.feedback)

    @classmethod
    def tearDownClass(cls):
        Path("test_ekko_dash.db").unlink(missing_ok=True)

    def test_selfcontained_no_external_resources(self):
        self.assertNotIn("http://", self.html.replace("http://www.w3.org", ""))
        self.assertNotIn("https://", self.html)
        self.assertNotIn("<script src", self.html)

    def test_embedded_data_is_valid_json(self):
        m = re.search(
            r'<script id="ekko-data" type="application/json">(.*?)</script>',
            self.html, re.S)
        self.assertIsNotNone(m)
        data = json.loads(m.group(1).replace("<\\/", "</"))
        self.assertEqual(len(data["feedback"]), len(self.feedback))
        row = data["feedback"][0]
        for key in ("d", "s", "st", "sent", "topics", "rep"):
            self.assertIn(key, row)

    def test_score_and_business_rendered(self):
        self.assertIn("Dash Biz", self.html)
        self.assertIn(str(self.breakdown.score), self.html)

    def test_has_accessibility_tables_and_filters(self):
        for anchor in ("tvDonut", "tvTrend", "tvTopics",
                       "fPeriod", "fSource", "fSent", "fTopic"):
            self.assertIn(anchor, self.html)

    def test_has_analysis_section(self):
        for anchor in ("btnAnalyze", "findList", "suggList",
                       "computeFindings", "extractSuggestions", "staleNote"):
            self.assertIn(anchor, self.html)


if __name__ == "__main__":
    unittest.main()
