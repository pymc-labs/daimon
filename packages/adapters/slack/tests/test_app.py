"""Tests for SlackApp (Phase 80, STURN-01..05).

Covers:
- Task 1: ack-first dispatch, teardown routing, drain_and_close
- Task 2: dedup gate, per-event token resolve, Slack Connect rejection,
          structural per_event_client assertion
- Task 3 (Plan 06): orchestration — session continuity, watermark, ⌛/coalesce,
          tenant cap
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import re
import re as _re
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import httpx
from anthropic import AsyncAnthropic
from anthropic.types.beta import (
    BetaManagedAgentsModelConfig,
    BetaManagedAgentsSession,
    BetaManagedAgentsSessionAgent,
)
from anthropic.types.beta.beta_managed_agents_session_stats import BetaManagedAgentsSessionStats
from anthropic.types.beta.beta_managed_agents_session_usage import BetaManagedAgentsSessionUsage
from cryptography.fernet import Fernet
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core._models import Tenant
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.slack_bot_tokens import get_slack_bot_token, upsert_slack_bot_token
from daimon.core.stores.thread_sessions import create_thread_session, get_live_thread_session
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.ma import (
    _agent_response as _agent_response,  # pyright: ignore[reportPrivateUsage]  # test-only
)
from daimon.testing.ma import (
    _environment_response as _environment_response,  # pyright: ignore[reportPrivateUsage]  # test-only
)
from daimon.testing.ma import build_fake_anthropic
from pydantic import SecretStr
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from yarl import URL

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_APP_PY_PATH = pathlib.Path(__file__).parent.parent / "daimon/adapters/slack/app.py"


def _make_agent_env_handler() -> Callable[[httpx.Request], httpx.Response]:
    """Minimal httpx.MockTransport handler for MA agent/environment retrieves.

    Handles GET /v1/agents/{id} and GET /v1/environments/{id} — the two
    endpoints called by _run_thread_turn when creating a new MA session.
    Raises AssertionError for any other path so unexpected calls are visible.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        m = _re.match(r"^/v1/agents/(?P<id>[^/]+)$", path)
        if m and request.method == "GET":
            return httpx.Response(200, json=_agent_response(agent_id=m.group("id")))
        m = _re.match(r"^/v1/environments/(?P<id>[^/]+)$", path)
        if m and request.method == "GET":
            env = _environment_response(environment_id=m.group("id"))
            return httpx.Response(200, json=env.model_dump(mode="json"))
        raise AssertionError(f"_make_agent_env_handler: unhandled {request.method} {path}")

    return handler


@dataclasses.dataclass
class _FakeSocketClient:
    """Minimal Socket Mode client fake — records call order and ack payloads."""

    call_log: list[str] = dataclasses.field(default_factory=list[str])
    # Ordered list of SocketModeResponse objects sent via send_socket_mode_response.
    # Tests can inspect .sent_responses[0].payload to assert ack payload content.
    sent_responses: list[SocketModeResponse] = dataclasses.field(
        default_factory=list[SocketModeResponse]
    )

    async def send_socket_mode_response(self, response: SocketModeResponse) -> None:
        self.call_log.append("send_socket_mode_response")
        self.sent_responses.append(response)

    async def close(self) -> None:
        self.call_log.append("close")


def _make_events_api_request(
    *,
    event_type: str,
    team_id: str = "T_TEST",
    channel: str = "C_TEST",
    event_ts: str = "1000000000.000001",
    user: str = "U_TEST",
    user_team: str | None = None,
    extra_event: dict[str, Any] | None = None,
) -> SocketModeRequest:
    """Build a minimal events_api SocketModeRequest."""
    event: dict[str, Any] = {
        "type": event_type,
        "channel": channel,
        "event_ts": event_ts,
        "ts": event_ts,
        "user": user,
        "text": "<@U_BOT> hello",
    }
    if user_team is not None:
        event["user_team"] = user_team
    if extra_event:
        event.update(extra_event)
    return SocketModeRequest(
        type="events_api",
        envelope_id="env_test_001",
        payload={"team_id": team_id, "event": event},
    )


def _make_app(
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    *,
    crypto_key: str | None = None,
) -> SlackApp:
    """Build a SlackApp with injected dependencies for testing."""
    settings = MagicMock()
    if crypto_key is not None:
        settings.crypto.keys = (SecretStr(crypto_key),)
    else:
        settings.crypto.keys = ()
    settings.slack.max_concurrent_turns_per_tenant = 3

    if sessionmaker is None:
        # Provide a factory that returns an AsyncMock context manager when
        # no real DB is needed (ack-order / drain tests).
        sessionmaker = MagicMock()

    runtime = SlackRuntime(
        settings=settings,
        anthropic=MagicMock(spec=AsyncAnthropic),
        sessionmaker=sessionmaker,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )
    return SlackApp(runtime=runtime)


# ---------------------------------------------------------------------------
# Task 1 — ack-first, teardown, drain
# ---------------------------------------------------------------------------


async def test_on_request_ack_first_when_events_api_request_sends_ack_before_spawn() -> None:
    """send_socket_mode_response must be the first awaited call in on_request.

    The ack deadline is 3 s; any I/O before ack triggers a Slack retry and
    produces duplicate turns (STURN-01 Pitfall 1).
    """
    fake_client = _FakeSocketClient()
    app = _make_app()

    # Replace _handle_app_mention with a sentinel that records when it STARTS.
    handle_started: list[str] = []

    async def _sentinel_handle(event: dict[str, Any], *, team_id: str) -> None:
        handle_started.append("handle_app_mention_started")

    app._handle_app_mention = _sentinel_handle  # type: ignore[method-assign]

    req = _make_events_api_request(event_type="app_mention")
    await app.on_request(fake_client, req)  # type: ignore[arg-type]

    # The call log must have the ack first — before the background task ran.
    assert "send_socket_mode_response" in fake_client.call_log, (
        "on_request must call send_socket_mode_response"
    )
    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "send_socket_mode_response must be the FIRST entry in call_log"
    )

    # Drain spawned tasks so the test loop is clean.
    pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_on_request_ignores_non_events_api_type_when_hello_arrives() -> None:
    """on_request returns immediately for non events_api types (e.g. hello)."""
    fake_client = _FakeSocketClient()
    app = _make_app()

    spawned: list[str] = []
    original_spawn = app._spawn  # pyright: ignore[reportPrivateUsage]

    def _spy_spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        spawned.append("spawned")
        return original_spawn(coro)

    app._spawn = _spy_spawn  # type: ignore[method-assign]

    req = SocketModeRequest(
        type="hello",
        envelope_id="env_hello",
        payload={"type": "hello"},
    )
    await app.on_request(fake_client, req)  # type: ignore[arg-type]

    assert not spawned, "non events_api request must not spawn any background task"


