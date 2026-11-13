"""`drained`: waiting out a cancelled helper never eats the turn's own cancellation.

The stuck-turn contract cases reconnect thousands of times a second, and the
ceiling's `wait_for` cancellation used to land now and then on the `finally` that
drains the stop waiter. The drain dropped it, the turn reconnected until the
test's outer bound, and the case failed with TimeoutError instead of ending at
its ceiling.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest
from daimon.core.turn.drain import drained

TURN = pathlib.Path(__file__).parents[2] / "daimon" / "core" / "turn"


async def _slow_to_stop() -> None:
    """A helper that takes a moment to finish once cancelled, so a second
    cancellation can land while its owner waits for it."""
    try:
        await asyncio.Event().wait()
    finally:
        await asyncio.sleep(0.05)


async def test_a_cancelled_helper_is_drained_quietly() -> None:
    helper = asyncio.create_task(_slow_to_stop())
    await asyncio.sleep(0)
    helper.cancel()
    with drained():
        await helper
    assert helper.cancelled()


async def test_the_owners_cancellation_during_a_drain_propagates() -> None:
    draining = asyncio.Event()
    ran_on: list[bool] = []

    async def owner() -> None:
        helper = asyncio.create_task(_slow_to_stop())
        await asyncio.sleep(0)
        helper.cancel()
        draining.set()
        with drained():
            await helper
        ran_on.append(True)

    task = asyncio.create_task(owner())
    await draining.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not ran_on


async def test_a_task_unwinding_from_its_cancellation_finishes_its_cleanup() -> None:
    """The cancellation the task is already handling is not raised again by a
    drain in its `finally`, so the steps after the drain still run."""
    started = asyncio.Event()
    cleaned: list[str] = []

    async def owner() -> None:
        helper = asyncio.create_task(_slow_to_stop())
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            helper.cancel()
            with drained():
                await helper
            cleaned.append("closed the stream")

    task = asyncio.create_task(owner())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == ["closed the stream"]


async def test_a_ceiling_landing_during_a_drain_ends_the_wait() -> None:
    """The ceiling path: `wait_for` times out while the turn drains a helper."""
    ran_on: list[bool] = []

    async def turn() -> None:
        helper = asyncio.create_task(_slow_to_stop())
        await asyncio.sleep(0)
        helper.cancel()
        with drained():
            await helper
        ran_on.append(True)
        await asyncio.sleep(0.2)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(turn(), timeout=0.01)
    assert not ran_on


async def test_only_the_named_exceptions_are_dropped() -> None:
    async def fails() -> None:
        raise ValueError("boom")

    helper = asyncio.create_task(fails())
    with pytest.raises(ValueError), drained(asyncio.CancelledError):
        await helper


def _suppresses_cancellation(node: ast.Call) -> bool:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name != "suppress":
        return False
    for arg in node.args:
        arg_name = arg.attr if isinstance(arg, ast.Attribute) else getattr(arg, "id", None)
        if arg_name in {"CancelledError", "BaseException"}:
            return True
    return False


NOT_A_DRAIN = {
    ("driver.py", "_decide_or_refuse_on_cancel"): (
        "already handling the turn's cancellation: it shields the abort cleanup "
        "through repeated cancellations, then re-raises the original"
    ),
}
"""(module, top-level function) to why its `suppress` is not a drain."""


def _offenders(path: pathlib.Path) -> list[str]:
    found: list[str] = []
    for top in ast.parse(path.read_text(), filename=str(path)).body:
        name = getattr(top, "name", "<module>")
        if (path.name, name) in NOT_A_DRAIN:
            continue
        found.extend(
            f"{path.name}:{node.lineno} ({name})"
            for node in ast.walk(top)
            if isinstance(node, ast.Call) and _suppresses_cancellation(node)
        )
    return found


def test_every_turn_module_drains_through_the_helper() -> None:
    """A `suppress(CancelledError)` or `suppress(BaseException)` around a task
    await would drop the turn's own cancellation. Every turn module uses `drained`."""
    assert [found for path in sorted(TURN.glob("*.py")) for found in _offenders(path)] == []


def test_every_allowed_suppress_still_exists() -> None:
    """A moved or removed exception cannot leave a stale allow-list entry."""
    for module, function in NOT_A_DRAIN:
        tree = ast.parse((TURN / module).read_text())
        top = next(node for node in tree.body if getattr(node, "name", None) == function)
        assert any(
            isinstance(node, ast.Call) and _suppresses_cancellation(node) for node in ast.walk(top)
        ), (module, function)
