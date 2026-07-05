"""Outbound proxy configuration for the worker process.

All of the app's internet traffic — AI provider calls and the update-check
relay — leaves through ``requests`` in this worker. The embedded webview runs
with ``--no-proxy-server`` (loopback rendering must never depend on a corporate
proxy), so the worker is the single place where "how do we reach the internet"
is answered.

Resolution order (first match wins):

1. ``HTTP_PROXY``/``HTTPS_PROXY`` environment variables set *before* launch —
   respected untouched (standard tooling override).
2. The proxy URL stored (encrypted) via Preferences → Network. Applied by
   exporting the same env vars inside this worker process, so every
   ``requests`` call inherits it with no per-call plumbing. Supports Basic
   auth credentials in the URL (``http://user:pass@proxy:8080``).
3. The system proxy from the Windows registry (per-user Internet Settings) —
   ``requests`` picks this up automatically via ``urllib``'s ``getproxies()``.
   Note: only *fixed* proxy configurations are visible this way; a PAC-script
   config cannot be evaluated here and needs the explicit URL from (2).

``NO_PROXY`` always gets loopback entries so worker-internal calls can never
be routed out, whichever source applies.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request

from Application_Logic import Logic_AI_Credentials as credentials

logger = logging.getLogger(__name__)

_LOOPBACK_NO_PROXY = "127.0.0.1,localhost"

# Tracks whether the current env vars were exported by apply_stored_proxy(),
# so a later clear/update knows it may rewrite them (vs. a user-provided
# environment we must leave alone).
_applied_url: str | None = None


def mask_credentials(url: str) -> str:
    """``http://user:secret@proxy:8080`` → ``http://user:***@proxy:8080``."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.password:
            host = parts.hostname or ""
            if parts.port:
                host += f":{parts.port}"
            netloc = f"{parts.username}:***@{host}"
            return urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    except ValueError:
        pass
    return url


def validate_proxy_url(url: str) -> str | None:
    """Return an error message if the URL is not a usable proxy URL."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "Not a valid URL."
    if parts.scheme not in ("http", "https", "socks5", "socks5h"):
        return "Proxy URL must start with http://, https:// or socks5://."
    if not parts.hostname:
        return "Proxy URL has no host."
    return None


def _ensure_no_proxy() -> None:
    current = os.environ.get("NO_PROXY", "")
    entries = [e.strip() for e in current.split(",") if e.strip()]
    for needed in _LOOPBACK_NO_PROXY.split(","):
        if needed not in entries:
            entries.append(needed)
    os.environ["NO_PROXY"] = ",".join(entries)


def _env_proxy() -> str | None:
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var)
        if val:
            return val
    return None


def _system_proxy() -> str | None:
    """Fixed system proxy (Windows registry / Unix env), if any."""
    proxies = urllib.request.getproxies()
    return proxies.get("https") or proxies.get("http")


def apply_stored_proxy() -> None:
    """Export the stored proxy (if any) into this process's environment.

    Called at worker startup and after every change from the Network settings.
    A pre-existing user environment proxy always wins and is left untouched.
    """
    global _applied_url
    _ensure_no_proxy()

    env = _env_proxy()
    if env and env != _applied_url:
        logger.info("Outbound proxy from environment: %s", mask_credentials(env))
        return

    stored = credentials.get_network_proxy()
    if stored:
        os.environ["HTTP_PROXY"] = stored
        os.environ["HTTPS_PROXY"] = stored
        _applied_url = stored
        logger.info("Outbound proxy from settings: %s", mask_credentials(stored))
    elif _applied_url:
        # Setting was cleared while running — drop what we exported earlier.
        for var in ("HTTP_PROXY", "HTTPS_PROXY"):
            if os.environ.get(var) == _applied_url:
                del os.environ[var]
        _applied_url = None
        logger.info("Outbound proxy setting cleared; using system configuration")


def proxy_status() -> dict[str, str | bool | None]:
    """Effective outbound proxy, for display in Preferences → Network."""
    env = _env_proxy()
    stored = credentials.get_network_proxy()
    if env and env != _applied_url:
        source, url = "environment", env
    elif stored:
        source, url = "manual", stored
    else:
        system = _system_proxy()
        source, url = ("system", system) if system else ("direct", None)
    return {
        "source": source,
        "proxy_url": mask_credentials(url) if url else None,
        "manual_configured": bool(stored),
    }
