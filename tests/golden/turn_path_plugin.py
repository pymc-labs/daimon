"""Offline fixture authorization; leave SDK traffic and domain logic intact."""

from __future__ import annotations

import functools
import importlib
import os
import sys
from collections.abc import Mapping
from datetime import datetime
from types import ModuleType
from typing import Any, cast

import pytest
from runner import MUX_PENDING, TURN_PATHS, TurnPath, mux_turn_bridge_available


def install_turn_path(
    turn_path: TurnPath,
    *,
    source: ModuleType,
    patch: pytest.MonkeyPatch,
    clock: type[datetime],
) -> None:
    if not mux_turn_bridge_available():
        if turn_path == "mux":
            raise RuntimeError(MUX_PENDING)
        return

    from daimon.core.turn import driver
    from mux.contracts.ids import Scope

    original = driver.run_turn
    offline_scope = Scope(
        tenant_id="offline-oracle-tenant",
        account_id="offline-oracle-account",
        principal_id="offline-oracle-host",
        authorization_id="offline-scripted-turn",
    )

    @functools.wraps(original)
    async def selected_turn(**kwargs: Any) -> Any:
        if kwargs.get("path") not in (None, turn_path):
            raise AssertionError("Caller selected a different path from the oracle replay")
        kwargs["path"] = turn_path
        if turn_path == "mux" and kwargs.get("scope") is None:
            kwargs["scope"] = offline_scope
        return await original(**kwargs)

    # Cover the public export, driver, host imports and direct test aliases.
    # Never replace a mocked caller or a different function with the same name.
    modules = [
        module
        for name, module in tuple(cast(Mapping[str, object], sys.modules).items())
        if isinstance(module, ModuleType) and name.startswith("daimon.")
    ]
    if source not in modules:
        modules.append(source)
    for module in modules:
        for name, value in tuple(vars(module).items()):
            if value is original:
                patch.setattr(module, name, selected_turn)
    if turn_path == "mux":
        anthropic_turn = importlib.import_module("mux.drivers.anthropic.turn")

        # Absolute deadlines must use the same frozen clock as host fixtures.
        patch.setattr(anthropic_turn, "datetime", clock)


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup(item: Any) -> None:
    import oracle_plugin

    requested = os.environ.get("DAIMON_ORACLE_TURN_PATH", "legacy")
    if requested not in TURN_PATHS:
        raise ValueError("Oracle turn path must be legacy or mux")
    install_turn_path(
        requested,
        source=item.module,
        patch=oracle_plugin.PATCH,
        clock=oracle_plugin.Clock,
    )
