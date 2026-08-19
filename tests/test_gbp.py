"""Test Fase 2 — Google Business Profile: connettore, bozze AI, endpoint.

Tutto SENZA rete: le chiamate GBP sono stubbate a livello di metodo del
connettore e il gateway AI non è configurato (fallback deterministico).
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

RAW_ANSWERED = {
    "name": "accounts/1/locations/2/reviews/rev-answered",
    "reviewId": "rev-answered",
    "reviewer": {"displayName": "Mario Rossi"},
    "starRating": "FOUR",
    "comment": "Ottima esperienza, staff gentile.",
    "createTime": "2026-05-01T10:00:00Z",
    "updateTime": "2026-05-01T10:00:00Z",
    "reviewReply": {"comment": "Grazie mille!",
                    "updateTime": "2026-05-02T09:00:00Z"},
}

RAW_UNANSWERED = {
    "name": "accounts/1/locations/2/reviews/rev-open",
    "reviewId": "rev-open",
    "reviewer": {"displayName": "Anna Bianchi"},
    "starRating": "TWO",
    "comment": "Attesa troppo lunga alla cassa.",
    "createTime": "2026-06-10T18:30:00Z",
    "updateTime": "2026-06-10T18:30:00Z",
}

AI_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
           "EKKO_AI_PROVIDER", "EKKO_AI_MODEL")


def _no_network(*a, **k):  # pragma: no cover - guardia anti-rete
    raise AssertionError("Chiamata di rete inattesa nei test GBP")


class NormalizeReviewTest(unittest.TestCase):
    """Recensione v4 -> FeedbackObject (mapping enum, reply, lineage)."""

    def test_normalize_answered(self):
        from ekko.connectors.base import ConnectorRun
        from ekko.connectors.gbp import GbpConnector
        from ekko.core.models import Source
        run = ConnectorRun()
        fo = GbpConnector().normalize_review(RAW_ANSWERED, "biz1", run)
        self.assertEqual(fo.source, Source.GOOGLE)
        self.assertEqual(fo.source_native_id, "rev-answered")
        self.assertAlmostEqual(fo.rating, 0.8)          # FOUR -> 4/5
        self.assertEqual(fo.text, "Ottima esperienza, staff gentile.")
        self.assertEqual(fo.published_at.year, 2026)
        self.assertIsNotNone(fo.reply)
        self.assertEqual(fo.reply.text, "Grazie mille!")
        self.assertEqual(fo.lineage.connector, "gbp")
        self.assertEqual(fo.lineage.license, "official_api")
        # pseudonimizzazione: mai il nome in chiaro
        self.assertNotIn("Mario", fo.author_hash)
        self.assertEqual(run.fetched, 1)

    def test_star_enum_and_unspecified(self):
        from ekko.connectors.base import ConnectorRun
        from ekko.connectors.gbp import GbpConnector, star_value
        self.assertEqual(star_value("ONE"), 1)
        self.assertEqual(star_value("FIVE"), 5)
        self.assertIsNone(star_value("STAR_RATING_UNSPECIFIED"))
        self.assertIsNone(star_value(None))
        rv = dict(RAW_UNANSWERED, starRating="STAR_RATING_UNSPECIFIED")
        fo = GbpConnector().normalize_review(rv, "biz1", ConnectorRun())
        self.assertIsNone(fo.rating)
        self.assertIsNone(fo.reply)


class TemplateReplyTest(unittest.TestCase):
    """Fallback deterministico di generate_review_reply (nessuna chiave AI)."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in AI_KEYS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_fallback_high_low(self):
        from ekko.ai.gateway import AIGateway
        gw = AIGateway()
        self.assertFalse(gw.available())
        high = gw.generate_review_reply("Bar Prova", "Tutto perfetto", 5,
                                        {"author": "Luca", "signature": "Il team"})
        low = gw.generate_review_reply("Bar Prova", "Pessimo", 1,
                                       {"author": "Luca", "signature": "Il team"})
        self.assertIn("Bar Prova", high)
        self.assertIn("Luca", high)
        self.assertIn("Il team", high)
        self.assertNotEqual(high, low)
        self.assertIn("dispiace", low.lower())
        # nessun placeholder residuo
        for txt in (high, low):
            self.assertNotIn("{nome}", txt)
            self.assertNotIn("{autore}", txt)
            self.assertNotIn("{firma}", txt)

    def test_custom_template_and_defaults(self):
        from ekko.ai.gateway import template_reply
        out = template_reply("Bar Prova", "ok", 3,
                             {"template": "Ciao {autore}, grazie da {nome}! {firma}",
                              "signature": "— Direzione"})
        self.assertEqual(out, "Ciao cliente, grazie da Bar Prova! — Direzione")
        # senza firma: default "Lo staff di <nome>"
        out2 = template_reply("Bar Prova", None, None, {})
        self.assertIn("Lo staff di Bar Prova", out2)