async def test_handle_teardown_when_app_uninstalled_archives_tenant_and_deletes_token(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """_handle_teardown routes to teardown_slack_install, which archives the
    tenant and deletes the bot-token row."""
    team_id = "T_APP_80_TEARDOWN"

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=b"fake-encrypted-token",
    )
    await db_session.flush()

    # Verify precondition: token row exists.
    pre_token = await get_slack_bot_token(db_session, team_id=team_id)
    assert pre_token is not None, "token row must exist before teardown"

    app = _make_app(db_session_factory)
    await app._handle_teardown(team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    # Token row must be gone.
    post_token = await get_slack_bot_token(db_session, team_id=team_id)
    assert post_token is None, (
        "_handle_teardown must delete the slack_bot_tokens row via teardown_slack_install"
    )

    # Tenant must be soft-archived.
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    tenant_row = (
        await db_session.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one()
    assert tenant_row.archived_at is not None, (
        "_handle_teardown must archive the tenant via teardown_slack_install"
    )


async def test_drain_and_close_flips_draining_and_calls_client_close() -> None:
    """drain_and_close sets draining=True and awaits client.close()."""
    fake_client = _FakeSocketClient()
    app = _make_app()

    assert app.draining is False, "draining must start False"

    await app.drain_and_close(fake_client)  # type: ignore[arg-type]

    assert app.draining is True, "drain_and_close must set draining=True"
    assert "close" in fake_client.call_log, "drain_and_close must await client.close()"


async def test_drain_and_close_waits_for_in_flight_tasks_before_closing() -> None:
    """drain_and_close polls _processing until empty, then closes."""
    fake_client = _FakeSocketClient()
    app = _make_app()

    thread_ts = "1000000000.000002"
    app._processing.add(thread_ts)  # pyright: ignore[reportPrivateUsage]

    # Release the in-flight slot after a short delay so drain can proceed.
    async def _release() -> None:
        await asyncio.sleep(0.05)
        app._processing.discard(thread_ts)  # pyright: ignore[reportPrivateUsage]

    asyncio.create_task(_release())
    await app.drain_and_close(fake_client)  # type: ignore[arg-type]

    assert not app._processing, "drain must poll until _processing is empty"  # pyright: ignore[reportPrivateUsage]
    assert "close" in fake_client.call_log, "drain_and_close must call client.close() after drain"


# ---------------------------------------------------------------------------
# Task 2 — dedup, per-event token, Slack Connect
# ---------------------------------------------------------------------------


async def test_handle_app_mention_dedup_when_same_event_ts_drops_second_invocation(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Second invocation with the same (team_id, channel, event_ts) must be
    dropped by the dedup gate without reaching the orchestration seam."""
    team_id = "T_APP_80_DEDUP"
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(fernet, "xoxb-dedup-test"),
    )
    await db_session.flush()

    app = _make_app(db_session_factory, crypto_key=fernet_key)

    orchestrate_calls: list[dict[str, Any]] = []

    async def _spy_orchestrate(
        event: dict[str, Any],
        *,
        team_id: str,
        channel: str,
        event_ts: str,
        web_client: Any,
        tenant_id: uuid.UUID,
    ) -> None:
        orchestrate_calls.append({"event_ts": event_ts})

    app._orchestrate = _spy_orchestrate  # type: ignore[method-assign]

    event: dict[str, Any] = {
        "type": "app_mention",
        "channel": "C_TEST",
        "event_ts": "1000000001.000001",
        "ts": "1000000001.000001",
        "user": "U_TEST",
        "text": "<@U_BOT> hello",
    }

    # First invocation — must reach orchestrate.
    await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]
    assert len(orchestrate_calls) == 1, "first invocation must reach the orchestration seam"

    # Second invocation — dedup gate must drop it.
    await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]
    assert len(orchestrate_calls) == 1, (
        "second invocation with the same event_ts must be dropped by the dedup gate"
    )


async def test_handle_app_mention_no_token_when_no_token_row_drops(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When no slack_bot_tokens row exists for the team, the event is dropped
    without reaching the orchestration seam."""
    team_id = "T_APP_80_NO_TOKEN"

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    # Intentionally no upsert_slack_bot_token.

    app = _make_app(db_session_factory)

    orchestrate_calls: list[dict[str, Any]] = []

    async def _spy_orchestrate(
        event: dict[str, Any],
        *,
        team_id: str,
        channel: str,
        event_ts: str,
        web_client: Any,
        tenant_id: uuid.UUID,
    ) -> None:
        orchestrate_calls.append({"team_id": team_id})

    app._orchestrate = _spy_orchestrate  # type: ignore[method-assign]

    event: dict[str, Any] = {
        "type": "app_mention",
        "channel": "C_TEST",
        "event_ts": "1000000002.000001",
        "ts": "1000000002.000001",
        "user": "U_TEST",
        "text": "<@U_BOT> hello",
    }

    await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    assert len(orchestrate_calls) == 0, (
        "event must be dropped when no token row exists — token-existence is the "
        "tenant liveness signal (STURN-03)"
    )


async def test_handle_app_mention_slack_connect_external_when_external_user_posts_ephemeral(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """When the event sender belongs to a different Slack workspace (Slack Connect),
    a chat_postEphemeral rejection is sent and the orchestration seam is NOT reached.

    ``fake_slack_web_client`` activates an aioresponses context that intercepts ALL
    aiohttp requests made by any AsyncWebClient during this test, including the
    per-event client constructed inside ``_handle_app_mention``.
    """
    from yarl import URL

    team_id = "T_APP_80_CONNECT"
    external_team = "T_EXTERNAL"
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    await upsert_slack_bot_token(
        db_session,
        team_id=team_id,
        encrypted_token=encrypt_token(fernet, "xoxb-connect-test"),
    )
    await db_session.flush()

    app = _make_app(db_session_factory, crypto_key=fernet_key)

    orchestrate_calls: list[dict[str, Any]] = []

    async def _spy_orchestrate(
        event: dict[str, Any],
        *,
        team_id: str,
        channel: str,
        event_ts: str,
        web_client: Any,
        tenant_id: uuid.UUID,
    ) -> None:
        orchestrate_calls.append({"team_id": team_id})

    app._orchestrate = _spy_orchestrate  # type: ignore[method-assign]

    event: dict[str, Any] = {
        "type": "app_mention",
        "channel": "C_TEST",
        "event_ts": "1000000003.000001",
        "ts": "1000000003.000001",
        "user": "U_EXTERNAL",
        "user_team": external_team,  # Slack Connect: user is from a different team
        "text": "<@U_BOT> hello",
    }

    # The fake_slack_web_client fixture's aioresponses context already intercepts
    # all aiohttp calls (including those from a freshly constructed AsyncWebClient).
    # chat.postEphemeral is pre-registered as ok=True by _register_slack_defaults.
    mock = fake_slack_web_client.mock  # type: ignore[union-attr]
    await app._handle_app_mention(event, team_id=team_id)  # pyright: ignore[reportPrivateUsage]

    assert len(orchestrate_calls) == 0, (
        "Slack Connect external event must be dropped — orchestration must not run"
    )

    # Verify chat_postEphemeral was called against the intercepted transport.
    ephemeral_calls = [
        req
        for (_, url), reqs in mock.requests.items()
        if url == URL("https://slack.com/api/chat.postEphemeral")
        for req in reqs
    ]
    assert len(ephemeral_calls) == 1, (
        "chat_postEphemeral must be called exactly once for a Slack Connect external event"
    )


def test_per_event_client_never_assigned_to_self_or_runtime() -> None:
    """AsyncWebClient must only be constructed inside handler functions, never
    cached on self, runtime, or at module level.

    Caching a client across events causes cross-workspace token leakage (T-80-01).
    This structural test parses app.py and asserts the assignment pattern is absent.
    """
    source = _APP_PY_PATH.read_text()

    # Detect any assignment of AsyncWebClient to self.* or class-level attributes.
    bad_pattern = re.compile(r"(?:self\.\w+|runtime\.\w+)\s*=\s*AsyncWebClient\b")
    matches = bad_pattern.findall(source)
    assert not matches, f"AsyncWebClient must not be cached on self or runtime: {matches}"

    # Confirm at least one per-event construction exists (sanity check).
    assert "AsyncWebClient(" in source, (
        "app.py must contain at least one AsyncWebClient( construction"
    )


# ---------------------------------------------------------------------------
# Task 3 (Plan 06) — orchestration: session continuity, watermark, ⌛/coalesce, cap
# ---------------------------------------------------------------------------


def _make_orchestrate_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    max_concurrent_turns_per_tenant: int = 3,
    deployment_default: DeploymentDefault | None = None,
) -> tuple[SlackApp, AsyncAnthropic]:
    """Build a SlackApp for orchestration tests.

    Returns (app, anthropic_client). The anthropic client is a real
    AsyncAnthropic backed by a httpx.MockTransport that handles
    GET /v1/agents/{id} and GET /v1/environments/{id} — the two MA endpoints
    called when creating a new session in _run_thread_turn.

    ``deployment_default`` defaults to the seeded defaults/config.yaml values
    (agent "daimon", environment "default") so tests without scoped rows
    resolve the same tags a fresh deployment would.
    """
    settings = MagicMock()
    settings.crypto.keys = ()
    settings.slack.max_concurrent_turns_per_tenant = max_concurrent_turns_per_tenant
    settings.mcp.public_url = None
    # app_root_url=None short-circuits _maybe_post_connect_nudge (Task 11) — these
    # orchestration tests don't exercise the connect-nudge flow.
    settings.mcp.app_root_url = None
    settings.defaults_root = MagicMock()

    anthropic_client = build_fake_anthropic(_make_agent_env_handler())

    runtime = SlackRuntime(
        settings=settings,
        anthropic=anthropic_client,
        sessionmaker=sessionmaker,
        http_client=MagicMock(spec=httpx.AsyncClient),
        deployment_default=(
            deployment_default
            if deployment_default is not None
            else DeploymentDefault(agent_name="daimon", environment_name="default")
        ),
    )
    return SlackApp(runtime=runtime), anthropic_client


async def test_orchestrate_first_turn_when_new_thread_creates_session_row_and_writes_watermark(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """first_turn_session: a thread_sessions row (platform="slack") exists after
    the turn and its watermark_message_id equals lifecycle.final_ts.

    Verifies STURN-05: session persistence + watermark.
    """
    from daimon.core.stores.identity import get_or_create_platform_principal

    team_id = "T_ORCH_80_FIRST"
    channel = "C_TEST"
    thread_ts = "9000000001.000001"
    event_ts = thread_ts
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    # Pre-create the principal so account_id is known for the row verification query.
    # _orchestrate uses external_id=str(event["user"]) = "U_TEST_FIRST".
    async with db_session_factory() as s:
        pre_principal = await get_or_create_platform_principal(
            s, tenant_id=tenant_id, platform="slack", external_id="U_TEST_FIRST"
        )
        await s.commit()

    app, _ = _make_orchestrate_app(db_session_factory)

    # fake_run_turn calls lifecycle.on_terminal_success so final_ts is set.
    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text="Hello!")])
        await lifecycle.on_terminal_success(state)
        return state

    event: dict[str, Any] = {
        "type": "app_mention",
        "ts": thread_ts,
        "event_ts": event_ts,
        "channel": channel,
        "user": "U_TEST_FIRST",
        "text": "<@U_BOT> hello",
    }

    # Build a real BetaManagedAgentsSession inline — no MagicMock shortcuts so
    # ma_session_id carries a real string id, not a mock attribute.
    _now = datetime.now(UTC)
    _agent_snapshot = BetaManagedAgentsSessionAgent(
        id="agent_test_id",
        mcp_servers=[],
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        name="test-agent",
        skills=[],
        tools=[],
        type="agent",
        version=1,
    )
    _fake_session = BetaManagedAgentsSession(
        id="sess-first-001",
        agent=_agent_snapshot,
        created_at=_now,
        environment_id="env_test_id",
        metadata={},
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        type="session",
        updated_at=_now,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
    )

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
        mock_resolve_agent.return_value = "agent_test_id"
        mock_resolve_env.return_value = "env_test_id"
        mock_create_session.return_value = _fake_session
        mock_run_turn.side_effect = _fake_run_turn

        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event,
            team_id=team_id,
            channel=channel,
            event_ts=event_ts,
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
        )

        # The environment must resolve the seeded "default" tag (defaults/config.yaml),
        # not "production" — the latter has no matching resource on a fresh tenant, so
        # the turn dies with MAResolverMissError and the user gets no reply.
        assert mock_resolve_env.await_args is not None, "resolve_environment must be called"
        assert mock_resolve_env.await_args.kwargs["daimon_tag"] == "default", (
            "first turn must resolve the seeded 'default' environment tag"
        )

        # create_session must receive mcp_settings + agent_uuid so the per-agent
        # daimon-mcp vault credential is provisioned. Without them the MA session
        # has no MCP credential and every tool call fails with
        # "no credential is stored for this server URL".
        assert mock_create_session.await_args is not None, "create_session must be called"
        cs_kwargs = mock_create_session.await_args.kwargs
        assert cs_kwargs.get("mcp_settings") is not None, (
            "create_session must receive mcp_settings to bootstrap the MCP vault credential"
        )
        assert cs_kwargs.get("agent_uuid") is not None, (
            "create_session must receive agent_uuid (per-agent vault key)"
        )

    # Assert thread_sessions row was created with platform="slack".
    async with db_session_factory() as s:
        row = await get_live_thread_session(
            s,
            tenant_id=tenant_id,
            platform="slack",
            thread_id=thread_ts,
            account_id=pre_principal.account_id,
        )

    assert row is not None, "thread_sessions row must exist after first turn"
    assert row.platform == "slack", "platform must be 'slack'"
    assert row.ma_session_id == "sess-first-001", (
        "ma_session_id must match the session created by create_session"
    )
    # The watermark comes from lifecycle.final_ts which is the ts from chat.postMessage.
    # fake_slack_web_client registers chat.postMessage → {"ts": "1000000000.000001"}.
    assert row.watermark_message_id == "1000000000.000001", (
        "watermark must equal lifecycle.final_ts from chat.postMessage (STURN-05)"
    )


