"""Continuous monitoring worker entrypoint. Runs indefinitely.

Populate WatchRules/Listings/Products via database/crud.py (or
scripts/watch.py add) before running this. See connectors/defaults.py for
which merchants are wired up.

Usage:
    python -m app.main_worker

Stops cleanly on Ctrl-C (SIGINT) or SIGTERM: the current tick always
finishes, then Discord, the database session, and the PID file are
cleaned up.

Writes its PID to data/worker.pid on startup and removes it on clean
shutdown, so `scripts/watch.py status` can report whether a worker looks
to be running — a plain PID file, not a process manager.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from dotenv import load_dotenv

from app.worker import DEFAULT_POLL_INTERVAL_SECONDS, run_forever
from connectors.defaults import build_default_registry
from database.session import create_all, get_engine, get_session_factory
from notifications.discord.client import DiscordNotifier
from notifications.discord.config import load_discord_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PID_FILE = Path("data") / "worker.pid"


async def main() -> None:
    load_dotenv(override=True)
    config = load_discord_config()

    engine = get_engine()
    create_all(engine)
    session = get_session_factory(engine)()

    registry = build_default_registry()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    try:
        async with DiscordNotifier(config) as notifier:
            await run_forever(
                session,
                registry,
                notifier,
                poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
                stop_event=stop_event,
            )
    finally:
        session.close()
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
