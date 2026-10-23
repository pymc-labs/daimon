"""CLI approval settings need current native admin authority at every scope."""

from io import StringIO
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.cli.commands import channels
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.errors import DaimonError
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed


@pytest.mark.parametrize("target,thread", [(None, False), ("123", False), ("456", True)])
@pytest.mark.parametrize("value", ["off", "inherit"])
async def test_cli_uses_personal_credential_and_propagates_refusal(
    monkeypatch: pytest.MonkeyPatch,
    target: str | None,
    thread: bool,
    value: str,
) -> None:
    from typing import cast
    from unittest.mock import MagicMock

    import fastmcp
    from daimon.core.access_policy import Approvals
    from fastmcp.exceptions import ToolError

    client = AsyncMock()
    client.call_tool.side_effect = ToolError("Only a current server admin can change approvals.")
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=client)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(fastmcp, "Client", factory)
    with pytest.raises(DaimonError, match="admin"):
        await channels.channels_approvals_set(
            console=Console(file=StringIO()),
            url="https://daimon.example/mcp",
            token="personal-member-credential",
            value=cast(Approvals, value),
            channel_id=target,
            thread=thread,
        )
    factory.assert_called_once_with("https://daimon.example/mcp", auth="personal-member-credential")
    client.call_tool.assert_awaited_once_with(
        "set_approval_gate", {"value": value, "channel_id": target, "thread": thread}
    )


def test_cli_rejects_claimed_admin_id() -> None:
    from daimon.adapters.cli.main import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        app,
        [
            "channels",
            "approvals",
            "set",
            "off",
            "--url",
            "https://daimon.example/mcp",
            "--token",
            "personal-member-credential",
            "--admin-user-id",
            "public-owner-id",
        ],
    )
    assert result.exit_code == 2
    assert "No such option" in result.output


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