async def test_orchestrate_continuation_when_live_session_exists_calls_build_delta_xml(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """continuation_delta: a second mention in the same thread reuses the MA session
    and replays via build_delta_xml(watermark_ts=<prior watermark>), then updates
    the watermark.
    """
    from daimon.core.stores.identity import get_or_create_platform_principal

    team_id = "T_ORCH_80_CONT"
    channel = "C_TEST"
    thread_ts = "9000000002.000001"
    prior_watermark = "9000000002.000002"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    # Pre-create the principal so account_id is known for both the seed row and the verify query.
    # _orchestrate uses external_id=str(event["user"]) = "U_TEST_CONT".
    async with db_session_factory() as s:
        cont_principal = await get_or_create_platform_principal(
            s, tenant_id=tenant_id, platform="slack", external_id="U_TEST_CONT"
        )
        await s.commit()

    # Pre-create a live thread_sessions row (as if a first turn already ran).
    async with db_session_factory() as s:
        await create_thread_session(
            s,
            tenant_id=tenant_id,
            platform="slack",
            thread_id=thread_ts,
            account_id=cont_principal.account_id,
            ma_session_id="sess-cont-existing",
            watermark_message_id=prior_watermark,
        )
        await s.commit()

    app, _ = _make_orchestrate_app(db_session_factory)

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text="Continuation!")])
        await lifecycle.on_terminal_success(state)
        return state

    event: dict[str, Any] = {
        "type": "app_mention",
        "ts": "9000000002.000003",
        "thread_ts": thread_ts,
        "event_ts": "9000000002.000003",
        "channel": channel,
        "user": "U_TEST_CONT",
        "text": "<@U_BOT> follow-up",
    }

    with (
        patch("daimon.adapters.slack.app.run_turn", new_callable=AsyncMock) as mock_run_turn,
        patch(
            "daimon.adapters.slack.app.build_delta_xml", new_callable=AsyncMock
        ) as mock_build_delta,
    ):
        mock_run_turn.side_effect = _fake_run_turn
        mock_build_delta.return_value = (
            "<context><thread_delta/></context>\n\n<user_query>follow-up</user_query>"
        )

        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event,
            team_id=team_id,
            channel=channel,
            event_ts="9000000002.000003",
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
        )

    # Assert build_delta_xml was called with the prior watermark.
    assert mock_build_delta.called, "build_delta_xml must be called for a continuation turn"
    call_kwargs = mock_build_delta.call_args.kwargs
    assert call_kwargs.get("watermark_ts") == prior_watermark, (
        f"build_delta_xml must be called with watermark_ts={prior_watermark!r}, "
        f"got {call_kwargs.get('watermark_ts')!r}"
    )

    # Assert watermark was updated in the DB.
    async with db_session_factory() as s:
        row = await get_live_thread_session(
            s,
            tenant_id=tenant_id,
            platform="slack",
            thread_id=thread_ts,
            account_id=cont_principal.account_id,
        )
    assert row is not None, "thread_sessions row must still exist"
    assert row.watermark_message_id != prior_watermark, (
        "watermark must be updated after the continuation turn"
    )


