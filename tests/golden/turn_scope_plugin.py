"""Offline fixture authorization; leave SDK traffic and domain logic intact."""

from __future__ import annotations

import functools
import importlib
import sys
from collections.abc import Mapping
from datetime import datetime
from types import ModuleType
from typing import Any, cast

import pytest


def install_turn_scope(
    *, source: ModuleType, patch: pytest.MonkeyPatch, clock: type[datetime]
) -> None:
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
    async def scoped_turn(**kwargs: Any) -> Any:
        if kwargs.get("scope") is None:
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
                patch.setattr(module, name, scoped_turn)
    # Absolute deadlines must use the same frozen clock as host fixtures.
    patch.setattr(importlib.import_module("mux.drivers.anthropic.turn"), "datetime", clock)


@pytest.hookimpl(trylast=True)
def pytest_runtest_setup(item: Any) -> None:
    import oracle_plugin

    install_turn_scope(source=item.module, patch=oracle_plugin.PATCH, clock=oracle_plugin.Clock)
