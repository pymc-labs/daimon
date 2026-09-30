"""`/dm` refuses to open a DM from a channel whose spending budget is used up.

Admission, the live role lookup and the DM store are patched on the module;
the budget check reads a real `channel_budgets` row.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.slack import direct_messages as dm_module
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.stores.channel_budgets import set_channel_budget
from daimon.core.stores.domain import Role
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_TEAM = "T_DM_BUDGET"
_CHANNEL = "C_SOURCE"


@pytest.mark.parametrize(("limit_usd", "refused"), [(Decimal("0"), True), (Decimal("5"), False)])
async def test_dm_from_a_channel_over_its_budget_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    limit_usd: Decimal,
    refused: bool,
) -> None:
    tenant = await provision_tenant(db_session_factory, platform="slack", workspace_id=_TEAM)
    async with db_session_factory.begin() as session:
        await set_channel_budget(
            session,
            tenant_id=tenant.tenant_id,
            platform="slack",
            channel_id=_CHANNEL,
            limit_usd=limit_usd,
            window="monthly",
            starts_at=None,
            ends_at=None,
            set_by_account_id=None,
        )
    client = MagicMock()
    client.conversations_history = AsyncMock(return_value={"messages": []})
    client.conversations_open = AsyncMock(return_value={"channel": {"id": "D_NEW"}})
    client.chat_postMessage = AsyncMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(dm_module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dm_module, "_live_role", AsyncMock(return_value=Role.USER))
    monkeypatch.setattr(dm_module, "require_dm_enabled", AsyncMock())
    monkeypatch.setattr(dm_module, "admit", AsyncMock(return_value=MagicMock()))
    start_dm = AsyncMock()
    monkeypatch.setattr(dm_module, "start_dm", start_dm)
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory

    await dm_module.handle_dm_command(
        runtime, {"team_id": _TEAM, "user_id": "U1", "channel_id": _CHANNEL, "text": ""}
    )

    reply = client.chat_postEphemeral.await_args.kwargs["text"]
    if refused:
        assert reply.startswith("This channel has used its spending budget."), reply
        client.conversations_open.assert_not_awaited()
        start_dm.assert_not_awaited()
    else:
        assert reply == "Ready in your DMs.", "a channel within its budget opens the DM"
        start_dm.assert_awaited_once()