async def test_orchestrate_queue_coalesce_when_thread_in_flight_adds_hourglass_and_drains_to_one_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """queue_coalesce: a second mention while a turn is in flight adds a ⌛
    reaction and the queued mention drains into exactly one follow-up turn.

    Verifies STURN-06 / D-01 parity.
    """
    team_id = "T_ORCH_80_COALESCE"
    channel = "C_TEST"
    thread_ts = "9000000003.000001"
    event_ts1 = "9000000003.000001"
    event_ts2 = "9000000003.000002"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)

    app, _ = _make_orchestrate_app(db_session_factory)

    turn_count = 0
    # Gate that pauses the first run_turn call so event2 can arrive while task1
    # is in-flight.  The test sets this after sending event2.
    first_turn_gate = asyncio.Event()

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        nonlocal turn_count
        turn_count += 1
        if turn_count == 1:
            await first_turn_gate.wait()  # guaranteed suspension — event2 arrives here
        state = TurnState(content=[TextBlock(kind="text", text="Coalesce response")])
        await lifecycle.on_terminal_success(state)
        return state

    event1: dict[str, Any] = {
        "type": "app_mention",
        "ts": event_ts1,
        "event_ts": event_ts1,
        "channel": channel,
        "user": "U_TEST_A",
        "text": "<@U_BOT> first",
    }
    event2: dict[str, Any] = {
        "type": "app_mention",
        "ts": event_ts2,
        "thread_ts": thread_ts,
        "event_ts": event_ts2,
        "channel": channel,
        "user": "U_TEST_B",
        "text": "<@U_BOT> second",
    }

    # Patch all store functions so concurrent tasks don't share the single test connection.
    # The coalesce test verifies queue/drain logic, not DB correctness (that is tested
    # in test_orchestrate_first_turn_*).
    fake_principal = MagicMock()
    fake_principal.account_id = uuid.uuid4()
    fake_row = MagicMock()
    fake_row.id = uuid.uuid4()

    with (
        patch(
            "daimon.adapters.slack.app.get_or_create_platform_principal", new_callable=AsyncMock
        ) as mock_principal,
        patch(
            "daimon.adapters.slack.app.get_live_thread_session", new_callable=AsyncMock
        ) as mock_get_session,
        patch(
            "daimon.adapters.slack.app.create_thread_session", new_callable=AsyncMock
        ) as mock_create_ts,
        patch("daimon.adapters.slack.app.update_watermark", new_callable=AsyncMock),
        patch("daimon.adapters.slack.app.reconcile_tenant_defaults", new_callable=AsyncMock),
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
        mock_principal.return_value = fake_principal
        mock_get_session.return_value = None  # simulate new thread each time
        mock_create_ts.return_value = fake_row
        mock_resolve_agent.return_value = "agent_coalesce_id"
        mock_resolve_env.return_value = "env_coalesce_id"
        _now_c = datetime.now(UTC)
        _agent_snap_c = BetaManagedAgentsSessionAgent(
            id="agent_coalesce_id",
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
            name="test-agent",
            skills=[],
            tools=[],
            type="agent",
            version=1,
        )
        mock_create_session.return_value = BetaManagedAgentsSession(
            id="sess-coalesce-001",
            agent=_agent_snap_c,
            created_at=_now_c,
            environment_id="env_coalesce_id",
            metadata={},
            resources=[],
            stats=BetaManagedAgentsSessionStats(),
            status="idle",
            type="session",
            updated_at=_now_c,
            usage=BetaManagedAgentsSessionUsage(),
            vault_ids=[],
        )
        mock_run_turn.side_effect = _fake_run_turn

        # Start the first orchestrate as a background task.
        task1 = asyncio.create_task(
            app._orchestrate(  # pyright: ignore[reportPrivateUsage]
                event1,
                team_id=team_id,
                channel=channel,
                event_ts=event_ts1,
                web_client=fake_slack_web_client.client,
                tenant_id=tenant_id,
            )
        )
        # Yield to the event loop so task1 runs past _processing.add(thread_id)
        # and suspends inside run_turn at first_turn_gate.wait().
        await asyncio.sleep(0)

        # Second mention arrives while the first turn is in flight (task1 is
        # paused at first_turn_gate — thread_id is in _processing).
        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event2,
            team_id=team_id,
            channel=channel,
            event_ts=event_ts2,
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
        )

        # Release task1 to complete the first turn and drain event2.
        first_turn_gate.set()

        # Wait for task1 to complete (it also drains the queue).
        await task1

    # Assert ⌛ reaction was added.
    # reactions.add sends params as query string (url.path == "/api/reactions.add"),
    # so compare on path only — not the full URL which includes query params.
    reactions_calls = [
        req
        for (method, url), reqs in fake_slack_web_client.mock.requests.items()
        if method == "POST" and url.path == "/api/reactions.add"
        for req in reqs
    ]
    assert len(reactions_calls) >= 1, (
        "reactions_add must be called with 'hourglass_flowing_sand' for the queued mention"
    )

    # Assert exactly 2 turns ran: one for event1, one drain for event2.
    assert turn_count == 2, f"exactly 2 turns must run (1 initial + 1 drain), got {turn_count}"


