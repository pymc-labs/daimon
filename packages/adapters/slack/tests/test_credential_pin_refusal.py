"""A Slack private form for a pinned agent, asked for outside its channels, is refused
before it is consumed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from daimon.adapters.slack.credential_submissions import (
    _validate_submission,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_pins import PIN_WRITE_REFUSAL
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.credential_requests import (
    create_credential_request,
    peek_credential_request,
)
from daimon.testing import MARouter, build_fake_anthropic, ma_agent
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_a_pinned_target_is_refused_before_the_form_is_consumed(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PIN_FORM")
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
    async with db_session_factory.begin() as session:
        await create_credential_request(
            session,
            token="tok-pin",
            kind="env",
            tenant_id=tenant.id,
            agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="ag_acme"),
            account_id=uuid.uuid4(),
            target="NOTES_TOKEN",
            mcp_server_url=None,
            requester_platform_user_id="U1",
            channel_id="C_CLIENTB",
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="ag_acme",
            target_name="Acme",
            requested_work=None,
            platform="slack",
            parent_channel_id="C_CLIENTB",
            origin_thread_id="1700000000.000001",
        )
        # The pin lands after the card was posted.
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy(agent_channel_pins={"acme-config": ("C_ACME",)}),
        )
    client = SimpleNamespace(chat_postEphemeral=AsyncMock())

    row = await _validate_submission(
        runtime,  # type: ignore[arg-type]
        client,  # type: ignore[arg-type]
        token="tok-pin",
        team_id="T_PIN_FORM",
        user_id="U1",
        channel_id="C_CLIENTB",
        kind="env",
    )

    assert row is None
    assert client.chat_postEphemeral.await_args.kwargs["text"] == PIN_WRITE_REFUSAL
    async with db_session_factory() as session:
        live = await peek_credential_request(session, token="tok-pin")
    assert live is not None and live.used_at is None, "nothing was consumed or written"
