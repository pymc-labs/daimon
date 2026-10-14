"""Tests for DiscordRuntime construction and build_runtime bootstrap."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.scope import DeploymentDefault
from daimon.testing.turn_fakes import deps_for


def _make_runtime() -> DiscordRuntime:
    """Build a DiscordRuntime with stub values for structural tests."""
    return DiscordRuntime(
        settings=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub for structural test
        anthropic=MagicMock(),  # pyright: ignore[reportArgumentType]
        sessionmaker=MagicMock(),  # pyright: ignore[reportArgumentType]
        notebook_rate_limiter=RateLimiter(max_requests=999),
        billing_config=None,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        turn_deps=replace(deps_for(), turn_path=None),  # stub for structural test
    )


class TestDiscordRuntime:
    def test_is_frozen_dataclass(self) -> None:
        assert dataclasses.is_dataclass(DiscordRuntime), "DiscordRuntime should be a dataclass"
        fields = {f.name for f in dataclasses.fields(DiscordRuntime)}
        assert fields == {
            "settings",
            "anthropic",
            "sessionmaker",
            "notebook_rate_limiter",
            "billing_config",
            "deployment_default",
            "resolver_cache",
            "turn_deps",
            "mcp_token_probe",
            "group_members",
            "owner_key",
            "ownership_lost",
            "gateway_lease",
            "fleet_card_factory",
        }, f"expected 14 fields, got {fields}"
        factory_field = next(
            field
            for field in dataclasses.fields(DiscordRuntime)
            if field.name == "fleet_card_factory"
        )
        assert factory_field.default is None

    def test_frozen(self) -> None:
        """DiscordRuntime should be immutable (frozen=True)."""
        rt = _make_runtime()
        with pytest.raises(dataclasses.FrozenInstanceError):
            rt.billing_config = None  # type: ignore[misc]  # intentionally testing frozen
