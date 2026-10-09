"""The experimental turn path requires an explicit opt-in."""

from __future__ import annotations

import pytest
from daimon.core.config import TurnSettings, load_settings, load_turn_settings
from pydantic import ValidationError


@pytest.mark.parametrize("value", [None, "", "legacy", "mux"])
def test_turn_flag_matches_main_settings_and_turn_only_loader(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    monkeypatch.delenv("DAIMON_TURN", raising=False)
    if value is None:
        monkeypatch.delenv("DAIMON_TURN__PATH", raising=False)
    else:
        monkeypatch.setenv("DAIMON_TURN__PATH", value)
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://offline:offline@localhost/db")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "offline-fixture")
    expected = value or "legacy"
    assert load_settings(_env_file=None).turn.path == expected
    assert load_turn_settings(_env_file=None).path == expected
    assert TurnSettings().path == "legacy"


def test_turn_only_loader_needs_no_unrelated_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_TURN__PATH", "mux")
    monkeypatch.delenv("DAIMON_TURN", raising=False)
    monkeypatch.delenv("DAIMON_DATABASE__URL", raising=False)
    monkeypatch.delenv("DAIMON_ANTHROPIC__API_KEY", raising=False)
    assert load_turn_settings(_env_file=None).path == "mux"


def test_turn_path_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_TURN__PATH", "typo")
    with pytest.raises(ValidationError):
        load_turn_settings(_env_file=None)


@pytest.mark.parametrize("explicit", [False, True])
async def test_runtime_path_fallback_honors_env_and_keeps_explicit_overrides(monkeypatch, explicit):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from uuid import UUID

    from daimon.core.ma_resolver import new_resolver_cache
    from daimon.core.scope import DeploymentDefault
    from daimon.core.turn.deps import build_turn_deps
    from daimon.core.turn.run import _turn_port_kwargs
    from daimon.testing.ma_transport import ScriptedTransport

    monkeypatch.setenv("DAIMON_TURN__PATH", "mux")
    settings = MagicMock()
    settings.crypto.keys = []
    settings.public_url = None
    settings.turn = TurnSettings(path="legacy") if explicit else MagicMock()
    transport = ScriptedTransport()
    async with transport.client() as client:
        deps = build_turn_deps(
            settings,
            client,
            MagicMock(),
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
            billing_config=None,
        )
        kwargs = _turn_port_kwargs(
            deps,
            SimpleNamespace(grant=None, account_id=UUID(int=4)),
            "session",
            tenant_id=UUID(int=3),
        )
    assert deps.turn_path == ("legacy" if explicit else None)
    assert kwargs["path"] == ("legacy" if explicit else "mux")
    if not explicit:
        assert kwargs["scope"].tenant_id == str(UUID(int=3))
        assert kwargs["scope"].account_id == str(UUID(int=4))
    assert transport.requests == []