async def test_orchestrate_first_mention_adds_eyes_reaction_before_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A first mention gets an immediate 👀 ack so cold-start isn't dead air."""
    team_id = "T_ORCH_80_EYES"
    channel = "C_TEST"
    thread_ts = "9000000005.000001"
    event_ts = thread_ts
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)

    app, _ = _make_orchestrate_app(db_session_factory)

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text="Hello!")])
        await lifecycle.on_terminal_success(state)
        return state

    event: dict[str, Any] = {
        "type": "app_mention",
        "ts": thread_ts,
        "event_ts": event_ts,
        "channel": channel,
        "user": "U_TEST_EYES",
        "text": "<@U_BOT> hello",
    }

    _now = datetime.now(UTC)
    _agent_snapshot = BetaManagedAgentsSessionAgent(
        id="agent_eyes_id",
        mcp_servers=[],
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        name="test-agent",
        skills=[],
        tools=[],
        type="agent",
        version=1,
    )
    _fake_session = BetaManagedAgentsSession(
        id="sess-eyes-001",
        agent=_agent_snapshot,
        created_at=_now,
        environment_id="env_eyes_id",
        metadata={},
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        type="session",
        updated_at=_now,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
    )

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
        mock_resolve_agent.return_value = "agent_eyes_id"
        mock_resolve_env.return_value = "env_eyes_id"
        mock_create_session.return_value = _fake_session
        mock_run_turn.side_effect = _fake_run_turn

        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event,
            team_id=team_id,
            channel=channel,
            event_ts=event_ts,
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
        )

    reactions_calls = [
        (method, url, kwargs)
        for (method, url), calls in fake_slack_web_client.mock.requests.items()
        if method == "POST" and url.path == "/api/reactions.add"
        for kwargs in calls
    ]
    assert any("eyes" in str(kwargs) for _, _, kwargs in reactions_calls), (
        "first mention must add the 👀 reaction before the turn runs"
    )


