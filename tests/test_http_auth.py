#!/usr/bin/env python3
"""
Unit Tests for HTTP Authentication and Security Hardening in Taktstock
----------------------------------------------------------------------
Verifica:
1. Validazione di TAKTSTOCK_AUTH_TOKEN / UFFICO_AUTH_TOKEN (obbligatorio, minimo 16 caratteri)
2. Autenticazione tramite Header X-Taktstock-Token (e fallback X-Ufficio-Token)
3. Autenticazione tramite Header Authorization: Bearer
4. Rifiuto del token passato in query string (?token=...)
5. Rifiuto del bypass basato sul solo header Cloudflare (Cf-Access-Authenticated-User-Email)
6. Cookie di sessione HMAC-SHA256 con scadenza (accettato se valido, rifiutato se manomesso o scaduto)
7. Limite dimensione body POST (413 Payload Too Large per Content-Length > 10MB)
"""

import os
import sys
import time
import hmac
import hashlib
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from health_server import (
    MIN_AUTH_TOKEN_LENGTH,
    AUTH_SESSION_COOKIE_NAME,
    AUTH_SESSION_DURATION_SECONDS,
    MAX_POST_BODY_BYTES,
    get_auth_secret,
    create_session_cookie_value,
    verify_session_cookie_value,
    TaktstockHealthHandler,
    UfficioHealthHandler,
)

VALID_SECRET = "super-secret-token-1234567890"  # >= 16 chars
WEAK_SECRET = "short-token"                   # < 16 chars


class TestAuthSecretValidation(unittest.TestCase):
    def test_missing_token_enforce_validity_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                get_auth_secret(enforce_validity=True)
            self.assertIn("mancante", str(ctx.exception))

    def test_weak_token_enforce_validity_raises(self):
        with patch.dict(os.environ, {"UFFICO_AUTH_TOKEN": WEAK_SECRET}, clear=True):
            with self.assertRaises(ValueError) as ctx:
                get_auth_secret(enforce_validity=True)
            self.assertIn("troppo debole", str(ctx.exception))

    def test_valid_token_enforce_validity_succeeds(self):
        with patch.dict(os.environ, {"UFFICO_AUTH_TOKEN": VALID_SECRET}, clear=True):
            secret = get_auth_secret(enforce_validity=True)
            self.assertEqual(secret, VALID_SECRET)

    def test_taktstock_token_enforce_validity_succeeds(self):
        with patch.dict(os.environ, {"TAKTSTOCK_AUTH_TOKEN": VALID_SECRET}, clear=True):
            secret = get_auth_secret(enforce_validity=True)
            self.assertEqual(secret, VALID_SECRET)

    def test_fallback_dashboard_password_valid(self):
        with patch.dict(os.environ, {"DASHBOARD_PASSWORD": VALID_SECRET}, clear=True):
            secret = get_auth_secret(enforce_validity=True)
            self.assertEqual(secret, VALID_SECRET)


class TestSessionCookieHMAC(unittest.TestCase):
    def test_create_and_verify_valid_cookie(self):
        cookie_val = create_session_cookie_value(VALID_SECRET, duration_seconds=3600)
        self.assertTrue(verify_session_cookie_value(cookie_val, VALID_SECRET))

    def test_expired_cookie_rejected(self):
        # Cookie scaduto 10 secondi fa
        cookie_val = create_session_cookie_value(VALID_SECRET, duration_seconds=-10)
        self.assertFalse(verify_session_cookie_value(cookie_val, VALID_SECRET))

    def test_tampered_cookie_signature_rejected(self):
        cookie_val = create_session_cookie_value(VALID_SECRET, duration_seconds=3600)
        parts = cookie_val.split(".", 1)
        tampered_cookie = f"{parts[0]}.wrongsignature00000000000000000000"
        self.assertFalse(verify_session_cookie_value(tampered_cookie, VALID_SECRET))

    def test_tampered_cookie_timestamp_rejected(self):
        cookie_val = create_session_cookie_value(VALID_SECRET, duration_seconds=3600)
        parts = cookie_val.split(".", 1)
        # Cambia il timestamp lasciando la vecchia firma
        future_ts = int(parts[0]) + 10000
        tampered_cookie = f"{future_ts}.{parts[1]}"
        self.assertFalse(verify_session_cookie_value(tampered_cookie, VALID_SECRET))

    def test_cookie_verified_with_different_secret_rejected(self):
        cookie_val = create_session_cookie_value(VALID_SECRET, duration_seconds=3600)
        another_secret = "another-secret-token-abcdefghij"
        self.assertFalse(verify_session_cookie_value(cookie_val, another_secret))

    def test_malformed_cookie_rejected(self):
        self.assertFalse(verify_session_cookie_value("", VALID_SECRET))
        self.assertFalse(verify_session_cookie_value("nosignature", VALID_SECRET))
        self.assertFalse(verify_session_cookie_value("notanint.sig", VALID_SECRET))


