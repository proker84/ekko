"""Test multi-tenant: Accedi con Google + isolamento dati per agenzia."""
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


class MultiTenantTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        os.environ["EKKO_DATABASE_URL"] = f"sqlite:///{self.tmp.name}"
        # niente rete: connettori disattivati (nessun demo, nessuna API)
        for k in ("GOOGLE_MAPS_API_KEY", "TRUSTPILOT_API_KEY", "DATAFORSEO_AUTH",
                  "EKKO_DEMO_FALLBACK", "EKKO_ENABLE_PUBLIC_TRUSTPILOT"):
            os.environ.pop(k, None)
        import importlib
        from ekko.storage import db
        importlib.reload(db)
        db.init_db()
        self.db = db

    def tearDown(self):
        os.unlink(self.tmp.name)

    # ---- login disattivato: comportamento single-tenant invariato ----
    def test_auth_disabled_single_tenant(self):
        os.environ.pop("GOOGLE_OAUTH_CLIENT_ID", None)
        os.environ.pop("GOOGLE_OAUTH_CLIENT_SECRET", None)
        import importlib
        from ekko.api import main
        importlib.reload(main)
        main.default_connectors = lambda: []  # nessun side effect
        c = main.app.test_client()
        self.assertEqual(c.get("/").status_code, 200)          # home diretta
        r = c.post("/search", data={"name": "Eataly"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/businesses/pub-eataly/dashboard", r.headers["Location"])
        self.assertEqual(self.db.get_business_owner("pub-eataly"), "pub")

    # ---- login attivo: gating + isolamento fra agenzie ----
    def test_auth_enabled_isolation(self):
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "cid.apps.googleusercontent.com"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "secret"
        import importlib
        from ekko.api import main
        importlib.reload(main)
        main.default_connectors = lambda: []

        # home senza login -> redirect a /login
        anon = main.app.test_client()
        r = anon.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        # pagina di login servita
        lp = anon.get("/login")
        self.assertEqual(lp.status_code, 200)
        self.assertIn("Accedi con Google", lp.get_data(as_text=True))

        keyA = main.owner_key("subA")
        keyB = main.owner_key("subB")
        self.assertNotEqual(keyA, keyB)

        # agenzia A cerca "Eataly"
        ca = main.app.test_client()
        with ca.session_transaction() as s:
            s["uid"] = "subA"; s["email"] = "a@ag.it"; s["name"] = "Agenzia A"
        ra = ca.post("/search", data={"name": "Eataly"})
        self.assertEqual(ra.status_code, 302)
        bidA = f"{keyA}-eataly"
        self.assertIn(f"/businesses/{bidA}/dashboard", ra.headers["Location"])
        self.assertEqual(self.db.get_business_owner(bidA), keyA)

        # agenzia B cerca la stessa azienda -> id distinto, isolato
        cb = main.app.test_client()
        with cb.session_transaction() as s:
            s["uid"] = "subB"; s["email"] = "b@ag.it"; s["name"] = "Agenzia B"
        rb = cb.post("/search", data={"name": "Eataly"})
        bidB = f"{keyB}-eataly"
        self.assertIn(f"/businesses/{bidB}/dashboard", rb.headers["Location"])
        self.assertNotEqual(bidA, bidB)

        # dati per rendere le aziende elencabili
        self.db.insert_feedback(_make_feedback(bidA, "x1"))
        self.db.insert_feedback(_make_feedback(bidB, "x1"))

        # A vede solo la propria; B solo la propria
        listA = self.db.list_businesses(owner_id=keyA)
        listB = self.db.list_businesses(owner_id=keyB)
        self.assertEqual([b["id"] for b in listA], [bidA])
        self.assertEqual([b["id"] for b in listB], [bidB])

        # B NON può aprire la dashboard di A -> 403
        forb = cb.get(f"/businesses/{bidA}/dashboard")
        self.assertEqual(forb.status_code, 403)

        # A può aprire la propria
        ok = ca.get(f"/businesses/{bidA}/dashboard")
        self.assertEqual(ok.status_code, 200)

        # logout azzera la sessione -> redirect al login
        lo = ca.get("/logout")
        self.assertIn("/login", lo.headers["Location"])
        after = ca.get("/")
        self.assertIn("/login", after.headers["Location"])

    # ---- unità: decodifica id_token e authorization_url ----
    def test_oauth_helpers(self):
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "cid.apps.googleusercontent.com"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "secret"
        import base64, json
        from ekko.auth import google_oauth
        self.assertTrue(google_oauth.enabled())
        url = google_oauth.authorization_url("https://x.dev", "st8")
        self.assertIn("client_id=cid.apps.googleusercontent.com", url)
        self.assertIn("redirect_uri=https%3A%2F%2Fx.dev%2Fauth%2Fcallback", url)
        self.assertIn("state=st8", url)
        # id_token fittizio: header.payload.signature
        payload = {"sub": "123", "email": "u@x.it", "name": "U", "email_verified": True}
        seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        claims = google_oauth._decode_id_token(f"h.{seg}.s")
        self.assertEqual(claims["sub"], "123")
        self.assertEqual(claims["email"], "u@x.it")


if __name__ == "__main__":
    unittest.main()
