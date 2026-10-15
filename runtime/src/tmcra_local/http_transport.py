"""Keep loopback requests local without changing remote provider networking."""
from __future__ import annotations

import ipaddress
import urllib.request
from urllib.parse import urlsplit


def is_loopback_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname or ""
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even another loopback port may be an unrelated process. Require the
        # configured endpoint to answer directly; never forward credentials.
        return None


def open_request(request: urllib.request.Request, *, timeout: float):
    if is_loopback_url(request.full_url):
        # ProxyHandler({}) also disables proxies discovered from macOS system
        # settings. Do not install a global opener or mutate the environment.
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        ).open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)
