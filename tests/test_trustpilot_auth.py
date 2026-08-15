"""Test dell'auth Trustpilot: header apikey pubblico, OAuth client-credentials,
cache token, refresh, anti-429. Nessuna rete reale (httpx mockato)."""
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from ekko.connectors.trustpilot import TrustpilotConnector
from ekko.connectors.trustpilot_auth import TrustpilotAuth, TrustpilotAuthError

CACHE = "test_tp_token.json"


def fake_resp(status=200, payload=None, text=""):
    m = mock.Mock()
    m.status_code = status
    m.json.return_value = payload or {}
    m.text = text
    return m


class TestAuth(unittest.TestCase):
    def setUp(self):
        Path(CACHE).unlink(missing_ok=True)
        self.auth = TrustpilotAuth(api_key="KEY", api_secret="SECRET",
                                   cache_path=CACHE)

    def tearDown(self):
        Path(CACHE).unlink(missing_ok=True)

    def test_available_needs_both(self):
        self.assertTrue(self.auth.available())
        self.assertFalse(TrustpilotAuth(api_key="k", api_secret=None,
                                        cache_path=CACHE).available())

    def test_basic_header_is_base64(self):
        import base64
        h = self.auth._basic_header()
        self.assertTrue(h.startswith("Basic "))
        self.assertEqual(base64.b64decode(h[6:]).decode(), "KEY:SECRET")

    def test_client_credentials_fetch_and_cache(self):
        with mock.patch("httpx.post", return_value=fake_resp(
                200, {"access_token": "AT", "refresh_token": "RT",
                      "expires_in": 360000})) as p:
            tok = self.auth.get_access_token(_now=1000)
        self.assertEqual(tok, "AT")
        p.assert_called_once()
        # la seconda chiamata usa la cache, niente rete
        with mock.patch("httpx.post") as p2:
            tok2 = self.auth.get_access_token(_now=1001)
        self.assertEqual(tok2, "AT")
        p2.assert_not_called()

    def test_expired_cache_triggers_refresh(self):
        # semino cache scaduta con refresh token
        Path(CACHE).write_text(json.dumps({
            "client_id": "KEY", "access_token": "OLD", "refresh_token": "RT",
            "expires_at": 500}))
        with mock.patch("httpx.post", return_value=fake_resp(
                200, {"access_token": "NEW", "refresh_token": "RT2",
                      "expires_in": 360000})) as p:
            tok = self.auth.get_access_token(_now=1000)
        self.assertEqual(tok, "NEW")
        # ha usato l'endpoint di refresh
        self.assertIn("refresh", p.call_args.args[0])

    def test_429_raises(self):
        with mock.patch("httpx.post", return_value=fake_resp(429, text="slow down")):
            with self.assertRaises(TrustpilotAuthError):
                self.auth.get_access_token(_now=1000)

    def test_token_file_is_chmod_600(self):
        with mock.patch("httpx.post", return_value=fake_resp(
                200, {"access_token": "AT", "expires_in": 360000})):
            self.auth.get_access_token(_now=1000)
        mode = oct(os.stat(CACHE).st_mode)[-3:]
        self.assertEqual(mode, "600")


class TestConnectorHeaders(unittest.TestCase):
    def test_public_apikey_is_header_not_query(self):
        conn = TrustpilotConnector(api_key="PUBKEY", api_secret=None)
        self.assertEqual(conn._public_headers(), {"apikey": "PUBKEY"})
        # OAuth privato non disponibile senza secret → nessun bearer
        self.assertIsNone(conn._private_headers())

    def test_resolve_uses_header(self):
        conn = TrustpilotConnector(api_key="PUBKEY", api_secret=None)
        from ekko.core.models import BusinessRef
        with mock.patch("httpx.get", return_value=fake_resp(
                200, {"id": "BU123"})) as p:
            bu = conn.resolve_business_unit(
                BusinessRef(id="x", name="X", domain="x.it"))
        self.assertEqual(bu, "BU123")
        # apikey passata come header, non come query param
        self.assertEqual(p.call_args.kwargs["headers"], {"apikey": "PUBKEY"})
        self.assertNotIn("apikey", p.call_args.kwargs["params"])

    def test_health_reports_oauth(self):
        self.assertFalse(
            TrustpilotConnector(api_key="k", api_secret=None).health()["private_oauth"])
        self.assertTrue(
            TrustpilotConnector(api_key="k", api_secret="s").health()["private_oauth"])


if __name__ == "__main__":
    unittest.main()
