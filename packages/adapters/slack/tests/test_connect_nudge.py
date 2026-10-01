"""Once-ever ephemeral connect nudge on first mention (Task 11).

Drives ``SlackApp._maybe_post_connect_nudge`` directly against a fake
``AsyncWebClient`` transport (records ``chat.postEphemeral`` calls) and the
real Task 4 stores on the test DB. No ``_orchestrate`` plumbing needed here —
the method only touches ``slack_user_tokens`` / ``slack_connect_prompts`` and
posts an ephemeral message.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.slack_connect_prompts import was_connect_prompted
from daimon.core.stores.slack_user_tokens import upsert_slack_user_token
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing import ma_session, ma_session_agent
from pydantic import SecretStr
from slack_sdk.errors import SlackApiError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .harness import make_orchestrate_app


def _make_nudge_app(sessionmaker: async_sessionmaker[AsyncSession]) -> SlackApp:
    """Build a SlackApp with real slack/mcp settings needed by the nudge method.

    Mirrors ``_make_orchestrate_app`` in ``test_app.py`` (MagicMock settings,
    real sessionmaker) but sets ``slack.signing_secret`` and
    ``mcp.app_root_url`` explicitly since ``_maybe_post_connect_nudge`` reads
    both directly (a bare MagicMock would satisfy the ``is not None`` checks
    but ``build_slack_connect_url`` needs a real secret and URL string).
    """
    settings = MagicMock()
    settings.slack.signing_secret = SecretStr("test-signing-secret")
    settings.mcp.app_root_url = "https://daimon.example.com"

    runtime = SlackRuntime(
        settings=settings,
        anthropic=MagicMock(),
        sessionmaker=sessionmaker,
        billing_config=None,
        http_client=MagicMock(spec=httpx.AsyncClient),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        deployment_default=DeploymentDefault(agent_name="daimon", environment_name="default"),
    )
    return SlackApp(runtime=runtime)


async def test_nudge_posted_once_for_unconnected_unprompted_user(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """First mention posts exactly one ephemeral nudge; a second mention for
    the same (team, user) does not re-nudge (once-ever)."""
    app = _make_nudge_app(db_session_factory)
    team_id = "T_NUDGE_ONCE"
    slack_user_id = "U_NUDGE_ONCE"

    await app._maybe_post_connect_nudge(  # pyright: ignore[reportPrivateUsage]
        fake_slack_web_client.client,
        team_id=team_id,
        slack_user_id=slack_user_id,
        channel="C1",
        thread_ts="1.1",
    )

    ephemeral_calls = [
        req
        for (method, url), reqs in fake_slack_web_client.mock.requests.items()
        if method == "POST" and url.path == "/api/chat.postEphemeral"
        for req in reqs
    ]
    assert len(ephemeral_calls) == 1, "first mention must post exactly one nudge"
    posted_body: dict[str, Any] = ephemeral_calls[0].kwargs["json"]
    assert "/oauth/slack/connect?state=" in posted_body["text"], "nudge carries connect URL"
    assert posted_body["thread_ts"] == "1.1", "nudge lands in the thread being read"

    await app._maybe_post_connect_nudge(  # pyright: ignore[reportPrivateUsage]
        fake_slack_web_client.client,
        team_id=team_id,
        slack_user_id=slack_user_id,
        channel="C1",
        thread_ts="1.1",
    )

    ephemeral_calls_after = [
        req
        for (method, url), reqs in fake_slack_web_client.mock.requests.items()
        if method == "POST" and url.path == "/api/chat.postEphemeral"
        for req in reqs
    ]
    assert len(ephemeral_calls_after) == 1, "second mention must not re-nudge (once ever)"


async def test_nudge_skipped_for_connected_user(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A user with an existing slack_user_tokens row is never nudged."""
    team_id = "T_NUDGE_CONNECTED"
    slack_user_id = "U_NUDGE_CONNECTED"

    async with db_session_factory() as s:
        await upsert_slack_user_token(
            s,
            team_id=team_id,
            slack_user_id=slack_user_id,
            encrypted_token=b"ciphertext",
            scopes="channels:history",
        )
        await s.commit()

    app = _make_nudge_app(db_session_factory)

    await app._maybe_post_connect_nudge(  # pyright: ignore[reportPrivateUsage]
        fake_slack_web_client.client,
        team_id=team_id,
        slack_user_id=slack_user_id,
        channel="C1",
        thread_ts="1.1",
    )

    ephemeral_calls = [
        req
        for (method, url), reqs in fake_slack_web_client.mock.requests.items()
        if method == "POST" and url.path == "/api/chat.postEphemeral"
        for req in reqs
    ]
    assert ephemeral_calls == [], "connected users are never nudged"


