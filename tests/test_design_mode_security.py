#!/usr/bin/env python3
"""
Unit Tests for Design Mode SSRF Hardening & URL Validation
---------------------------------------------------------
Verifies:
1. Rejection of non-HTTP schemes (file://, ftp://, gopher://, etc.)
2. Rejection of loopback, unspecified, link-local, and private networks (127.0.0.1, 10.x, 192.168.x, 169.254.x, localhost, ::1)
3. Acceptance of valid public HTTPS URLs (with mocked socket getaddrinfo to avoid real network)
4. Acceptance of hostnames specified in TAKTSTOCK_DESIGN_ALLOWED_HOSTS (explicit allowlist)
5. Blocking of HTTP redirects toward private or insecure destinations (SafeRedirectHandler)
6. DesignCapture.capture_url returns immediate error without launching browser or making network calls
"""

import os
import sys
import socket
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER_DIR))

from url_validator import (
    validate_safe_url,
    get_allowed_hosts,
    SafeRedirectHandler,
)
from design_mode import DesignCapture


class TestUrlValidatorSsrf(unittest.TestCase):
    def test_non_http_schemes_strictly_rejected(self):
        """1. file:// and non-HTTP schemes must be rejected."""
        test_urls = [
            "file:///etc/passwd",
            "file:///home/massimo/taktstock/.env",
            "ftp://ftp.example.com/file.txt",
            "gopher://127.0.0.1:70",
            "javascript:alert(1)",
            "data:text/html,<h1>Hello</h1>",
        ]
        for url in test_urls:
            is_safe, err_msg = validate_safe_url(url)
            self.assertFalse(is_safe, f"Should reject non-HTTP scheme: {url}")
            self.assertIn("not allowed", err_msg.lower())

    def test_loopback_and_private_ips_rejected(self):
        """2. localhost, 127.0.0.1, 192.168.x.x, 10.x.x.x, 169.254.x.x must be rejected."""
        blocked_urls = [
            "http://localhost:8080/admin",
            "http://localhost.localdomain/secret",
            "http://127.0.0.1:8765/health",
            "http://127.0.1.1/",
            "http://10.0.0.1/dashboard",
            "http://10.255.255.255/",
            "http://172.16.0.1/",
            "http://172.31.255.255/",
            "http://192.168.1.1/router",
            "http://192.168.0.254/",
            "http://169.254.169.254/latest/meta-data",  # AWS/Cloud metadata
            "http://0.0.0.0/",
            "http://[::1]/",
            "http://[fe80::1]/",
        ]
        for url in blocked_urls:
            is_safe, err_msg = validate_safe_url(url)
            self.assertFalse(is_safe, f"Should block private/loopback URL: {url}")
            self.assertIn("blocked", err_msg.lower())

    def test_domain_resolving_to_private_ip_rejected(self):
        """Domains resolving to private/loopback IPs (e.g. via DNS rebinding) must be blocked."""
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))]
        with patch("socket.getaddrinfo", return_value=fake_addrinfo):
            is_safe, err_msg = validate_safe_url("http://malicious.local/test")
            self.assertFalse(is_safe)
            self.assertIn("loopback", err_msg.lower())

    def test_valid_public_https_url_accepted_without_network_call(self):
        """3. Valid public HTTPS URL accepted without network calls."""
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        with patch("socket.getaddrinfo", return_value=fake_addrinfo):
            is_safe, err_msg = validate_safe_url("https://example.com/app")
            self.assertTrue(is_safe)
            self.assertIsNone(err_msg)

    def test_hostname_in_allowlist_accepted(self):
        """4. Hostname explicitly configured in TAKTSTOCK_DESIGN_ALLOWED_HOSTS is accepted."""
        with patch.dict(os.environ, {"TAKTSTOCK_DESIGN_ALLOWED_HOSTS": "appsmith.internal, localhost"}):
            # localhost in explicit allowlist
            is_safe_lh, err_lh = validate_safe_url("http://localhost:3000/app")
            self.assertTrue(is_safe_lh)
            self.assertIsNone(err_lh)

            # appsmith.internal in explicit allowlist
            is_safe_app, err_app = validate_safe_url("http://appsmith.internal:8080/dashboard")
            self.assertTrue(is_safe_app)
            self.assertIsNone(err_app)

            # other internal host not in allowlist is still blocked
            is_safe_other, _ = validate_safe_url("http://192.168.1.100:8080")
            self.assertFalse(is_safe_other)


class TestRedirectHandlerSecurity(unittest.TestCase):
    def test_redirect_to_private_ip_is_blocked(self):
        """5. HTTP redirect toward private host must raise HTTPError 403."""
        import urllib.error
        handler = SafeRedirectHandler()
        req = MagicMock()
        fp = MagicMock()
        headers = {}

        # Simulate redirect toward 169.254.169.254
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            handler.redirect_request(
                req, fp, 302, "Found", headers, "http://169.254.169.254/latest/meta-data"
            )
        self.assertEqual(ctx.exception.code, 403)
        self.assertIn("SSRF", ctx.exception.msg)


class TestDesignCaptureSecurity(unittest.TestCase):
    def test_capture_url_returns_error_immediately_on_ssrf(self):
        """6. DesignCapture.capture_url must return clear error without invoking browser or network."""
        capture = DesignCapture()
        result = capture.capture_url("http://127.0.0.1:8765/api/run")

        self.assertFalse(result["success"])
        self.assertEqual(result["driver"], "blocked")
        self.assertIn("SSRF", result["error"])
        self.assertIsNone(result["screenshot_path"])


if __name__ == "__main__":
    unittest.main()