class TestHandlerAuthentication(unittest.TestCase):
    def _create_mock_handler(self, headers=None, path="/"):
        handler = MagicMock(spec=UfficioHealthHandler)
        handler.headers = headers or {}
        handler.path = path
        # Inietta il vero metodo _is_authenticated
        handler._is_authenticated = UfficioHealthHandler._is_authenticated.__get__(handler, UfficioHealthHandler)
        return handler

    def test_auth_with_valid_x_taktstock_token(self):
        with patch.dict(os.environ, {"TAKTSTOCK_AUTH_TOKEN": VALID_SECRET}):
            handler = self._create_mock_handler(headers={"X-Taktstock-Token": VALID_SECRET})
            self.assertTrue(handler._is_authenticated())

    def test_auth_with_invalid_x_taktstock_token(self):
        with patch.dict(os.environ, {"TAKTSTOCK_AUTH_TOKEN": VALID_SECRET}):
            handler = self._create_mock_handler(headers={"X-Taktstock-Token": "wrong-token-123456"})
            self.assertFalse(handler._is_authenticated())

    def test_auth_with_valid_x_ufficio_token(self):
        with patch.dict(os.environ, {"UFFICO_AUTH_TOKEN": VALID_SECRET}):
            handler = self._create_mock_handler(headers={"X-Ufficio-Token": VALID_SECRET})
            self.assertTrue(handler._is_authenticated())

    def test_auth_with_invalid_x_ufficio_token(self):
        with patch.dict(os.environ, {"UFFICO_AUTH_TOKEN": VALID_SECRET}):
            handler = self._create_mock_handler(headers={"X-Ufficio-Token": "wrong-token-123456"})
            self.assertFalse(handler._is_authenticated())

    def test_auth_with_valid_bearer_token(self):
        with patch.dict(os.environ, {"UFFICO_AUTH_TOKEN": VALID_SECRET}):
            handler = self._create_mock_handler(headers={"Authorization": f"Bearer {VALID_SECRET}"})
            self.assertTrue(handler._is_authenticated())

    def test_auth_with_invalid_bearer_token(self):
        with patch.dict(os.environ, {"UFFICO_AUTH_TOKEN": VALID_SECRET}):
            handler = self._create_mock_handler(headers={"Authorization": "Bearer wrong-token-123456"})
            self.assertFalse(handler._is_authenticated())

    def test_query_string_token_strictly_rejected(self):
        """Verifica che il token in query string (?token=...) sia ignorato e non conceda l'accesso."""
        with patch.dict(os.environ, {"UFFICO_AUTH_TOKEN": VALID_SECRET}):
            handler = self._create_mock_handler(path=f"/dashboard?token={VALID_SECRET}")
            self.assertFalse(handler._is_authenticated())

    def test_cloudflare_header_alone_strictly_rejected(self):
        """Verifica che il solo header Cloudflare non conceda alcun bypass di autenticazione."""
        with patch.dict(os.environ, {"UFFICO_AUTH_TOKEN": VALID_SECRET}):
            handler = self._create_mock_handler(headers={"Cf-Access-Authenticated-User-Email": "admin@example.com"})
            self.assertFalse(handler._is_authenticated())

    def test_auth_with_valid_hmac_cookie(self):
        with patch.dict(os.environ, {"UFFICO_AUTH_TOKEN": VALID_SECRET}):
            cookie_val = create_session_cookie_value(VALID_SECRET, duration_seconds=3600)
            handler = self._create_mock_handler(headers={"Cookie": f"{AUTH_SESSION_COOKIE_NAME}={cookie_val}"})
            self.assertTrue(handler._is_authenticated())

    def test_auth_with_expired_hmac_cookie(self):
        with patch.dict(os.environ, {"UFFICO_AUTH_TOKEN": VALID_SECRET}):
            cookie_val = create_session_cookie_value(VALID_SECRET, duration_seconds=-10)
            handler = self._create_mock_handler(headers={"Cookie": f"{AUTH_SESSION_COOKIE_NAME}={cookie_val}"})
            self.assertFalse(handler._is_authenticated())

    def test_no_auth_provided_rejected(self):
        with patch.dict(os.environ, {"UFFICO_AUTH_TOKEN": VALID_SECRET}):
            handler = self._create_mock_handler()
            self.assertFalse(handler._is_authenticated())

    def test_missing_server_secret_rejects_all(self):
        """Se il server non ha il secret configurato, l'accesso è sempre negato (non aperto)."""
        with patch.dict(os.environ, {}, clear=True):
            handler = self._create_mock_handler(headers={"X-Ufficio-Token": "some-random-token"})
            self.assertFalse(handler._is_authenticated())


class TestPostBodyLimits(unittest.TestCase):
    def _create_mock_handler(self, headers=None, body=b"", path="/"):
        import io
        handler = MagicMock(spec=UfficioHealthHandler)
        handler.headers = headers or {}
        handler.path = path
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler._is_authenticated = MagicMock(return_value=True)
        handler.do_POST = UfficioHealthHandler.do_POST.__get__(handler, UfficioHealthHandler)
        return handler

    def test_oversized_post_body_rejected_with_413(self):
        huge_size = MAX_POST_BODY_BYTES + 1024
        handler = self._create_mock_handler(headers={"Content-Length": str(huge_size)})
        handler.do_POST()
        handler.send_response.assert_called_with(413)

    def test_invalid_content_length_rejected_with_400(self):
        handler = self._create_mock_handler(headers={"Content-Length": "not-a-number"})
        handler.do_POST()
        handler.send_response.assert_called_with(400)


if __name__ == "__main__":
    unittest.main()
