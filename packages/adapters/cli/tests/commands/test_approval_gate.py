"""CLI approval settings need current native admin authority at every scope."""

from io import StringIO
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.cli.commands import channels
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Subject
from daimon.core.errors import DaimonError
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed


@pytest.mark.parametrize("target,thread", [(None, False), ("123", False), ("456", True)])
async def test_nonadmin_cli_refused_without_policy_or_audit_changes(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    target: str | None,
    thread: bool,
) -> None:
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="123")
    fresh = AsyncMock(return_value=Subject(platform_user_id="member"))
    monkeypatch.setattr(channels, "approval_admin", fresh)
    runtime = build_cli_runtime(db_session_factory)
    with pytest.raises(DaimonError, match="admin"):
        await channels.channels_approvals_set(
            rt=runtime,
            console=Console(file=StringIO()),
            platform="discord",
            workspace_id="123",
            value="off",
            admin_user_id="member",
            channel_id=target,
            thread=thread,
        )
    async with db_session_factory() as session:
        assert await load_access_policy(session, tenant_id=tenant.id) == TenantAccessPolicy()
        assert not await list_events(session, tenant_id=tenant.id)
    assert fresh.await_args.kwargs["tenant_id"] == tenant.id


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
