"""Points Python's ssl module at certifi's CA bundle.

Some macOS Python builds (notably python.org installers) don't use the
system certificate store, causing SSL verification failures when
connecting to Discord. This supplies a valid, well-maintained CA bundle —
it never disables verification and never sets ssl=False anywhere. Replaces
having to run `export SSL_CERT_FILE="$(python -m certifi)"` manually in
every terminal.
"""

from __future__ import annotations

import contextlib
import os
import sys

import certifi


def ensure_ssl_certificate_bundle() -> None:
    """Idempotent. Uses setdefault(): an operator-provided SSL_CERT_FILE
    (e.g. a corporate CA bundle) always wins over this."""
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    _refresh_already_cached_aiohttp_context()


def _refresh_already_cached_aiohttp_context() -> None:
    """Best-effort safety net for import ordering.

    aiohttp caches its default SSLContext as a module-level singleton the
    moment `aiohttp.connector` is first imported (which happens as soon as
    anything does `import discord`) — using whatever SSL_CERT_FILE was set
    at that instant. If aiohttp was already imported before this function
    got a chance to run, rebuild that cached context now that the env var
    is set, instead of depending on getting every import order exactly
    right everywhere `discord` might get imported. Reaches into a private
    aiohttp attribute deliberately as a fallback only: if aiohttp changes
    its internals, this quietly does nothing and we fall back to correct
    import ordering (see notifications/discord/__init__.py).
    """
    connector_module = sys.modules.get("aiohttp.connector")
    if connector_module is None:
        return
    make_context = getattr(connector_module, "_make_ssl_context", None)
    if make_context is None:
        return
    with contextlib.suppress(Exception):
        connector_module._SSL_CONTEXT_VERIFIED = make_context(True)