class GbpEndpointsTest(unittest.TestCase):
    """Contratto API /api/gbp/* su db temporaneo, auth disattivata (owner pub)."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        os.environ["EKKO_DATABASE_URL"] = f"sqlite:///{self.tmp.name}"
        for k in ("GOOGLE_MAPS_API_KEY", "TRUSTPILOT_API_KEY",
                  "DATAFORSEO_AUTH", "EKKO_DEMO_FALLBACK",
                  "EKKO_ENABLE_PUBLIC_TRUSTPILOT", "GOOGLE_OAUTH_CLIENT_ID",
                  "GOOGLE_OAUTH_CLIENT_SECRET", "EKKO_DISABLED_SOURCES",
                  *AI_KEYS):
            os.environ.pop(k, None)
        import importlib
        from ekko.storage import db
        importlib.reload(db)
        db.init_db()
        self.db = db
        from ekko.api import main
        importlib.reload(main)
        main.default_connectors = lambda owner_id=None: []
        self.main = main
        self.client = main.app.test_client()
        # guardia anti-rete sul modulo del connettore
        import ekko.connectors.gbp as gbp_mod
        self.gbp = gbp_mod
        self._httpx_patches = [
            patch.object(gbp_mod.httpx, "get", _no_network),
            patch.object(gbp_mod.httpx, "put", _no_network),
            patch.object(gbp_mod.httpx, "post", _no_network),
        ]
        for p in self._httpx_patches:
            p.start()
        # azienda di proprietà dell'owner 'pub' (auth disattivata)
        from ekko.core.models import BusinessRef
        self.bid = "pub-bar-prova"
        db.upsert_business(BusinessRef(id=self.bid, name="Bar Prova"),
                           owner_id="pub")

    def tearDown(self):
        for p in self._httpx_patches:
            p.stop()
        os.unlink(self.tmp.name)

    # ---------- helper ----------
    def _connect(self):
        far = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.db.upsert_oauth_token("pub", self.gbp.PROVIDER, "tok-abc",
                                   "refresh-abc", far, self.gbp.PROVIDER,
                                   datetime.now(timezone.utc))

    def _link(self):
        self.db.upsert_gbp_link(self.bid, "accounts/1", "locations/2",
                                "Bar Prova - Centro",
                                datetime.now(timezone.utc))

    # ---------- 409 se non connesso ----------
    def test_409_when_not_connected(self):
        for method, url in (
                ("post", f"/api/gbp/link/{self.bid}"),
                ("post", f"/api/gbp/sync/{self.bid}"),
                ("get", f"/api/gbp/reviews/{self.bid}"),
                ("post", f"/api/gbp/draft/{self.bid}/rev-open"),
                ("post", f"/api/gbp/reply/{self.bid}/rev-open")):
            r = getattr(self.client, method)(url, json={"text": "x",
                                                        "account": "a",
                                                        "location": "l"})
            self.assertEqual(r.status_code, 409, url)
            self.assertEqual(r.get_json(),
                             {"ok": False, "error": "gbp_not_connected"}, url)
        st = self.client.get(f"/api/gbp/status/{self.bid}").get_json()
        self.assertFalse(st["connected"])
        self.assertEqual(st["linked"], {"location": None, "title": None})
        self.assertEqual(st["locations"], [])

    # ---------- status + link ----------
    def test_status_link_flow(self):
        self._connect()
        with patch.object(self.gbp.GbpConnector, "list_accounts",
                          lambda s, o: [{"name": "accounts/1"}]), \
             patch.object(self.gbp.GbpConnector, "list_locations",
                          lambda s, o, a: [{"name": "locations/2",
                                            "title": "Bar Prova - Centro",
                                            "storefrontAddress": {
                                                "addressLines": ["Via Roma 1"],
                                                "locality": "Milano"}}]):
            st = self.client.get(f"/api/gbp/status/{self.bid}").get_json()
        self.assertTrue(st["connected"])
        self.assertIsNone(st["linked"]["location"])
        self.assertEqual(len(st["locations"]), 1)
        loc = st["locations"][0]
        self.assertEqual(loc["account"], "accounts/1")
        self.assertEqual(loc["name"], "locations/2")
        self.assertEqual(loc["title"], "Bar Prova - Centro")
        self.assertIn("Via Roma 1", loc["address"])
        # link
        r = self.client.post(f"/api/gbp/link/{self.bid}",
                             json={"account": "accounts/1",
                                   "location": "locations/2",
                                   "title": "Bar Prova - Centro"})
        self.assertEqual(r.get_json(), {"ok": True})
        st2 = self.client.get(f"/api/gbp/status/{self.bid}").get_json()
        self.assertEqual(st2["linked"], {"location": "locations/2",
                                         "title": "Bar Prova - Centro"})
        self.assertEqual(st2["locations"], [])   # già collegato -> lista vuota

    # ---------- sync nel feedback store ----------
    def test_sync_stores_and_dedups(self):
        self._connect(); self._link()
        with patch.object(self.gbp.GbpConnector, "fetch_reviews",
                          lambda s, o, a, l: [RAW_ANSWERED, RAW_UNANSWERED]):
            r1 = self.client.post(f"/api/gbp/sync/{self.bid}").get_json()
            r2 = self.client.post(f"/api/gbp/sync/{self.bid}").get_json()
        self.assertEqual(r1, {"ok": True, "fetched": 2, "stored": 2,
                              "duplicates": 0})
        self.assertEqual(r2, {"ok": True, "fetched": 2, "stored": 0,
                              "duplicates": 2})
        fos = self.db.load_feedback(self.bid)
        self.assertEqual(len(fos), 2)
        self.assertEqual({f.source.value for f in fos}, {"google"})
        # stadio 0 applicato (sentiment dal rating)
        self.assertTrue(all(f.enrichment.sentiment is not None for f in fos))

    # ---------- reviews live + flusso draft -> save -> reply ----------
    def test_reviews_draft_save_reply_flow(self):
        self._connect(); self._link()
        sent = {}

        def fake_send(s, owner, account, location, review_id, text):
            sent.update(owner=owner, account=account, location=location,
                        review_id=review_id, text=text)
            return True, "ok"

        with patch.object(self.gbp.GbpConnector, "fetch_reviews",
                          lambda s, o, a, l: [RAW_ANSWERED, RAW_UNANSWERED]), \
             patch.object(self.gbp.GbpConnector, "send_reply", fake_send):
            # solo le non risposte
            r = self.client.get(
                f"/api/gbp/reviews/{self.bid}?only=unanswered").get_json()
            self.assertEqual(len(r["reviews"]), 1)
            row = r["reviews"][0]
            self.assertEqual(row["review_id"], "rev-open")
            self.assertEqual(row["rating"], 2)
            self.assertEqual(row["author"], "Anna Bianchi")
            self.assertFalse(row["has_reply"])
            self.assertIsNone(row["draft"])
            # tutte: has_reply/reply_text aggiornati dal live
            allr = self.client.get(
                f"/api/gbp/reviews/{self.bid}").get_json()["reviews"]
            answered = next(x for x in allr
                            if x["review_id"] == "rev-answered")
            self.assertTrue(answered["has_reply"])
            self.assertEqual(answered["reply_text"], "Grazie mille!")
            # 1) genera bozza (AI non configurata -> template deterministico)
            d = self.client.post(
                f"/api/gbp/draft/{self.bid}/rev-open").get_json()
            self.assertEqual(d["draft"]["status"], "draft")
            self.assertIn("Anna Bianchi", d["draft"]["text"])   # {autore}
            self.assertIn("dispiace", d["draft"]["text"].lower())  # rating 2
            # 2) l'utente la modifica e la salva
            r = self.client.post(
                f"/api/gbp/draft/{self.bid}/rev-open/save",
                json={"text": "Risposta rivista dal titolare."})
            self.assertEqual(r.get_json(), {"ok": True})
            r = self.client.get(
                f"/api/gbp/reviews/{self.bid}?only=unanswered").get_json()
            self.assertEqual(r["reviews"][0]["draft"],
                             {"text": "Risposta rivista dal titolare.",
                              "status": "draft"})
            # 3) invio (solo su azione esplicita dell'utente)
            r = self.client.post(
                f"/api/gbp/reply/{self.bid}/rev-open",
                json={"text": "Risposta rivista dal titolare."})
            self.assertEqual(r.get_json(), {"ok": True})
        self.assertEqual(sent["review_id"], "rev-open")
        self.assertEqual(sent["account"], "accounts/1")
        self.assertEqual(sent["location"], "locations/2")
        self.assertEqual(sent["text"], "Risposta rivista dal titolare.")
        draft = self.db.get_reply_draft(self.bid, "rev-open")
        self.assertEqual(draft["status"], "sent")
        self.assertIsNotNone(draft["sent_at"])
        # snapshot conservato dopo il save (COALESCE)
        self.assertIn("Anna Bianchi", draft["review_snapshot_json"])

    def test_reply_error_returns_502(self):
        self._connect(); self._link()
        with patch.object(self.gbp.GbpConnector, "send_reply",
                          lambda s, o, a, l, rid, t: (False, "GBP HTTP 429: quota")):
            r = self.client.post(f"/api/gbp/reply/{self.bid}/rev-open",
                                 json={"text": "ciao"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.get_json(),
                         {"ok": False, "error": "GBP HTTP 429: quota"})
        # niente 'sent' su errore
        draft = self.db.get_reply_draft(self.bid, "rev-open")
        self.assertTrue(draft is None or draft["status"] != "sent")

    # ---------- settings round-trip ----------
    def test_settings_roundtrip(self):
        st = self.client.get(f"/api/gbp/settings/{self.bid}").get_json()
        self.assertEqual(st["language"], "it")
        self.assertFalse(st["auto_draft"])
        body = {"tone": "informale", "language": "en",
                "signature": "— Bar Prova", "template": "Grazie {autore}! {firma}",
                "auto_draft": True}
        r = self.client.post(f"/api/gbp/settings/{self.bid}", json=body)
        self.assertEqual(r.get_json(), {"ok": True})
        st2 = self.client.get(f"/api/gbp/settings/{self.bid}").get_json()
        self.assertEqual(st2, {"tone": "informale", "language": "en",
                               "signature": "— Bar Prova",
                               "template": "Grazie {autore}! {firma}",
                               "auto_draft": True})

    # ---------- token: refresh automatico quando scaduto ----------
    def test_token_refresh_when_expired(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.db.upsert_oauth_token("pub", self.gbp.PROVIDER, "tok-old",
                                   "refresh-abc", past, "s",
                                   datetime.now(timezone.utc))
        with patch.object(self.gbp.gbp_oauth, "refresh_access_token",
                          lambda rt: {"access_token": f"new-for-{rt}",
                                      "expires_at": (datetime.now(timezone.utc)
                                                     + timedelta(hours=1)).isoformat()}):
            tok = self.gbp.GbpConnector()._access_token("pub")
        self.assertEqual(tok, "new-for-refresh-abc")
        saved = self.db.get_oauth_token("pub", self.gbp.PROVIDER)
        self.assertEqual(saved["access_token"], "new-for-refresh-abc")
        self.assertEqual(saved["refresh_token"], "refresh-abc")  # COALESCE


class GbpOauthHelpersTest(unittest.TestCase):
    """authorization_url: scope business.manage, offline+consent, callback."""

    def test_authorization_url(self):
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "cid.apps.googleusercontent.com"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "secret"
        try:
            from ekko.auth import gbp_oauth
            self.assertTrue(gbp_oauth.enabled())
            url = gbp_oauth.authorization_url("https://x.dev", "st9")
            self.assertIn("business.manage", url)
            self.assertIn("access_type=offline", url)
            self.assertIn("prompt=consent", url)
            self.assertIn("state=st9", url)
            self.assertIn("redirect_uri=https%3A%2F%2Fx.dev%2Fgbp%2Fcallback",
                          url)
        finally:
            os.environ.pop("GOOGLE_OAUTH_CLIENT_ID", None)
            os.environ.pop("GOOGLE_OAUTH_CLIENT_SECRET", None)


if __name__ == "__main__":
    unittest.main()
