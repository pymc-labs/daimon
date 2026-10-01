"""A Discord private form for a pinned agent, asked for outside its channels, is refused
before it is consumed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from daimon.adapters.discord.credential_origin import refuse_if_credential_target_unavailable
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import PIN_WRITE_REFUSAL
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing import MARouter, build_fake_anthropic, ma_agent
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_a_pinned_target_is_refused_before_the_form_is_consumed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="guild-pin-form")
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"acme-config": ("111",)}),
    )
    await db_session.commit()
    router = MARouter()
    router.add_agent_list(
        ma_agent(
            id="ag_acme", name="Acme", tenant_id=tenant.id, metadata={"daimon_name": "acme-config"}
        )
    )
    runtime = SimpleNamespace(
        anthropic=build_fake_anthropic(router.dispatch), sessionmaker=db_session_factory
    )
    now = datetime.now(UTC)
    row = CredentialRequestRow(
        token="tok",
        kind="env",
        tenant_id=tenant.id,
        agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_acme"),
        account_id=uuid.uuid4(),
        target="NOTES_TOKEN",
        mcp_server_url=None,
        requester_platform_user_id="42",
        channel_id="222",
        platform="discord",
        parent_channel_id="999",
        origin_thread_id="222",
        idempotency_key=uuid.uuid4(),
        target_name="old-name",
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        used_at=None,
    )
    interaction = SimpleNamespace(followup=SimpleNamespace(send=AsyncMock()))

    refused = await refuse_if_credential_target_unavailable(
        interaction,  # type: ignore[arg-type]
        runtime=runtime,  # type: ignore[arg-type]
        row=row,
    )

    assert refused is True
    interaction.followup.send.assert_awaited_once_with(PIN_WRITE_REFUSAL, ephemeral=True)