async def test_orchestrate_eyes_reaction_transport_error_does_not_leak_thread_slot(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A transport error (not a SlackApiError) on the 👀 ack must not leak state.

    The eyes reactions_add call now sits inside the try/finally that releases
    _processing and the tenant in-flight slot. A network blip on that call
    (aiohttp.ClientError / asyncio.TimeoutError, not just SlackApiError) must
    not leave the thread stuck in _processing forever.
    """
    team_id = "T_ORCH_80_EYES_XPORT"
    channel = "C_TEST"
    thread_ts = "9000000006.000001"
    event_ts = thread_ts
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)

    app, _ = _make_orchestrate_app(db_session_factory)

    # Override the shared fixture's repeat=True 200-OK reactions.add matcher:
    # aioresponses matches in registration order, so the fixture's default
    # (registered first) would otherwise always win. Clear and re-register
    # with the transport-error matcher first (consumed once, since it isn't
    # ``repeat``), then restore the rest of the defaults for any subsequent
    # calls the turn makes.
    reactions_add_pattern = re.compile(r"https://slack\.com/api/reactions\.add.*")
    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        reactions_add_pattern,
        exception=aiohttp.ClientOSError("connection reset"),
    )
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        reactions_add_pattern,
        payload={"ok": True},
        repeat=True,
    )
    for method in ("auth.test", "chat.postMessage", "chat.update", "chat.postEphemeral"):
        fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
            f"https://slack.com/api/{method}",
            payload={"ok": True, "ts": "1000000000.000001", "channel": "C_TEST"},
            repeat=True,
        )
    fake_slack_web_client.mock.get(  # pyright: ignore[reportUnknownMemberType]
        re.compile(r"https://slack\.com/api/conversations\.replies.*"),
        payload={"ok": True, "messages": [], "has_more": False},
        repeat=True,
    )
    fake_slack_web_client.mock.get(  # pyright: ignore[reportUnknownMemberType]
        re.compile(r"https://slack\.com/api/users\.info.*"),
        payload={
            "ok": True,
            "user": {
                "id": "U_TEST",
                "name": "tester",
                "is_admin": False,
                "is_owner": False,
                "is_primary_owner": False,
            },
        },
        repeat=True,
    )

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text="Hello!")])
        await lifecycle.on_terminal_success(state)
        return state

    event: dict[str, Any] = {
        "type": "app_mention",
        "ts": thread_ts,
        "event_ts": event_ts,
        "channel": channel,
        "user": "U_TEST_EYES_XPORT",
        "text": "<@U_BOT> hello",
    }

    _now = datetime.now(UTC)
    _agent_snapshot = BetaManagedAgentsSessionAgent(
        id="agent_eyes_xport_id",
        mcp_servers=[],
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        name="test-agent",
        skills=[],
        tools=[],
        type="agent",
        version=1,
    )
    _fake_session = BetaManagedAgentsSession(
        id="sess-eyes-xport-001",
        agent=_agent_snapshot,
        created_at=_now,
        environment_id="env_eyes_xport_id",
        metadata={},
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        type="session",
        updated_at=_now,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
    )

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
        mock_resolve_agent.return_value = "agent_eyes_xport_id"
        mock_resolve_env.return_value = "env_eyes_xport_id"
        mock_create_session.return_value = _fake_session
        mock_run_turn.side_effect = _fake_run_turn

        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event,
            team_id=team_id,
            channel=channel,
            event_ts=event_ts,
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
        )

    assert mock_run_turn.await_count == 1, (
        "the turn must still run despite the eyes reaction transport error"
    )
    assert thread_ts not in app._processing, (  # pyright: ignore[reportPrivateUsage]
        "thread slot must be released even when the eyes reaction raises a transport error"
    )
    assert tenant_id not in app._inflight, (  # pyright: ignore[reportPrivateUsage]
        "tenant in-flight slot must be released even when the eyes reaction raises a transport error"
    )


async def test_orchestrate_tenant_cap_when_exhausted_sends_ephemeral_and_skips_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """tenant_cap: when the per-tenant concurrency cap is exhausted, the adapter
    posts a chat_postEphemeral rejection and does NOT start a turn.

    Verifies STURN-06 / T-80-05.
    """
    team_id = "T_ORCH_80_CAP"
    channel = "C_TEST"
    event_ts = "9000000004.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    cap = 2

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)

    app, _ = _make_orchestrate_app(db_session_factory, max_concurrent_turns_per_tenant=cap)

    # Saturate the tenant in-flight count.
    app._inflight[tenant_id] = cap  # pyright: ignore[reportPrivateUsage]

    event: dict[str, Any] = {
        "type": "app_mention",
        "ts": event_ts,
        "event_ts": event_ts,
        "channel": channel,
        "user": "U_TEST_CAP",
        "text": "<@U_BOT> hello",
    }

    with patch("daimon.adapters.slack.app.run_turn", new_callable=AsyncMock) as mock_run_turn:
        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event,
            team_id=team_id,
            channel=channel,
            event_ts=event_ts,
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
        )

    # Assert chat_postEphemeral was called.
    ephemeral_url = URL("https://slack.com/api/chat.postEphemeral")
    ephemeral_calls = [
        req
        for (_, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == ephemeral_url
        for req in reqs
    ]
    assert len(ephemeral_calls) >= 1, (
        "chat_postEphemeral must be called when the per-tenant cap is exhausted"
    )

    # Assert run_turn was NOT called.
    mock_run_turn.assert_not_called()  # pyright: ignore[reportUnknownMemberType]


async def test_orchestrate_first_turn_when_channel_agent_propagated_resolves_channel_agent_tag(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A channel-scope agent_name written by /agent-setup propagation must be
    the daimon_tag resolved when a new thread session is created — not the
    hardcoded deployment default.
    """
    team_id = "T_ORCH_SCOPED_AGENT"
    channel = "C_SCOPED"
    thread_ts = "9000000010.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)
    # Simulate /agent-setup → "Set as default → This channel".
    async with db_session_factory() as s:
        await set_fields(
            s,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel),
            tenant_id=tenant_id,
            agent_name="marketing-bot",
            mode="agent",
        )
        await s.commit()

    app, _ = _make_orchestrate_app(db_session_factory)

    async def _fake_run_turn(*, lifecycle: Any, **kwargs: Any) -> TurnState:
        state = TurnState(content=[TextBlock(kind="text", text="Hello!")])
        await lifecycle.on_terminal_success(state)
        return state

    event: dict[str, Any] = {
        "type": "app_mention",
        "ts": thread_ts,
        "event_ts": thread_ts,
        "channel": channel,
        "user": "U_TEST_SCOPED",
        "text": "<@U_BOT> hello",
    }

    _now = datetime.now(UTC)
    _fake_session = BetaManagedAgentsSession(
        id="sess-scoped-001",
        agent=BetaManagedAgentsSessionAgent(
            id="agent_scoped_id",
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
            name="marketing-bot",
            skills=[],
            tools=[],
            type="agent",
            version=1,
        ),
        created_at=_now,
        environment_id="env_scoped_id",
        metadata={},
        resources=[],
        stats=BetaManagedAgentsSessionStats(),
        status="idle",
        type="session",
        updated_at=_now,
        usage=BetaManagedAgentsSessionUsage(),
        vault_ids=[],
    )

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
        mock_resolve_agent.return_value = "agent_scoped_id"
        mock_resolve_env.return_value = "env_scoped_id"
        mock_create_session.return_value = _fake_session
        mock_run_turn.side_effect = _fake_run_turn

        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event,
            team_id=team_id,
            channel=channel,
            event_ts=thread_ts,
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
        )

        assert mock_resolve_agent.await_args is not None, "resolve_agent must be called"
        assert mock_resolve_agent.await_args.kwargs["daimon_tag"] == "marketing-bot", (
            "new session must resolve the channel-propagated agent, not the deployment default"
        )
        # Environment has no channel/tenant override here → deployment default.
        assert mock_resolve_env.await_args is not None, "resolve_environment must be called"
        assert mock_resolve_env.await_args.kwargs["daimon_tag"] == "default", (
            "environment falls through to the deployment default when no scoped row sets it"
        )


