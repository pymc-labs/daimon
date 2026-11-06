"""Every step the scheduler tick awaits sits behind one shared failure boundary.

`_run_loops` ends all loops when one loop raises, so a database error inside any
sweep would stop routine dispatch until a restart. `scheduler_step` is the shared
boundary; the contract test fails when a step in `tick` or the `--once` sequence
lacks it and is not in the allow-list.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing
from cryptography.fernet import Fernet, MultiFernet
from daimon.adapters.scheduler import main as scheduler_main
from daimon.adapters.scheduler.main import (
    _close_github_app_sessions,  # pyright: ignore[reportPrivateUsage]  # boundary behaviour test
    _refresh_github_app_sessions,  # pyright: ignore[reportPrivateUsage]  # boundary behaviour test
    _run_loops,  # pyright: ignore[reportPrivateUsage]  # loop-level test
    _run_one_tick_step,  # pyright: ignore[reportPrivateUsage]  # boundary behaviour test
    _sweep_github_app_tokens,  # pyright: ignore[reportPrivateUsage]  # boundary behaviour test
    _sweep_pending_files,  # pyright: ignore[reportPrivateUsage]  # boundary behaviour test
    scheduler_step,
)
from daimon.core.config import Settings
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import SessionTransaction

# Awaited calls that need no decorator, with the reason each is safe. Empty: every
# step goes through `scheduler_step`. Attribute calls (`await obj.method()`) are
# keyed by their source text.
ALLOWED_UNGUARDED: dict[str, str] = {}


def _call_name(call: ast.Call) -> str:
    """`name` for a plain call, source text such as `obj.method` for an attribute call."""
    return ast.unparse(call.func)


def _called_names(nodes: list[ast.AST]) -> list[str]:
    """Functions awaited, or called inside a lambda, under `nodes`."""
    names: list[str] = []
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                names.append(_call_name(node.value))
            elif isinstance(node, ast.Lambda):
                names.extend(
                    _call_name(inner)
                    for inner in ast.walk(node.body)
                    if isinstance(inner, ast.Call)
                )
    return names


def tick_step_names(source: str) -> set[str]:
    """Steps run by the `--once` path and by every callable passed to `_run_loops`.

    A callback can be a lambda, a name, or an expression holding names. A name that
    resolves to a function nested in the entrypoint (such as `tick`) is searched for
    its steps. A name that resolves to a module-level function is itself a step.
    """
    tree = ast.parse(source)
    module_level = {n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef)}
    nested = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name not in module_level
    }
    scopes: list[ast.AST] = []
    direct: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Attribute)
            and node.test.attr == "once"
        ):
            scopes.extend(node.body)
        if isinstance(node, ast.Call) and _call_name(node) == "_run_loops":
            for keyword in node.keywords:
                for inner in ast.walk(keyword.value):
                    if isinstance(inner, ast.Lambda):
                        scopes.append(inner)
                    elif isinstance(inner, ast.Name) and inner.id in nested:
                        scopes.append(nested[inner.id])
                    elif isinstance(inner, ast.Name) and inner.id in module_level:
                        direct.add(inner.id)
    return set(_called_names(scopes)) | direct


def unguarded_steps(source: str) -> set[str]:
    """Steps that neither carry `@scheduler_step` nor sit in the allow-list."""
    tree = ast.parse(source)
    guarded: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and any(
            isinstance(d, ast.Name) and d.id == "scheduler_step" for d in node.decorator_list
        ):
            guarded.add(node.name)
    return tick_step_names(source) - guarded - set(ALLOWED_UNGUARDED)


def _main_source() -> str:
    return Path(inspect.getfile(scheduler_main)).read_text()


def test_every_tick_step_is_behind_the_shared_boundary() -> None:
    steps = tick_step_names(_main_source())
    assert len(steps) >= 20, f"the parser found too few steps: {sorted(steps)}"
    assert "_sweep_github_app_tokens" in steps
    assert unguarded_steps(_main_source()) == set()


def test_allow_list_has_no_stale_entries() -> None:
    assert set(ALLOWED_UNGUARDED) <= tick_step_names(_main_source())


def test_contract_covers_run_one_tick_and_the_loop_callbacks() -> None:
    steps = tick_step_names(_main_source())
    assert {"_run_one_tick_step", "_sweep_compute", "_sweep_privacy_deletes"} <= steps
    assert "run_one_tick" not in steps


def test_contract_flags_a_named_unguarded_callback() -> None:
    source = _main_source().replace(
        "                compute_sweep=lambda: _sweep_compute(compute),\n",
        "                compute_sweep=extra_loop,\n",
        1,
    )
    source = source.replace(
        "        async def privacy_sweep() -> None:",
        "        async def extra_loop() -> None:\n            await _new_cleanup()\n\n"
        "        async def privacy_sweep() -> None:",
        1,
    )
    source += "\n\nasync def _new_cleanup() -> None:\n    return None\n"
    assert unguarded_steps(source) == {"_new_cleanup"}


def test_contract_flags_an_attribute_call_in_tick() -> None:
    source = _main_source().replace(
        "            await _sweep_long_jobs(long_jobs)\n            await _drain_pending_usage(",
        "            await long_jobs.cleanup()\n            await _drain_pending_usage(",
        1,
    )
    assert unguarded_steps(source) == {"long_jobs.cleanup"}


def test_contract_flags_a_new_unguarded_step() -> None:
    source = _main_source()
    marker = "            await _sweep_github_app_tokens(sm, fernet=push_resync_fernet)\n"
    assert marker in source
    added = source.replace(marker, marker + "            await _sweep_new_thing(sm)\n", 1)
    added += "\n\nasync def _sweep_new_thing(sm: object) -> None:\n    return None\n"
    assert unguarded_steps(added) == {"_sweep_new_thing"}


def test_contract_flags_a_step_whose_decorator_was_removed() -> None:
    source = _main_source().replace(
        "@scheduler_step\nasync def _sweep_github_app_tokens(",
        "async def _sweep_github_app_tokens(",
        1,
    )
    assert unguarded_steps(source) == {"_sweep_github_app_tokens"}


async def test_github_app_token_sweep_logs_a_database_error_instead_of_raising(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    with (
        patch.object(
            scheduler_main, "select_stale_tokens", AsyncMock(side_effect=SQLAlchemyError("down"))
        ),
        structlog.testing.capture_logs() as logs,
    ):
        await _sweep_github_app_tokens(db_session_factory, fernet=fernet)
    assert [e["event"] for e in logs] == ["scheduler.step.failed"]
    assert logs[0]["step"] == "_sweep_github_app_tokens"


async def test_pending_file_sweep_logs_a_database_error_instead_of_raising(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with (
        patch.object(
            scheduler_main,
            "sweep_pending_file_deletes",
            AsyncMock(side_effect=SQLAlchemyError("down")),
        ),
        structlog.testing.capture_logs() as logs,
    ):
        await _sweep_pending_files(MagicMock(), db_session_factory)
    assert [e["event"] for e in logs] == ["scheduler.step.failed"]


async def test_github_app_session_steps_log_a_database_error_instead_of_raising(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    fernet = MultiFernet([Fernet(Fernet.generate_key())])
    with (
        patch.object(
            scheduler_main, "list_live_app_sessions", AsyncMock(side_effect=SQLAlchemyError("x"))
        ),
        patch.object(
            scheduler_main, "list_closed_app_sessions", AsyncMock(side_effect=SQLAlchemyError("x"))
        ),
        structlog.testing.capture_logs() as logs,
    ):
        await _refresh_github_app_sessions(
            MagicMock(), db_session_factory, settings=MagicMock(spec=Settings), fernet=fernet
        )
        await _close_github_app_sessions(MagicMock(), db_session_factory, fernet=fernet)
    assert [e["step"] for e in logs] == [
        "_refresh_github_app_sessions",
        "_close_github_app_sessions",
    ]


async def test_scheduler_step_does_not_hide_other_errors() -> None:
    @scheduler_step
    async def broken() -> None:
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        await broken()


async def test_a_failing_step_does_not_stop_the_next_tick() -> None:
    stop = asyncio.Event()
    ticks = 0

    @scheduler_step
    async def failing_sweep() -> None:
        raise SQLAlchemyError("down")

    async def tick() -> None:
        nonlocal ticks
        ticks += 1
        await failing_sweep()
        if ticks == 3:
            stop.set()

    noop: Callable[[], Awaitable[None]] = AsyncMock()
    kwargs: dict[str, Any] = {"usage_sweep": None, "interval_s": 0.01, "stop_event": stop}
    async with asyncio.timeout(5):
        await _run_loops(
            tick=tick, privacy_sweep=noop, compute_sweep=noop, compute_task_poll=noop, **kwargs
        )
    assert ticks == 3


async def test_run_one_tick_step_logs_a_database_error_at_commit(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`run_one_tick` opens its transaction outside its own catches."""

    def commit_fails(self: SessionTransaction, *args: object, **kwargs: object) -> None:
        raise OperationalError("COMMIT", {}, Exception("connection lost"))

    with (
        patch.object(SessionTransaction, "commit", commit_fails),
        structlog.testing.capture_logs() as logs,
    ):
        await _run_one_tick_step(
            now=datetime.now(UTC),
            sm=db_session_factory,
            fire=AsyncMock(),
            max_age=timedelta(minutes=5),
            max_concurrent_fires=1,
            dispatch_timeout_s=5.0,
        )
    assert [e["event"] for e in logs if e["log_level"] == "error"] == ["scheduler.step.failed"]
    assert logs[-1]["step"] == "_run_one_tick_step"
