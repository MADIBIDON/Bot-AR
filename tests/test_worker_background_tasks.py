"""No real network: confirms app/worker.py's background-task plumbing
(purchase attempts and discovery runs, both fired via the same
fire-and-forget shape) never leaves an exception unretrieved, always
cleans up `_background_tasks`, and that drain_background_tasks() used at
shutdown (app/main_worker.py) actually waits for in-flight work and
cancels anything that runs past its timeout.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.worker import _background_tasks, _fire_and_forget, drain_background_tasks


async def _boom() -> None:
    raise RuntimeError("simulated background task failure")


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def test_background_task_exception_is_logged_not_swallowed_or_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The done-callback calls task.exception() — which is what actually
    prevents asyncio's "Task exception was never retrieved" warning —
    and this confirms it also surfaces the failure via logging rather
    than silently discarding it."""

    async def scenario() -> None:
        with caplog.at_level(logging.ERROR, logger="app.worker"):
            _fire_and_forget(_boom())
            while _background_tasks:
                await asyncio.sleep(0)

    asyncio.run(scenario())

    assert any("background purchase attempt crashed" in r.message for r in caplog.records)


def test_background_tasks_set_is_empty_after_many_complete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No leak across many ticks: the set always drains back to empty."""

    async def scenario() -> None:
        for _ in range(50):
            _fire_and_forget(_sleep(0))
        while _background_tasks:
            await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(_background_tasks) == 0


def test_background_tasks_set_never_leaks_at_thousands_scale() -> None:
    """Phase 26 audit, section 16: explicitly asks for a stress test with
    thousands of small synthetic tasks, not just a handful — confirms the
    done-callback cleanup (task discard from _background_tasks) keeps up
    and the set doesn't grow unbounded under a bursty flood."""

    async def scenario() -> int:
        peak = 0
        for _ in range(5000):
            _fire_and_forget(_sleep(0))
            peak = max(peak, len(_background_tasks))
        while _background_tasks:
            await asyncio.sleep(0)
        return peak

    peak_size = asyncio.run(scenario())

    assert len(_background_tasks) == 0
    # Sanity that this actually exercised concurrent pending tasks rather
    # than draining after every single one (which would make the "no
    # leak" assertion trivially true for the wrong reason).
    assert peak_size > 1


def test_drain_background_tasks_waits_for_fast_task_to_finish() -> None:
    async def scenario() -> bool:
        _fire_and_forget(_sleep(0.02))
        await drain_background_tasks(timeout=1.0)
        return len(_background_tasks) == 0

    assert asyncio.run(scenario()) is True


def test_drain_background_tasks_cancels_slow_task_after_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> bool:
        with caplog.at_level(logging.WARNING, logger="app.worker"):
            _fire_and_forget(_sleep(10))
            await drain_background_tasks(timeout=0.05)
        return len(_background_tasks) == 0

    drained_empty = asyncio.run(scenario())

    assert drained_empty is True
    assert any("still running after" in r.message for r in caplog.records)


def test_drain_background_tasks_is_a_no_op_with_nothing_pending() -> None:
    asyncio.run(drain_background_tasks(timeout=1.0))  # must not hang or raise