async def test_orchestrate_first_turn_when_no_agent_configured_posts_guidance_and_skips_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """With no scoped rows and an empty deployment default, the turn must not
    start; the user gets a message pointing at /agent-setup instead.
    """
    team_id = "T_ORCH_NO_CONFIG"
    channel = "C_NO_CONFIG"
    thread_ts = "9000000011.000001"
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    await provision_tenant(db_session_factory, platform="slack", workspace_id=team_id)

    app, _ = _make_orchestrate_app(db_session_factory, deployment_default=DeploymentDefault())

    event: dict[str, Any] = {
        "type": "app_mention",
        "ts": thread_ts,
        "event_ts": thread_ts,
        "channel": channel,
        "user": "U_TEST_NO_CONFIG",
        "text": "<@U_BOT> hello",
    }

    with (
        patch(
            "daimon.adapters.slack.app.resolve_agent", new_callable=AsyncMock
        ) as mock_resolve_agent,
        patch("daimon.adapters.slack.app.run_turn", new_callable=AsyncMock) as mock_run_turn,
    ):
        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event,
            team_id=team_id,
            channel=channel,
            event_ts=thread_ts,
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
        )

        mock_resolve_agent.assert_not_called()  # pyright: ignore[reportUnknownMemberType]
        mock_run_turn.assert_not_called()  # pyright: ignore[reportUnknownMemberType]

    # A guidance message pointing at /agent-setup must have been posted.
    post_url = URL("https://slack.com/api/chat.postMessage")
    post_bodies = [
        req.kwargs.get("json") or json.loads(req.kwargs.get("data") or "{}")
        for (_, url), reqs in fake_slack_web_client.mock.requests.items()
        if url == post_url
        for req in reqs
    ]
    guidance = [b for b in post_bodies if "agent-setup" in str(b.get("text", ""))]
    assert guidance, (
        f"expected a chat.postMessage mentioning /agent-setup, got bodies: {post_bodies}"
    )


# ---------------------------------------------------------------------------
# Phase 81 Plan 05 — cancel registry: block_actions routing + author gate
# ---------------------------------------------------------------------------


def _make_block_actions_payload(
    *,
    action_id: str = "cancel_turn",
    message_ts: str = "1000000000.000001",
    user_id: str = "U_AUTHOR",
    team_id: str = "T_TEST",
    channel_id: str = "C_TEST",
) -> dict[str, Any]:
    """Build a minimal block_actions interactive payload inline (docs-grounded shape)."""
    return {
        "type": "block_actions",
        "actions": [{"action_id": action_id}],
        "container": {"message_ts": message_ts},
        "user": {"id": user_id},
        "team": {"id": team_id},
        "channel": {"id": channel_id},
    }


async def test_handle_block_action_when_author_clicks_cancel_sets_event() -> None:
    """cancel click from the turn author sets the cancel Event (D-01)."""
    app = _make_app()
    cancel = asyncio.Event()
    app._cancel_registry["1000000000.000001"] = (cancel, "U_AUTHOR")  # pyright: ignore[reportPrivateUsage]

    payload = _make_block_actions_payload(
        action_id="cancel_turn",
        message_ts="1000000000.000001",
        user_id="U_AUTHOR",
    )
    await app._handle_block_action(payload)  # pyright: ignore[reportPrivateUsage]

    assert cancel.is_set(), "cancel Event must be set when the turn author clicks cancel"


async def test_handle_block_action_when_non_author_clicks_cancel_event_unset() -> None:
    """cancel click from a non-author leaves the cancel Event unset (D-02 author gate)."""
    app = _make_app()
    cancel = asyncio.Event()
    app._cancel_registry["1000000000.000001"] = (cancel, "U_AUTHOR")  # pyright: ignore[reportPrivateUsage]

    payload = _make_block_actions_payload(
        action_id="cancel_turn",
        message_ts="1000000000.000001",
        user_id="U_OTHER",  # not the author
    )
    await app._handle_block_action(payload)  # pyright: ignore[reportPrivateUsage]

    assert not cancel.is_set(), "cancel Event must NOT be set for a non-author click (D-02)"


async def test_handle_block_action_when_status_ts_not_in_registry_is_noop() -> None:
    """block_actions with a status_ts absent from the registry is a silent no-op."""
    app = _make_app()

    payload = _make_block_actions_payload(
        action_id="cancel_turn",
        message_ts="9999999999.000001",  # not in registry
        user_id="U_AUTHOR",
    )
    # Must not raise — turn already ended/deregistered
    await app._handle_block_action(payload)  # pyright: ignore[reportPrivateUsage]


async def test_handle_block_action_when_wrong_action_id_is_ignored() -> None:
    """block_actions with action_id != 'cancel_turn' is silently ignored."""
    app = _make_app()
    cancel = asyncio.Event()
    app._cancel_registry["1000000000.000001"] = (cancel, "U_AUTHOR")  # pyright: ignore[reportPrivateUsage]

    payload = _make_block_actions_payload(
        action_id="some_other_action",  # not cancel_turn
        message_ts="1000000000.000001",
        user_id="U_AUTHOR",
    )
    await app._handle_block_action(payload)  # pyright: ignore[reportPrivateUsage]

    assert not cancel.is_set(), "cancel Event must NOT be set for an unrecognised action_id"


