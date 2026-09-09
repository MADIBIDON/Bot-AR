"""Discord notification package.

Applies the certifi SSL fix here, in the package's own __init__, because
Python always executes a package's __init__.py before any of its
submodules. aiohttp caches its default SSLContext as a module-level
singleton the first time `aiohttp.connector` is imported (which happens as
soon as anything does `import discord`) — setting SSL_CERT_FILE after that
point has no effect. Running the fix here guarantees it happens before any
submodule of this package (client.py, formatter.py) gets a chance to
`import discord` itself.
"""

from __future__ import annotations

from notifications.discord.ssl_fix import ensure_ssl_certificate_bundle

ensure_ssl_certificate_bundle()
