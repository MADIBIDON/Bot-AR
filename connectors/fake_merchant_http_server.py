"""Phase 34: a REAL local HTTP server — genuine TCP/loopback socket, real
HTTP/1.1 request parsing via Python's own http.server — standing in for
a merchant's checkout-initiation endpoint during the warm-path benchmark.
Its purpose is to make the benchmark exercise actual connection-pool
acquisition and socket dispatch, not just in-memory Python function
calls. Never contacted by anything except scripts/benchmark_*.py; never
proxies to or touches a real merchant.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, required for connection reuse to matter

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # silence per-request stderr logging across a 5000-iteration benchmark

    def do_POST(self) -> None:  # noqa: N802 (http.server's own naming convention)
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"ok": True, "checkout_id": "fake-checkout-1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeMerchantServer:
    """A minimal, real, local checkout-initiation endpoint. Bind to port
    0 (the default) to let the OS assign a free port."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 0) -> None:
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)

    def __enter__(self) -> FakeMerchantServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