async def test_on_request_interactive_block_actions_acks_first_then_spawns() -> None:
    """on_request with type='interactive' sends ack before spawning the cancel handler
    (ack-first preserved for block_actions envelopes)."""
    fake_client = _FakeSocketClient()
    app = _make_app()

    spawned: list[str] = []
    original_spawn = app._spawn  # pyright: ignore[reportPrivateUsage]

    def _spy_spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        spawned.append("spawned")
        return original_spawn(coro)

    app._spawn = _spy_spawn  # type: ignore[method-assign]

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_interactive_001",
        payload=_make_block_actions_payload(
            action_id="cancel_turn",
            message_ts="1000000000.000001",
            user_id="U_AUTHOR",
        ),
    )
    await app.on_request(fake_client, req)  # type: ignore[arg-type]

    assert "send_socket_mode_response" in fake_client.call_log, (
        "on_request must call send_socket_mode_response for interactive events"
    )
    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "send_socket_mode_response must be the FIRST entry in call_log (ack-first preserved)"
    )

    # Drain spawned tasks so the test loop is clean.
    pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_on_request_privacy_disconnect_block_action_spawns_privacy_handler() -> None:
    """privacy_slack_disconnect block_action must route to handle_privacy_block_action —
    the privacy panel's Disconnect Slack button is dead if the dispatcher drops it."""
    fake_client = _FakeSocketClient()
    app = _make_app()

    dispatched: list[str] = []

    async def _fake_handle_privacy(runtime: Any, payload: Any) -> None:
        dispatched.append(str(payload["actions"][0]["action_id"]))

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_privacy_disconnect_001",
        payload=_make_block_actions_payload(action_id="privacy_slack_disconnect"),
    )
    with patch(
        "daimon.adapters.slack.app.handle_privacy_block_action",
        new=_fake_handle_privacy,
    ):
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert dispatched == ["privacy_slack_disconnect"], (
        "handle_privacy_block_action must be spawned for privacy_slack_disconnect"
    )


# ---------------------------------------------------------------------------
# Phase 82 Plan 05 — slash_commands branch + view_submission ack-with-payload
# ---------------------------------------------------------------------------


async def test_on_request_slash_commands_acks_first_then_spawns_help_handler() -> None:
    """slash_commands envelope → ack first, then spawn handle_help_command (ack-first).

    Mirrors the events_api ack-first test (:160-184) for the new slash_commands branch.
    """
    fake_client = _FakeSocketClient()
    app = _make_app()

    spawned_cmds: list[str] = []

    async def _fake_help_command(runtime: Any, payload: Any) -> None:
        spawned_cmds.append("help")

    with patch("daimon.adapters.slack.app.handle_help_command", new=_fake_help_command):
        req = SocketModeRequest(
            type="slash_commands",
            envelope_id="env_slash_help_001",
            payload={
                "command": "/help",
                "team_id": "T_TEST",
                "user_id": "U_TEST",
                "channel_id": "C_TEST",
                "trigger_id": "trig_001",
            },
        )
        await app.on_request(fake_client, req)  # type: ignore[arg-type]

        # Drain spawned tasks.
        pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "slash_commands must ack first — send_socket_mode_response before spawning the handler"
    )
    assert "help" in spawned_cmds, (
        "handle_help_command must be spawned for a /help slash_commands envelope"
    )


async def test_on_request_view_submission_mismatch_acks_errors_and_no_purge() -> None:
    """view_submission with wrong username acks response_action=errors; no purge spawned.

    Verifies T-82-19 (ack-first) + D-05 (mismatch → re-display error, NO purge).
    The ack carries the error payload; the modal is NOT dismissed.
    """
    fake_client = _FakeSocketClient()
    app = _make_app()

    account_id = uuid.uuid4()
    payload: dict[str, Any] = {
        "type": "view_submission",
        "team": {"id": "T_TEST"},
        "user": {"id": "U_TEST"},
        "view": {
            "callback_id": "privacy_delete",
            "id": "V_TEST",
            "private_metadata": json.dumps(
                {
                    "account_id": str(account_id),
                    "user_name": "expected_user",
                    "view_id": "V_TEST",
                }
            ),
            "state": {
                "values": {
                    "confirm_name_block": {
                        "confirm_name": {"value": "wrong_user"},  # mismatch
                    }
                }
            },
        },
    }

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_vs_mismatch_001",
        payload=payload,
    )

    with patch(
        "daimon.adapters.slack.app.run_purge_and_update", new_callable=AsyncMock
    ) as mock_purge:
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "view_submission must ack first (send_socket_mode_response before any I/O)"
    )
    ack_payload: dict[str, Any] = fake_client.sent_responses[0].payload or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]  # SocketModeResponse.payload untyped
    assert ack_payload.get("response_action") == "errors", (
        "mismatched username must ack with response_action=errors (D-05 — no purge)"
    )
    mock_purge.assert_not_called()  # pyright: ignore[reportUnknownMemberType]


async def test_on_request_view_submission_match_acks_update_and_spawns_purge() -> None:
    """view_submission with correct username acks response_action=update and spawns purge.

    Verifies T-82-19 (ack-first) + D-05 (match → Deleting… view, purge in background).
    The ack carries response_action=update; run_purge_and_update is spawned as a bg task.
    """
    fake_client = _FakeSocketClient()
    app = _make_app()

    account_id = uuid.uuid4()
    matching_name = "alice"
    payload: dict[str, Any] = {
        "type": "view_submission",
        "team": {"id": "T_TEST"},
        "user": {"id": "U_TEST"},
        "view": {
            "callback_id": "privacy_delete",
            "id": "V_TEST",
            "private_metadata": json.dumps(
                {
                    "account_id": str(account_id),
                    "user_name": matching_name,
                    "view_id": "V_TEST",
                }
            ),
            "state": {
                "values": {
                    "confirm_name_block": {
                        "confirm_name": {"value": matching_name},  # correct match
                    }
                }
            },
        },
    }

    req = SocketModeRequest(
        type="interactive",
        envelope_id="env_vs_match_001",
        payload=payload,
    )

    fake_wc = MagicMock()

    with (
        patch(
            "daimon.adapters.slack.app.resolve_web_client", new_callable=AsyncMock
        ) as mock_resolve,
        patch(
            "daimon.adapters.slack.app.run_purge_and_update", new_callable=AsyncMock
        ) as mock_purge,
    ):
        mock_resolve.return_value = fake_wc
        await app.on_request(fake_client, req)  # type: ignore[arg-type]
        # Drain the spawned _run_purge() background task.
        pending = list(app._bg_tasks)  # pyright: ignore[reportPrivateUsage]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert fake_client.call_log[0] == "send_socket_mode_response", (
        "view_submission must ack first (ack before any background I/O)"
    )
    ack_payload: dict[str, Any] = fake_client.sent_responses[0].payload or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]  # SocketModeResponse.payload untyped
    assert ack_payload.get("response_action") == "update", (
        "matching username must ack with response_action=update (D-05 — Deleting… view)"
    )
    mock_purge.assert_called_once()  # pyright: ignore[reportUnknownMemberType]