async def test_nudge_not_marked_when_post_fails(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A failed chat.postEphemeral must not consume the once-ever marker, so
    the next mention retries the nudge."""
    team_id = "T_NUDGE_FAIL"
    slack_user_id = "U_NUDGE_FAIL"

    # Override the fixture's repeat=True 200-OK chat.postEphemeral matcher:
    # aioresponses matches in registration order, so the fixture's default
    # (registered first) would otherwise always win. Clear and re-register
    # only chat.postEphemeral with an error payload.
    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.post(  # pyright: ignore[reportUnknownMemberType]
        "https://slack.com/api/chat.postEphemeral",
        payload={"ok": False, "error": "channel_not_found"},
    )

    app = _make_nudge_app(db_session_factory)

    with pytest.raises(SlackApiError):
        await app._maybe_post_connect_nudge(  # pyright: ignore[reportPrivateUsage]
            fake_slack_web_client.client,
            team_id=team_id,
            slack_user_id=slack_user_id,
            channel="C1",
            thread_ts="1.1",
        )

    async with db_session_factory() as s:
        assert (
            await was_connect_prompted(s, team_id=team_id, slack_user_id=slack_user_id) is False
        ), "failed post must not consume the once-ever marker"


async def test_orchestrate_continues_when_nudge_store_call_raises_sqlalchemy_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A DB failure inside the connect-nudge path (e.g. get_slack_user_token
    raising SQLAlchemyError) must not fail the turn — the _orchestrate
    call-site wraps the nudge in contextlib.suppress(..., SQLAlchemyError, ...)
    precisely so a nudge-path DB hiccup never blocks the actual turn."""
    team_id = "T_NUDGE_DB_FAIL"
    channel = "C1"
    thread_ts = "9000000099.000001"
    event_ts = thread_ts
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

    await provision_tenant(
        db_session_factory, platform="slack", workspace_id=team_id, signup_credit=Decimal("10")
    )

    app, _ = make_orchestrate_app(
        db_session_factory, connect_nudge_url="https://daimon.example.com"
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
        "user": "U_NUDGE_DB_FAIL",
        "text": "<@U_BOT> hello",
    }

    _fake_session = ma_session(
        id="sess-nudge-fail-001",
        agent=ma_session_agent(id="agent_nudge_fail_id"),
        environment_id="env_nudge_fail_id",
    )

    with (
        patch(
            "daimon.adapters.slack.app.get_slack_user_token",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db unavailable"),
        ),
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
        mock_resolve_agent.return_value = "agent_nudge_fail_id"
        mock_resolve_env.return_value = "env_nudge_fail_id"
        mock_create_session.return_value = _fake_session
        mock_run_turn.side_effect = _fake_run_turn

        # Must not raise: the nudge's DB failure is swallowed at the
        # _orchestrate call site, and the turn proceeds normally.
        await app._orchestrate(  # pyright: ignore[reportPrivateUsage]
            event,
            team_id=team_id,
            channel=channel,
            event_ts=event_ts,
            web_client=fake_slack_web_client.client,
            tenant_id=tenant_id,
        )

    assert mock_run_turn.called, "the turn itself must still run despite the nudge DB failure"
    ephemeral_calls = [
        req
        for (method, url), reqs in fake_slack_web_client.mock.requests.items()
        if method == "POST" and url.path == "/api/chat.postEphemeral"
        for req in reqs
    ]
    assert ephemeral_calls == [], "no nudge could be posted once its own DB lookup raised"
