"""Turn-context row lifecycle around ``run_turn`` in ``_run_thread_turn``.

Drives the real ``_run_thread_turn`` via ``SlackApp._orchestrate`` (the same
harness the Task 3 orchestration tests in ``test_app.py`` use) with a stubbed
``run_turn`` that records DB state — verifying a live
``slack_turn_contexts`` row exists exactly while ``run_turn`` executes, and
is gone afterward whether the turn succeeds or raises.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsModelConfig, BetaManagedAgentsSession
from anthropic.types.beta import BetaManagedAgentsSessionAgent as _SessionAgent
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import TurnError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.slack_turn_contexts import get_slack_turn_channels
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.ma import (
    _agent_response as _agent_response,  # pyright: ignore[reportPrivateUsage]  # test-only
)
from daimon.testing.ma import (
    _environment_response as _environment_response,  # pyright: ignore[reportPrivateUsage]  # test-only
)
from daimon.testing.ma import build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _make_agent_env_handler() -> Any:
    """Minimal httpx.MockTransport handler for MA agent/environment retrieves.

    Mirrors ``_make_agent_env_handler`` in ``test_app.py`` — handles
    GET /v1/agents/{id} and GET /v1/environments/{id}, the two endpoints
    ``_run_thread_turn`` calls when creating a new MA session.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        m = re.match(r"^/v1/agents/(?P<id>[^/]+)$", path)
        if m and request.method == "GET":
            return httpx.Response(200, json=_agent_response(agent_id=m.group("id")))
        m = re.match(r"^/v1/environments/(?P<id>[^/]+)$", path)
        if m and request.method == "GET":
            env = _environment_response(environment_id=m.group("id"))
            return httpx.Response(200, json=env.model_dump(mode="json"))
        raise AssertionError(f"_make_agent_env_handler: unhandled {request.method} {path}")

    return handler


def _make_orchestrate_app(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> SlackApp:
    """Build a SlackApp with a real sessionmaker and a fake MA transport.

    Mirrors ``_make_orchestrate_app`` in ``test_app.py``.
    """
    settings = MagicMock()
    settings.crypto.keys = ()
    settings.slack.max_concurrent_turns_per_tenant = 3
    settings.mcp.public_url = None
    # app_root_url=None short-circuits _maybe_post_connect_nudge (Task 11) — these
    # turn-context tests don't exercise the connect-nudge flow.
    settings.mcp.app_root_url = None
    settings.defaults_root = MagicMock()

    runtime = SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(_make_agent_env_handler()),
        sessionmaker=sessionmaker,
        http_client=MagicMock(spec=httpx.AsyncClient),
        deployment_default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    return SlackApp(runtime=runtime)


def _fake_ma_session(*, session_id: str, agent_id: str, environment_id: str) -> Any:
    now = datetime.now(UTC)
    agent_snapshot = _SessionAgent(
        id=agent_id,
        mcp_servers=[],
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        name="test-agent",
        skills=[],
        tools=[],
        type="agent",
        version=1,
    )
    return BetaManagedAgentsSession(
        outcome_evaluations=[],
        id=session_id,
        agent=agent_snapshot,
        created_at=now,
        environment_id=environment_id,
        metadata={},
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        type="session",
        updated_at=now,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
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

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    async with db_session_factory() as s:
        principal = await get_or_create_platform_principal(
            s, tenant_id=tenant_id, platform="slack", external_id="U_TURN_CTX"
        )
        await s.commit()

    app = _make_orchestrate_app(db_session_factory)

    seen: list[frozenset[str]] = []

    async def fake_run_turn(**kwargs: object) -> TurnState:
        async with db_session_factory() as s:
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
            "daimon.adapters.slack.app.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.adapters.slack.app.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.adapters.slack.app.create_session", new_callable=AsyncMock
        ) as mock_create_session,
        patch("daimon.adapters.slack.app.run_turn", new_callable=AsyncMock) as mock_run_turn,
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

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    async with db_session_factory() as s:
        principal = await get_or_create_platform_principal(
            s, tenant_id=tenant_id, platform="slack", external_id="U_TURN_CTX_RAISE"
        )
        await s.commit()

    app = _make_orchestrate_app(db_session_factory)

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
            "daimon.adapters.slack.app.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch(
            "daimon.adapters.slack.app.resolve_environment", new_callable=AsyncMock
        ) as mock_resolve_env,
        patch(
            "daimon.adapters.slack.app.create_session", new_callable=AsyncMock
        ) as mock_create_session,
        patch("daimon.adapters.slack.app.run_turn", new_callable=AsyncMock) as mock_run_turn,
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
