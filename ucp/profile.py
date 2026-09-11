"""Generates this project's public UCP agent profile document.

Usage:
    python -m ucp.profile [output_path]

Default output: docs/agent-profile.json (GitHub Pages serves anything
under docs/ on main at https://<user>.github.io/<repo>/<path> — see
README-ucp-hosting.md at the repo root for the one-time setup).

Re-run and commit whenever the profile changes (e.g. a UCP protocol
version bump) — this is a static file, not a running service.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ucp.models import AgentProfile

UCP_VERSION = "2026-08-25"
DEFAULT_OUTPUT_PATH = Path("docs") / "agent-profile.json"

# Live-observed root cause (Phase 21): a business only activates a
# capability for us if OUR profile ALSO declares that same capability
# name — "Capability Intersection", ucp.dev/2026-08-25 spec, Overview
# section. Confirmed live: with capabilities={} every tools/call on
# Kairyu/RelicTCG returned "Tool not found: search_catalog" (-32602)
# even though tools/list listed it — the tool exists but was never in
# the negotiated intersection. spec/schema URLs below are the exact
# ucp.dev-hosted values Kairyu's own /.well-known/ucp response declared
# for these same capability names (dev.ucp.* is the UCP governing body's
# own namespace — reusing its canonical schema URLs is exactly what
# Authority Binding requires, not a merchant-specific value).
_SHOPPING_CAPABILITY_URLS: dict[str, dict[str, str]] = {
    "dev.ucp.shopping.catalog.search": {
        "spec": "https://ucp.dev/2026-08-25/specification/shopping/catalog/",
        "schema": "https://ucp.dev/2026-08-25/schemas/shopping/catalog_search.json",
    },
    "dev.ucp.shopping.catalog.lookup": {
        "spec": "https://ucp.dev/2026-08-25/specification/shopping/catalog/",
        "schema": "https://ucp.dev/2026-08-25/schemas/shopping/catalog_lookup.json",
    },
    "dev.ucp.shopping.cart": {
        "spec": "https://ucp.dev/2026-08-25/specification/shopping/cart/",
        "schema": "https://ucp.dev/2026-08-25/schemas/shopping/cart.json",
    },
    "dev.ucp.shopping.checkout": {
        "spec": "https://ucp.dev/2026-08-25/specification/shopping/checkout/",
        "schema": "https://ucp.dev/2026-08-25/schemas/shopping/checkout.json",
    },
    # Live-observed, second finding: with only the four capabilities
    # above, catalog search/lookup and cart tools work, and get_checkout
    # resolves — but create_checkout still answers "Tool not found",
    # even though "dev.ucp.shopping.checkout" shows active in the
    # response envelope. Kairyu's own /.well-known/ucp declares
    # "dev.ucp.shopping.fulfillment" as an extension with
    # extends=["dev.ucp.shopping.checkout","dev.ucp.shopping.cart"] and
    # config {"multi_destination":[],"method_combinations":[["shipping"]]}
    # — checkout *creation* on this store apparently requires that
    # extension to also be in the intersection (shipping/fulfillment is
    # presumably mandatory for a physical-goods checkout). Values below
    # are copied verbatim from Kairyu's real discovery response.
    "dev.ucp.shopping.fulfillment": {
        "spec": "https://ucp.dev/2026-08-25/specification/shopping/extensions/fulfillment/",
        "schema": "https://ucp.dev/2026-08-25/schemas/shopping/fulfillment.json",
        "extends": ["dev.ucp.shopping.checkout", "dev.ucp.shopping.cart"],
    },
}


def build_profile() -> AgentProfile:
    """No services, no payment handlers: this agent only ever calls a
    merchant's own UCP shopping tools — it exposes none of its own and
    never handles payment itself. capabilities declares exactly what
    this project's dry-run pipeline calls (catalog search/lookup, cart,
    checkout, and the fulfillment extension checkout creation requires)
    — nothing broader, so intersection activates only what we use."""
    capabilities = {
        name: [{"version": UCP_VERSION, **urls}] for name, urls in _SHOPPING_CAPABILITY_URLS.items()
    }
    return AgentProfile(version=UCP_VERSION, capabilities=capabilities)


def write_profile(output_path: Path = DEFAULT_OUTPUT_PATH) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document = build_profile().to_dict()
    output_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return output_path


def main() -> int:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT_PATH
    written = write_profile(output_path)
    print(f"Wrote UCP agent profile to {written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
