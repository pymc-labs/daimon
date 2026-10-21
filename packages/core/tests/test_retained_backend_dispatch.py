"""Handover and queued follow-ups honor the provider retained by a thread."""

from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core.anthropic_guard import AnthropicDisabled, anthropic_disabled, without_anthropic
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.continuity.continuation import ContinuationRequest, decide_continuation
from daimon.core.continuity.dispatch import dispatch_pending_continuations
from daimon.core.continuity.wakes import enqueue_wake
from daimon.core.native_admins import AdminCaller
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores import mux_state
from daimon.core.stores.domain import TaskContinuationRow
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.stores.thread_agent_bindings import create_binding, get_binding
from daimon.core.thread_handoff import switch_thread_on_request
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.state.lease import Slot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class NoAnthropic:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"retained selfhosted thread accessed Anthropic.{name}")


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
@pytest.mark.parametrize("shared", [False, True], ids=["caller", "shared"])
@pytest.mark.parametrize("retained", ["selfhosted", "anthropic"])
async def test_handover_and_timer_use_retained_backend_after_channel_switch(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    platform: Literal["discord", "slack", "teams"],
    shared: bool,
    retained: Literal["selfhosted", "anthropic"],
) -> None:
    tenant = await make_tenant(db_session, platform=platform)
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, tenant=tenant, account=account, platform=platform, external_id="caller"
    )
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="other-channel"),
        tenant_id=tenant.id,
        agent_name="research",
    )
    channel = channel_ref(tenant.id, platform, "channel")
    local = BackendConfig(backend="selfhosted", profile="selfhosted.loop", model="gpt-5")
    if shared:
        await set_channel_backend(db_session, channel, BackendConfig(thread_mode="shared"))
    original = await set_channel_backend(
        db_session,
        channel,
        local
        if retained == "selfhosted"
        else BackendConfig(thread_mode="shared" if shared else "per_caller"),
    )
    binding = ProviderBinding(
        id="retained",
        thread=ThreadRef(channel=channel, thread_id="thread"),
        provider=original.backend,
        profile=original.profile,
        native_refs={},
        generation=1,
        config_revision=original.local,
        legacy_account_id=None if shared and retained == "anthropic" else str(account.id),
    )
    await mux_state.put_binding(db_session, binding, expected_generation=0)
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform=platform,
        parent_channel_id="channel",
        thread_id="thread",
        responder_ma_agent_id="sh_agent_original" if retained == "selfhosted" else "agt_original",
        responder_name="original",
        creator_account_id=account.id,
        kind="handoff",
    )
    await set_channel_backend(
        db_session, channel, BackendConfig() if retained == "selfhosted" else local
    )
    await db_session.commit()
    router = MARouter()
    router.add_agent(ma_agent(id="agt_research", name="research", tenant_id=tenant.id))
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return router.dispatch(request)

    client = (
        cast(AsyncAnthropic, NoAnthropic())
        if retained == "selfhosted"
        else build_fake_anthropic(handle)
    )
    try:
        outcome = await switch_thread_on_request(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            platform=platform,
            parent_channel_id="channel",
            thread_id="thread",
            ma_agent_id="agt_research",
            caller=AdminCaller(platform_user_id="caller", is_server_admin=True),
            default=DeploymentDefault(agent_name="original"),
            channel="this channel",
            now=datetime.now(UTC),
        )
        assert outcome.switched == (retained == "anthropic")
        async with db_session_factory() as session:
            responder = await get_binding(
                session,
                tenant_id=tenant.id,
                platform=platform,
                parent_channel_id="channel",
                thread_id="thread",
            )
            assert responder is not None
            assert responder.responder_ma_agent_id == (
                "agt_research" if retained == "anthropic" else "sh_agent_original"
            )
            # A stale button must never leave a responder inconsistent with its provider binding.
            assert (
                await mux_state.get_binding(
                    session, Slot(thread=binding.thread, account_id=binding.legacy_account_id)
                )
                == binding
            )
        now = datetime.now(UTC)
        request = ContinuationRequest(
            tenant_id=tenant.id,
            platform=platform,
            parent_channel_id="channel",
            thread_id="thread",
            requester_account_id=account.id,
            requester_external_user_id="caller",
            target_ma_agent_id="agt_research",
            target_name="research",
            requested_work="Check build",
            reason="timer",
            idempotency_key=uuid4(),
        )
        await enqueue_wake(db_session_factory, request, available_at=now - timedelta(seconds=1))
        seeds: list[str] = []

        async def run(_row: TaskContinuationRow, seed: str) -> None:
            assert not anthropic_disabled()
            seeds.append(seed)

        await dispatch_pending_continuations(
            db_session_factory,
            client,
            tenant_id=tenant.id,
            platform=platform,
            thread_id="thread",
            run_follow_up=run,
            post_notice=AsyncMock(),
            latest_user_message_at=AsyncMock(return_value=None),
            now=lambda: now,
        )
        async with db_session_factory() as session:
            after = await get_continuation(session, idempotency_key=request.idempotency_key)
        assert after is not None
        if retained == "anthropic":
            assert after.status == "delivered"
            assert after.skip_reason is None
            assert len(seeds) == 1 and "Check build" in seeds[0]
            assert len(requests) == 2, "handover and timer both resolve the retained provider"
        else:
            assert after.status == "pending"
            assert after.skip_reason is None
            assert after.attempts == 0 and after.started_at is None
            assert after.lease_owner is None
            assert not seeds and not requests
        # A guard refusal is never evidence that a destination was deleted.
        if retained == "selfhosted":
            with without_anthropic(), pytest.raises(AnthropicDisabled):
                await decide_continuation(
                    db_session_factory,
                    client,
                    request=request,
                    now=now,
                    latest_user_message_at=None,
                    active_turn=False,
                )
    finally:
        if retained == "anthropic":
            await client.close()
    assert not anthropic_disabled()
