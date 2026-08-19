"""Test home pubblica + login in pagina + flag "È la mia azienda".

Senza rete: connettori disattivati, OAuth simulato via env var e sessione
(stessi pattern di tests/test_auth_multitenant.py).
"""
import importlib
import os
import tempfile
import unittest
from datetime import datetime, timezone


def _make_feedback(business_id: str, native: str):
    from ekko.core.models import (FeedbackObject, Lineage, Source,
                                  make_feedback_id, pseudonymize_author)
    return FeedbackObject(
        id=make_feedback_id("google", business_id, native),
        source=Source.GOOGLE,
        source_native_id=native,
        business_id=business_id,
        author_hash=pseudonymize_author("tester"),
        text="ottimo servizio",
        rating=1.0,
        published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        lineage=Lineage(connector="google", run_id="r", license="demo"),
    )


class HomePublicTest(unittest.TestCase):
    """OAuth ATTIVO: home visibile anche da anonimi, azioni protette in 401."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        os.environ["EKKO_DATABASE_URL"] = f"sqlite:///{self.tmp.name}"
        # niente rete: connettori disattivati (nessun demo, nessuna API)
        for k in ("GOOGLE_MAPS_API_KEY", "TRUSTPILOT_API_KEY", "DATAFORSEO_AUTH",
                  "EKKO_DEMO_FALLBACK", "EKKO_ENABLE_PUBLIC_TRUSTPILOT"):
            os.environ.pop(k, None)
        # OAuth simulato attivo (login richiesto per le azioni)
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "cid.apps.googleusercontent.com"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "secret"
        from ekko.storage import db
        importlib.reload(db)
        db.init_db()
        self.db = db
        from ekko.api import main
        importlib.reload(main)
        main.default_connectors = lambda owner_id=None: []  # nessun side effect
        self.main = main

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _client_logged(self, sub="subA", email="a@ag.it", name="Agenzia A"):
        c = self.main.app.test_client()
        with c.session_transaction() as s:
            s["uid"] = sub; s["email"] = email; s["name"] = name
        return c

    # ---- home pubblica -------------------------------------------------
    def test_home_200_senza_login(self):
        anon = self.main.app.test_client()
        r = anon.get("/")
        self.assertEqual(r.status_code, 200)   # niente redirect a /login

    def test_home_loggata_mostra_recent(self):
        c = self._client_logged()
        owner = self.main.owner_key("subA")
        # azienda dell'agenzia con almeno una recensione -> compare in "recenti"
        r = c.post("/search", data={"name": "Eataly"})
        self.assertEqual(r.status_code, 302)
        bid = f"{owner}-eataly"
        self.db.insert_feedback(_make_feedback(bid, "x1"))
        html = c.get("/").get_data(as_text=True)
        self.assertIn("Eataly", html)
        # l'anonimo NON vede le aziende altrui (recent=[])
        anon_html = self.main.app.test_client().get("/").get_data(as_text=True)
        self.assertNotIn("Eataly", anon_html)

    # ---- azioni protette: 401 JSON, non redirect HTML ------------------
    def test_match_401_json_senza_login(self):
        r = self.main.app.test_client().post("/match", data={"name": "Eataly"})
        self.assertEqual(r.status_code, 401)
        j = r.get_json()
        self.assertEqual(j["error"], "login_required")
        self.assertIn("/login", j["login_url"])

    def test_search_401_json_senza_login(self):
        r = self.main.app.test_client().post("/search", data={"name": "Eataly"})
        self.assertEqual(r.status_code, 401)
        j = r.get_json()
        self.assertEqual(j["error"], "login_required")
        self.assertIn("/login", j["login_url"])

    # ---- flag "È la mia azienda" ---------------------------------------
    def test_is_own_propagato_a_payload_e_dashboard(self):
        c = self._client_logged()
        owner = self.main.owner_key("subA")
        r = c.post("/search", data={"name": "Bar Mio", "is_own": "1"})
        self.assertEqual(r.status_code, 302)
        bid = f"{owner}-bar-mio"
        # il payload persistito (model serializzato) conserva il campo
        payload = self.db.get_business_payload(bid)
        self.assertIs(payload["is_own"], True)
        # la dashboard embedda is_own (e business_id) in data_json
        self.db.insert_feedback(_make_feedback(bid, "x1"))
        html = c.get(f"/businesses/{bid}/dashboard").get_data(as_text=True)
        self.assertIn('"is_own": true', html)
        self.assertIn(f'"business_id": "{bid}"', html)

    def test_is_own_default_false(self):
        c = self._client_logged()
        owner = self.main.owner_key("subA")
        c.post("/search", data={"name": "Bar Altrui"})   # campo assente -> False
        payload = self.db.get_business_payload(f"{owner}-bar-altrui")
        self.assertIs(payload["is_own"], False)


if __name__ == "__main__":
    unittest.main()
