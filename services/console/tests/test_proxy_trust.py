"""Client IP and cookie flags behind Cloudflare Tunnel (production) vs direct."""
from __future__ import annotations

import pytest
from starlette.requests import Request

from thaqip_console import auth


def _request(headers: dict[str, str], *, scheme: str = "http", peer: str = "172.18.0.9") -> Request:
    return Request({
        "type": "http", "method": "GET", "path": "/", "scheme": scheme,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (peer, 5555), "server": ("console", 8080), "query_string": b"",
    })


def test_without_trusted_proxy_a_spoofed_cf_header_is_ignored(monkeypatch):
    monkeypatch.setattr(auth, "TRUSTED_PROXY", "")
    assert auth.client_ip(_request({"CF-Connecting-IP": "1.2.3.4"})) == "172.18.0.9"


def test_behind_cloudflare_each_visitor_is_throttled_separately(monkeypatch):
    monkeypatch.setattr(auth, "TRUSTED_PROXY", "cloudflare")
    a = auth.client_ip(_request({"CF-Connecting-IP": "203.0.113.7"}))
    b = auth.client_ip(_request({"CF-Connecting-IP": "198.51.100.2"}))
    assert (a, b) == ("203.0.113.7", "198.51.100.2")
    # no header (e.g. a health check from inside the network): the peer address
    assert auth.client_ip(_request({})) == "172.18.0.9"


@pytest.mark.parametrize("forced,scheme,expected", [
    (False, "http", False), (False, "https", True), (True, "http", True)])
def test_secure_cookie_can_be_forced_when_tls_ends_at_the_edge(monkeypatch, forced, scheme, expected):
    monkeypatch.setattr(auth, "COOKIE_SECURE", forced)
    assert auth.cookie_secure(_request({}, scheme=scheme)) is expected
