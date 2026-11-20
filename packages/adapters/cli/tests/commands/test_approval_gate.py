"""Approval overrides have no CLI mutation surface or generic policy-clear bypass.

People change them in chat with `/settings` or `change_setting`, or in the web app;
the CLI deliberately has no setter.
"""

from io import StringIO

import pytest
from daimon.adapters.cli.commands import channels
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed


@pytest.mark.parametrize("operation", ["set", "clear"])
def test_cli_has_no_approval_mutation_commands(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from unittest.mock import MagicMock

    from daimon.adapters.cli.main import app
    from typer.testing import CliRunner

    runtime = MagicMock(side_effect=AssertionError("no runtime may be opened"))
    monkeypatch.setattr(channels, "build_runtime", runtime)
    result = CliRunner().invoke(app, ["channels", "approvals", operation, "off"])
    assert result.exit_code == 2
    assert "No such command" in result.output
    runtime.assert_not_called()


async def test_legacy_policy_clear_cannot_remove_gate_settings(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import typer
    from daimon.adapters.cli.commands.tenants import tenants_access_policy_set
    from daimon.core.stores.access_policy import set_access_policy

    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="123")
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(approvals="on", approvals_set_by="admin"),
        )
    with pytest.raises(typer.BadParameter, match="clear approval overrides"):
        await tenants_access_policy_set(
            rt=build_cli_runtime(db_session_factory),
            console=Console(file=StringIO()),
            platform="discord",
            external_id="123",
            clear=True,
            drop_agent_rules=True,
        )
    async with db_session_factory() as session:
        assert (await load_access_policy(session, tenant_id=tenant.id)).approvals == "on"
        assert not await list_events(session, tenant_id=tenant.id)


async def test_cli_policy_write_saves_no_automatic_rule_and_loses_none(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`tenants access-policy set` rebuilds from the loaded policy's dump."""
    from datetime import UTC, datetime, timedelta

    from daimon.adapters.cli.commands.tenants import tenants_access_policy_set
    from daimon.core.audience_facts import RoomFacts
    from daimon.core.permissions import effective_channel_rule
    from daimon.core.stores import access_policy as policy_store
    from daimon.core.stores.audience_facts import StoredRoom, publish_facts
    from sqlalchemy import text

    async def mode_on(*_: object, **__: object) -> str:
        return "on"

    monkeypatch.setattr(policy_store, "audience_mode", mode_on)
    narrow = "9000000000000002"
    now = datetime.now(UTC)
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="124")
        await publish_facts(
            session,
            tenant_id=tenant.id,
            rooms={narrow: StoredRoom(RoomFacts(contained=False, narrow=True))},
            now=now,
            fresh_until=now + timedelta(minutes=10),
        )
    await tenants_access_policy_set(
        rt=build_cli_runtime(db_session_factory),
        console=Console(file=StringIO()),
        platform="discord",
        external_id="124",
        internal_role=["222"],
    )
    async with db_session_factory() as session:
        stored = (
            await session.execute(
                text("SELECT policy FROM tenant_access_policies WHERE tenant_id = :t"),
                {"t": tenant.id},
            )
        ).scalar_one()
        policy = await policy_store.load_access_policy(session, tenant_id=tenant.id)
    assert "automatic" not in str(stored) and stored["internal_role_ids"] == ["222"]
    assert effective_channel_rule(policy, narrow).readers == "inside", "none was lost"
