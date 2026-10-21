"""Retained providers drive real participation gates, classifier and guard scopes."""

from decimal import Decimal
from typing import Literal
from unittest.mock import AsyncMock

import httpx
import pytest
from daimon.core.anthropic_guard import anthropic_disabled, guard_selfhosted_channel
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.config import ThreadParticipationSettings
from daimon.core.participation_gates import ParticipationGates
from daimon.core.stores import mux_state
from daimon.core.thread_participation import (
    ClassifierMessage,
    ParticipationMode,
    ResolvedParticipation,
)
from daimon.testing.factories import (
    make_account,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
)
from daimon.testing.isolated_sdk import build_async_anthropic
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ThreadRef
from mux.contracts.resources import ProviderBinding
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
@pytest.mark.parametrize("retained", ["selfhosted", "anthropic", "new"])
@pytest.mark.parametrize("shared", [False, True])
async def test_participation_uses_retained_backend_before_history_and_classifier(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: Literal["discord", "slack", "teams"],
    retained: str,
    shared: bool,
) -> None:
    tenant = await make_tenant(db_session, platform=platform)
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform=platform, external_id="caller", tenant=tenant, account=account
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal(100))
    channel = channel_ref(tenant.id, platform, "channel")
    local = BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-6-luna")
    if shared:
        await set_channel_backend(db_session, channel, BackendConfig(thread_mode="shared"))
    original = await set_channel_backend(
        db_session,
        channel,
        local
        if retained == "selfhosted"
        else BackendConfig(thread_mode="shared" if shared else "per_caller"),
    )
    if retained != "new":
        await mux_state.put_binding(
            db_session,
            ProviderBinding(
                id="retained",
                thread=ThreadRef(channel=channel, thread_id="thread"),
                provider=original.backend,
                profile=original.profile,
                native_refs={},
                generation=1,
                config_revision=original.local,
                legacy_account_id=None if shared and retained == "anthropic" else str(account.id),
            ),
            expected_generation=0,
        )
    await set_channel_backend(
        db_session, channel, BackendConfig() if retained == "selfhosted" else local
    )
    await db_session.commit()
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "msg_classifier",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5-20251001",
                "content": [
                    {"type": "text", "text": '{"decision":"respond","reason":"addressed"}'}
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 100, "output_tokens": 10},
            },
        )

    client = build_async_anthropic(
        transport=httpx.MockTransport(handle), api_key="test", base_url="https://api.anthropic.com"
    )
    recent = AsyncMock(return_value=[ClassifierMessage("person", "private history", False)])
    gates = ParticipationGates(
        platform=platform,
        settings=ThreadParticipationSettings(mode=ParticipationMode.ON),
        sessionmaker=db_session_factory,
        anthropic=client,
        bot_display_name="Daimon",
        billing_config=None,
        markup=Decimal(1),
    )
    try:
        async with guard_selfhosted_channel(
            db_session_factory,
            tenant_id=tenant.id,
            platform=platform,
            channel_id="channel",
            thread_id="thread",
            caller_id="caller",
        ) as disabled:
            assert disabled == (retained != "anthropic")
            assert anthropic_disabled() == disabled
            result = await gates.should_respond(
                tenant_id=tenant.id,
                channel_id="channel",
                thread_id="thread",
                caller_id="caller",
                candidates=[ClassifierMessage("person", "organic message", False)],
                recent=recent,
                resolved=ResolvedParticipation(ParticipationMode.ON, "thread"),
            )
        assert result == (retained == "anthropic")
        assert len(requests) == (1 if retained == "anthropic" else 0)
        assert recent.await_count == len(requests)
        if requests:
            assert b"private history" in requests[0].content
    finally:
        await client.close()
    assert not anthropic_disabled()
