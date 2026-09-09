"""Application entrypoint.

Phase 0: no monitoring, no Discord, no purchasing. This only proves the
project boots.
"""

from __future__ import annotations

from app import __version__

APP_NAME = "Retail Opportunity & Purchase Assistant"


def get_banner() -> str:
    return f"{APP_NAME} v{__version__}"


def main() -> None:
    print(get_banner())


if __name__ == "__main__":
    main()
