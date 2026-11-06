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
    _sweep_github_app_tokens,  # pyright: ignore[reportPrivateUsage]  # boundary behaviour test
    _sweep_pending_files,  # pyright: ignore[reportPrivateUsage]  # boundary behaviour test
    scheduler_step,
)
from daimon.core.config import Settings
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Awaited names that need no decorator, with the reason each is safe.
ALLOWED_UNGUARDED: dict[str, str] = {
    "run_one_tick": (
        "core pipeline in daimon.core.scheduler; it catches and logs each stage "
        "itself (advance_stale, claim_due_fireable, configured_routines, every fire)"
    ),
}


def _called_names(nodes: list[ast.stmt] | list[ast.AST]) -> list[str]:
    """Names of functions awaited, or called inside a lambda, under `nodes`."""
    names: list[str] = []
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                if isinstance(node.value.func, ast.Name):
                    names.append(node.value.func.id)
            elif isinstance(node, ast.Lambda):
                for inner in ast.walk(node.body):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                        names.append(inner.func.id)
    return names


def tick_step_names(source: str) -> set[str]:
    """Steps run by the repeating tick, the `--once` path and the side loops."""
    tree = ast.parse(source)
    scopes: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in {
            "tick",
            "usage_sweep",
            "privacy_sweep",
        }:
            scopes.append(node)
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Attribute)
            and node.test.attr == "once"
        ):
            scopes.extend(node.body)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_run_loops"
        ):
            scopes.extend(kw.value for kw in node.keywords if isinstance(kw.value, ast.Lambda))
    return set(_called_names(scopes))


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
