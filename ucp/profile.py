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


def build_profile() -> AgentProfile:
    """No services, no payment handlers, no capabilities: this agent only
    ever calls a merchant's own UCP shopping tools (cart, checkout) — it
    exposes none of its own and never handles payment itself."""
    return AgentProfile(version=UCP_VERSION)


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
