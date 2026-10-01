"""Turn-context row lifecycle around ``run_turn`` in ``_run_thread_turn``.

Drives the real ``_run_thread_turn`` via ``SlackApp._orchestrate`` (the same
harness the Task 3 orchestration tests in ``test_app.py`` use) with a stubbed
``run_turn`` that records DB state — verifying a live
``slack_turn_contexts`` row exists exactly while ``run_turn`` executes, and
is gone afterward whether the turn succeeds or raises.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import TurnError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.slack_turn_contexts import get_slack_turn_channels
from daimon.core.stores.turn_origins import get_active_origin
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing import ma_session, ma_session_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_orchestrate_app

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _fake_ma_session(*, session_id: str, agent_id: str, environment_id: str) -> Any:
    return ma_session(
        id=session_id,
        agent=ma_session_agent(id=agent_id),
        environment_id=environment_id,
    )


async def test_turn_context_row_lives_exactly_during_run_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The turn-context row must be visible to a reader while run_turn executes,
    and gone once the turn (and its finally block) completes."""
    team_id = "T_TURN_CTX_LIVE"
    channel = "C1"
    thread_ts = "1000000000.000001"
    event_ts = thread_ts
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    await provision_tenant(
        db_session_factory, platform="slack", workspace_id=team_id, signup_credit=Decimal("10")
    )
    async with db_session_factory() as s:
        principal = await get_or_create_platform_principal(
            s, tenant_id=tenant_id, platform="slack", external_id="U_TURN_CTX"
        )
        await s.commit()

    app, _ = make_orchestrate_app(db_session_factory)

    seen: list[frozenset[str]] = []
    origin_ids: list[uuid.UUID] = []

    async def fake_run_turn(**kwargs: object) -> TurnState:
        message = str(kwargs["user_message"])
        assert message.startswith("<turn_controls>"), (
            "trusted controls must supplement platform history"
        )
        controls = json.loads(message.splitlines()[1])
        origin_id = uuid.UUID(controls["origin_context_id"])
        origin_ids.append(origin_id)
        assert controls["parent_channel_id"] == channel, "controls carry parent location"
        assert controls["thread_id"] == thread_ts, "controls carry exact thread"
        assert controls["current_role"] == "user", "controls carry verified current role"
        assert controls["responder"]["ma_agent_id"] == "agent_turn_ctx_id", (
            "controls carry concrete responder"
        )
        assert controls["responder"]["handle"] == "@daimon", (
            "controls carry the handle people mention beside the agent name, so an operator "
            "renaming the bot account is never read as a second agent"
        )
        async with db_session_factory() as s:
            origin = await get_active_origin(
                s,
                origin_id=origin_id,
                tenant_id=tenant_id,
                account_id=principal.account_id,
                platform="slack",
                now=datetime.now(UTC),
            )
            assert origin is not None, "origin must be committed while the turn runs"
            seen.append(
                await get_slack_turn_channels(
                    s, tenant_id=tenant_id, account_id=principal.account_id, cutoff=EPOCH
                )
            )
        state = TurnState(content=[TextBlock(kind="text", text="hi")])
        lifecycle = kwargs["lifecycle"]
        await lifecycle.on_terminal_success(state)  # type: ignore[attr-defined]
        return state

    event: dict[str, Any] = {
        "type": "app_mention",
        "ts": thread_ts,
        "event_ts": event_ts,
        "channel": channel,
        "user": "U_TURN_CTX",
        "text": "<@U_BOT> hello",
    }

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.prepare.create_session", new_callable=AsyncMock
        ) as mock_create_session,
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as mock_run_turn,
    ):
        mock_resolve_agent.return_value = "agent_turn_ctx_id"
        mock_resolve_env.return_value = "env_turn_ctx_id"
        mock_create_session.return_value = _fake_ma_session(
            session_id="sess-turn-ctx-001",
            agent_id="agent_turn_ctx_id",
            environment_id="env_turn_ctx_id",
        )
        mock_run_turn.side_effect = fake_run_turn

        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event,
            team_id=team_id,
            channel=channel,
            event_ts=event_ts,
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
        )

    assert seen == [frozenset({"C1"})], "row must be visible while run_turn executes"

    async with db_session_factory() as s:
        after = await get_slack_turn_channels(
            s, tenant_id=tenant_id, account_id=principal.account_id, cutoff=EPOCH
        )
    assert after == frozenset(), "row must be deleted in finally"

    async with db_session_factory() as session:
        for origin_id in origin_ids:
            origin = await get_active_origin(
                session,
                origin_id=origin_id,
                tenant_id=tenant_id,
                account_id=principal.account_id,
                platform="slack",
                now=datetime.now(UTC),
            )
            assert origin is None, "turn must delete its trusted origin after execution"


async def test_turn_context_row_deleted_when_run_turn_raises(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The turn-context row must be deleted even when run_turn raises, and the
    error must still propagate to the caller."""
    team_id = "T_TURN_CTX_RAISE"
    channel = "C1"
    thread_ts = "1000000000.000002"
    event_ts = thread_ts
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    await provision_tenant(
        db_session_factory, platform="slack", workspace_id=team_id, signup_credit=Decimal("10")
    )
    async with db_session_factory() as s:
        principal = await get_or_create_platform_principal(
            s, tenant_id=tenant_id, platform="slack", external_id="U_TURN_CTX_RAISE"
        )
        await s.commit()

    app, _ = make_orchestrate_app(db_session_factory)

    async def fake_run_turn(**kwargs: object) -> TurnState:
        raise TurnError(kind="upstream", message="boom")

    event: dict[str, Any] = {
        "type": "app_mention",
        "ts": thread_ts,
        "event_ts": event_ts,
        "channel": channel,
        "user": "U_TURN_CTX_RAISE",
        "text": "<@U_BOT> hello",
    }

    with (
        patch(
            "daimon.core.turn.admission.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.core.turn.admission.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.core.turn.prepare.create_session", new_callable=AsyncMock
        ) as mock_create_session,
        patch("daimon.core.turn.run.run_turn", new_callable=AsyncMock) as mock_run_turn,
    ):
        mock_resolve_agent.return_value = "agent_turn_ctx_raise_id"
        mock_resolve_env.return_value = "env_turn_ctx_raise_id"
        mock_create_session.return_value = _fake_ma_session(
            session_id="sess-turn-ctx-raise-001",
            agent_id="agent_turn_ctx_raise_id",
            environment_id="env_turn_ctx_raise_id",
        )
        mock_run_turn.side_effect = fake_run_turn

        with pytest.raises(TurnError):
            await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
                event,
                team_id=team_id,
                channel=channel,
                event_ts=event_ts,
                web_client=fake_slack_web_client.client,
                tenant_id=tenant_id,
            )

    async with db_session_factory() as s:
        after = await get_slack_turn_channels(
            s, tenant_id=tenant_id, account_id=principal.account_id, cutoff=EPOCH
        )
    assert after == frozenset(), "row must be deleted in finally even when run_turn raises"
