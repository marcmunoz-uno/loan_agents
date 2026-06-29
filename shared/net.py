"""
shared/net.py — outbound URL safety (SSRF guard).

Used before fetching borrower-supplied URLs (Typeform asset statements,
inbound-email attachments). Deliberately DNS-free so it stays hermetic in
tests and adds no latency: it blocks the obvious SSRF vectors — non-http(s)
schemes, literal private/loopback/link-local IPs, localhost, and cloud
metadata endpoints. Hostnames are still resolved by the HTTP client itself;
DNS-rebinding to an internal IP is not covered here (see SECURITY notes).
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata",
}
# AWS/GCP/Azure link-local metadata service.
_BLOCKED_IPS = {"169.254.169.254", "100.100.100.200"}


class UnsafeURLError(ValueError):
    """Raised when a URL is not safe to fetch."""


def assert_safe_url(url: str) -> None:
    """Raise UnsafeURLError if the URL must not be fetched server-side."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"blocked scheme: {parsed.scheme or '(none)'}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeURLError("missing host")
    if host in _BLOCKED_HOSTS or host in _BLOCKED_IPS:
        raise UnsafeURLError(f"blocked host: {host}")
    # Literal IP address → reject anything not publicly routable.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    ):
        raise UnsafeURLError(f"blocked private/loopback IP: {host}")
