#!/usr/bin/env python3
"""
Taktstock URL Validator & SSRF Protection Module
------------------------------------------------
Provides strict security controls against SSRF (Server-Side Request Forgery) attacks:
- Permits only 'http' and 'https' schemes
- Blocks loopback, private networks, link-local, unspecified, multicast, and reserved IPs (IPv4 and IPv6)
- Resolves DNS to verify all IP addresses associated with the hostname
- Blocks HTTP redirects to forbidden hosts or IPs
- Supports explicit allowlist via TAKTSTOCK_DESIGN_ALLOWED_HOSTS (fallback UFFICIO_DESIGN_ALLOWED_HOSTS)
"""

import os
import socket
import ipaddress
import urllib.parse
import urllib.request
import urllib.error
import logging
from typing import Tuple, Optional, Set, List

logger = logging.getLogger("TaktstockUrlValidator")

ALLOWED_SCHEMES: Set[str] = {"http", "https"}


def get_allowed_hosts() -> Set[str]:
    """Retrieves list of hostnames explicitly permitted by TAKTSTOCK_DESIGN_ALLOWED_HOSTS or UFFICIO_DESIGN_ALLOWED_HOSTS."""
    raw = (os.environ.get("TAKTSTOCK_DESIGN_ALLOWED_HOSTS") or os.environ.get("UFFICIO_DESIGN_ALLOWED_HOSTS") or "").strip()
    if not raw:
        return set()
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def is_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> Tuple[bool, str]:
    """
    Checks if an IP address (IPv4 or IPv6) belongs to reserved or private categories:
    loopback, unspecified, multicast, link-local, private, reserved.
    """
    if ip.is_loopback:
        return True, f"Loopback address ({ip}) is not allowed"
    if ip.is_private:
        return True, f"Private network address ({ip}) is not allowed"
    if ip.is_link_local:
        return True, f"Link-local address ({ip}) is not allowed"
    if ip.is_unspecified:
        return True, f"Unspecified address ({ip}) is not allowed"
    if ip.is_multicast:
        return True, f"Multicast address ({ip}) is not allowed"
    if ip.is_reserved:
        return True, f"Reserved address ({ip}) is not allowed"
    return False, ""


def validate_safe_url(url: str) -> Tuple[bool, Optional[str]]:
    """
    Validates a URL to prevent SSRF attacks.
    Returns (is_safe, error_message).
    """
    if not url or not isinstance(url, str):
        return False, "Missing or invalid URL."

    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception as e:
        return False, f"Malformed URL: {e}."

    # 1. Scheme check
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return False, f"Scheme '{scheme}' is not allowed. Only 'http' and 'https' are permitted."

    hostname = parsed.hostname
    if not hostname:
        return False, "Missing hostname in URL."

    hostname_clean = hostname.strip().lower()

    # 2. Explicit allowlist check (exact hostname match)
    allowed_hosts = get_allowed_hosts()
    if hostname_clean in allowed_hosts:
        return True, None

    # 3. Check if hostname is an IP literal
    try:
        ip_obj = ipaddress.ip_address(hostname_clean)
        blocked, reason = is_ip_blocked(ip_obj)
        if blocked:
            return False, f"Access blocked: {reason}."
        return True, None
    except ValueError:
        # Not an IP literal, proceed to domain name validation
        pass

    # Explicit check on known loopback hostnames
    if hostname_clean in {"localhost", "localhost.localdomain"}:
        return False, f"Access blocked: Hostname '{hostname_clean}' is not allowed."

    # 4. DNS resolution to verify associated IPs
    try:
        addr_info = socket.getaddrinfo(hostname_clean, None)
    except socket.gaierror as e:
        return False, f"DNS resolution failed for '{hostname_clean}': {e}."
    except Exception as e:
        return False, f"Error during DNS resolution for '{hostname_clean}': {e}."

    if not addr_info:
        return False, f"No IP addresses resolved for '{hostname_clean}'."

    # Check all IPs returned by DNS
    for entry in addr_info:
        sockaddr = entry[4]
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            blocked, reason = is_ip_blocked(ip_obj)
            if blocked:
                return False, f"Access blocked for '{hostname_clean}': {reason}."
        except ValueError:
            return False, f"Invalid IP address '{ip_str}' returned by DNS."

    return True, None


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib handler that blocks HTTP redirects to dangerous hosts or IPs."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        is_safe, err_msg = validate_safe_url(newurl)
        if not is_safe:
            raise urllib.error.HTTPError(
                newurl, 403, f"Redirect blocked for SSRF security reasons: {err_msg}", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)
