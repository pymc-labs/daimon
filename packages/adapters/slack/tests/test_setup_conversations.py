"""Setup roots keep immutable identities and lifecycle without billed execution."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Literal

import httpx
import pytest
from aioresponses import aioresponses
from anthropic.types.beta import BetaManagedAgentsAgent, BetaManagedAgentsModelConfig
from cryptography.fernet import Fernet
from daimon.adapters.slack.admin import ADMIN_NOUN
from daimon.adapters.slack.agent_setup.actions import handle_agent_setup_action
from daimon.adapters.slack.app import SlackApp
from daimon.adapters.slack.runtime import SlackRuntime, build_turn_deps
from daimon.adapters.slack.setup_conversations import (
    create_setup_conversation,
    handle_setup_lifecycle,
)
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    Settings,
)
from daimon.core.errors import DaimonError
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.setup_conversations import build_setup_opener, setup_thread_name
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.thread_agent_bindings import get_binding
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_fake_anthropic, list_response
from pydantic import PostgresDsn, SecretStr
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize(
    ("is_admin", "target_deleted", "handoff_failure", "entry_surface"),
    [
        (False, False, None, "direct"),
        (True, False, None, "direct"),
        (False, True, None, "direct"),
        (True, True, None, "direct"),
        (False, False, "permalink", "direct"),
        (False, False, "launcher", "direct"),
        (False, False, None, "modal"),
        (True, False, None, "modal"),
        (False, False, None, "message"),
        (True, False, None, "message"),
    ],
)
async def test_setup_root_routes_daimon_and_retains_target_through_archive_and_delete(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    is_admin: bool,
    target_deleted: bool,
    handoff_failure: Literal["permalink", "launcher"] | None,
    entry_surface: Literal["direct", "modal", "message"],
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_SETUP")
    fernet_key = Fernet.generate_key()
    await upsert_slack_bot_token(
        db_session,
        team_id="T_SETUP",
        encrypted_token=Fernet(fernet_key).encrypt(b"xoxb-test"),
    )
    await db_session.commit()
    now = datetime.now(UTC)
    responder = BetaManagedAgentsAgent(
        id="agent_daimon",
        type="agent",
        name="daimon",
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        tools=[],
        skills=[],
        mcp_servers=[],
        metadata={
            "daimon_tenant": str(tenant.id),
            "daimon_name": "daimon",
            "daimon_managed": "true",
        },
        created_at=now,
        updated_at=now,
    )
    target = BetaManagedAgentsAgent(
        id="agent_specialist",
        type="agent",
        name="specialist",
        version=1,
        model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6", speed="standard"),
        system=None,
        tools=[],
        skills=[],
        mcp_servers=[],
        metadata={"daimon_tenant": str(tenant.id), "daimon_name": "specialist"},
        created_at=now,
        updated_at=now,
    )
    requests: list[httpx.Request] = []

    def ma_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/agents":
            return list_response([responder.model_dump(mode="json")])
        if request.url.path == "/v1/agents/agent_specialist":
            if target_deleted:
                return httpx.Response(
                    404,
                    json={
                        "type": "error",
                        "error": {"type": "not_found_error", "message": "deleted"},
                    },
                )
            return httpx.Response(200, json=target.model_dump(mode="json"))
        raise AssertionError(f"Unexpected MA call: {request.method} {request.url.path}")

    anthropic = build_fake_anthropic(ma_handler)
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]  # BaseSettings runtime option
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://test:test@localhost/test")),
        anthropic=AnthropicSettings(api_key=SecretStr("test")),
        crypto=CryptoSettings(keys=(SecretStr(fernet_key.decode()),)),
    )
    cache = new_resolver_cache()
    default = DeploymentDefault(agent_name="specialist")
    async with httpx.AsyncClient() as http_client:
        runtime = SlackRuntime(
            settings=settings,
            anthropic=anthropic,
            sessionmaker=db_session_factory,
            billing_config=None,
            http_client=http_client,
            resolver_cache=cache,
            deployment_default=default,
            turn_deps=build_turn_deps(
                settings,
                anthropic,
                db_session_factory,
                deployment_default=default,
                resolver_cache=cache,
                billing_config=None,
            ),
        )
        with aioresponses() as slack:
            slack.post(
                "https://slack.com/api/auth.test",
                payload={"ok": True, "user_id": "U_BOT"},
                repeat=True,
            )
            slack.get(
                re.compile(r"https://slack.com/api/users.info.*"),
                payload={"ok": True, "user": {"is_admin": is_admin}},
                repeat=True,
            )
            slack.post(
                "https://slack.com/api/chat.postMessage", payload={"ok": True, "ts": "123.456"}
            )
            slack.post(
                "https://slack.com/api/chat.postMessage", payload={"ok": True, "ts": "123.457"}
            )
            thread_link = (
                "https://workspace.slack.com/archives/C_PARENT/p123457"
                "?thread_ts=123.456&cid=C_PARENT"
            )
            slack.get(
                re.compile(r"https://slack.com/api/chat.getPermalink.*"),
                payload={"ok": False, "error": "message_not_found"}
                if handoff_failure == "permalink"
                else {"ok": True, "permalink": thread_link},
            )
            slack.post(
                "https://slack.com/api/chat.update",
                payload={"ok": False, "error": "channel_not_found"}
                if handoff_failure == "launcher"
                else {"ok": True},
            )
            slack.post(
                re.compile(r"https://slack.com/api/chat.delete.*"),
                payload={"ok": True},
                repeat=True,
            )
            slack.post("https://slack.com/api/views.update", payload={"ok": True})
            slack.post("https://slack.com/api/chat.postEphemeral", payload={"ok": True})
            if target_deleted:
                with pytest.raises(DaimonError, match="no longer exists"):
                    await create_setup_conversation(
                        runtime,
                        AsyncWebClient(token="xoxb-test"),
                        team_id="T_SETUP",
                        channel_id="C_PARENT",
                        user_id="U_OPENER",
                        target_ma_agent_id=target.id,
                    )
                assert not slack.requests, "deleted target must fail before platform creation"
                return
            if handoff_failure:
                with pytest.raises(SlackApiError):
                    await create_setup_conversation(
                        runtime,
                        AsyncWebClient(token="xoxb-test"),
                        team_id="T_SETUP",
                        channel_id="C_PARENT",
                        user_id="U_OPENER",
                        target_ma_agent_id=target.id,
                    )
                deleted_messages = [
                    url.query["ts"]
                    for (method, url), calls in slack.requests.items()
                    if method == "POST" and url.path.endswith("chat.delete")
                    for _call in calls
                ]
                assert deleted_messages == ["123.457", "123.456"], (
                    "failed handoff must remove both bot-created messages"
                )
                async with db_session_factory() as session:
                    binding = await get_binding(
                        session,
                        tenant_id=tenant.id,
                        platform="slack",
                        parent_channel_id="C_PARENT",
                        thread_id="123.456",
                    )
                assert binding is not None and binding.deleted, (
                    "failed setup must not remain an active conversation"
                )
                return
            if entry_surface == "direct":
                link = await create_setup_conversation(
                    runtime,
                    AsyncWebClient(token="xoxb-test"),
                    team_id="T_SETUP",
                    channel_id="C_PARENT",
                    user_id="U_OPENER",
                    target_ma_agent_id=target.id,
                )
            else:
                await handle_agent_setup_action(
                    runtime,
                    {
                        "team": {"id": "T_SETUP"},
                        "user": {"id": "U_OPENER"},
                        "channel": {"id": "C_PARENT"},
                        "actions": [{"action_id": "agent_setup__conversation", "value": target.id}],
                        "view": {
                            "id": "V_PANEL",
                            "private_metadata": json.dumps({"channel_id": "C_PARENT"}),
                        }
                        if entry_surface == "modal"
                        else {},
                    },
                )
                handoffs = [
                    (url.path, call.kwargs["json"])
                    for (method, url), calls in slack.requests.items()
                    if method == "POST"
                    and url.path.endswith(("views.update", "chat.postEphemeral"))
                    for call in calls
                ]
                assert len(handoffs) == 1, "entry should deliver one visible handoff"
                destination, handoff = handoffs[0]
                if entry_surface == "modal":
                    assert destination.endswith("views.update"), (
                        "modal entry must show the reply button in the open panel"
                    )
                    assert handoff["view_id"] == "V_PANEL", (
                        "handoff must replace the caller's panel"
                    )
                    handoff_blocks = handoff["view"]["blocks"]
                else:
                    assert destination.endswith("chat.postEphemeral"), (
                        "message entry must return the reply button to its caller"
                    )
                    assert handoff["user"] == "U_OPENER", "handoff must reach the opener"
                    handoff_blocks = handoff["blocks"]
                link = handoff_blocks[1]["elements"][0]["url"]
            assert link == thread_link, "handoff should use Slack's permalink to the opening reply"
            slack_requests_before = sum(len(calls) for calls in slack.requests.values())
            await SlackApp(runtime=runtime)._orchestrate(  # pyright: ignore[reportPrivateUsage]  # exercise listener turn boundary
                {
                    "type": "app_mention",
                    "ts": "123.456",
                    "user": "U_BOT",
                    "bot_id": "B_BOT",
                    "text": "Mention <@U_BOT>",
                },
                team_id="T_SETUP",
                channel="C_PARENT",
                event_ts="123.456",
                web_client=AsyncWebClient(token="xoxb-test"),
                tenant_id=tenant.id,
            )
            assert sum(len(calls) for calls in slack.requests.values()) == slack_requests_before, (
                "echoed opener must never begin turn admission or Slack role lookup"
            )
            await SlackApp(runtime=runtime)._orchestrate(  # pyright: ignore[reportPrivateUsage]  # exercise listener turn boundary
                {
                    "type": "app_mention",
                    "ts": "123.457",
                    "thread_ts": "123.456",
                    "user": "U_BOT",
                    "bot_id": "B_BOT",
                    "text": "Reply here and mention <@U_BOT>",
                },
                team_id="T_SETUP",
                channel="C_PARENT",
                event_ts="123.457",
                web_client=AsyncWebClient(token="xoxb-test"),
                tenant_id=tenant.id,
            )
            assert (
                sum(len(calls) for calls in slack.requests.values()) == slack_requests_before + 1
            ), "echoed thread reply may verify bot identity but must never begin a turn"
            updates = [
                call.kwargs["json"]
                for (method, url), calls in slack.requests.items()
                if method == "POST" and url.path.endswith("chat.update")
                for call in calls
            ]
            assert "specialist" in updates[0]["text"], "launcher should name the target"
            assert updates[0]["blocks"][1]["elements"][0]["url"] == link, (
                "visible reply button must open the created thread"
            )
            posts = [
                call.kwargs["json"]
                for (method, url), calls in slack.requests.items()
                if method == "POST" and url.path.endswith("chat.postMessage")
                for call in calls
            ]
            assert posts[1]["thread_ts"] == "123.456", "welcome belongs inside the setup thread"
            assert posts[1]["text"] == build_setup_opener(
                target_display="specialist",
                bot_mention="<@U_BOT>",
                is_admin=is_admin,
                admin_noun=ADMIN_NOUN,
            ), "the opener is posted as core renders it, with no Slack-only preamble"
            assert "specialist" in posts[1]["text"] and "<@U_BOT>" in posts[1]["text"], (
                "thread opener should name target and actual bot mention"
            )
            assert updates[0]["text"].startswith(setup_thread_name("specialist")), (
                "the launcher heading has one home in core, not a Slack spelling of its own"
            )
            assert "opened by" not in updates[0]["text"], (
                "the launcher should not add redundant opener attribution"
            )
        assert all(request.method == "GET" for request in requests), (
            "opening setup must not create a session or billed turn"
        )
        assert runtime.deployment_default.agent_name == "specialist", (
            "setup must preserve parent routing"
        )
        for event, archived, deleted in [
            (
                {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel": "C_PARENT",
                    "deleted_ts": "123.999",
                },
                False,
                False,
            ),
            ({"type": "channel_archive", "channel": "C_PARENT"}, True, False),
            ({"type": "channel_unarchive", "channel": "C_PARENT"}, False, False),
            (
                {
                    "type": "message",
                    "subtype": "message_deleted",
                    "channel": "C_PARENT",
                    "deleted_ts": "123.456",
                },
                False,
                True,
            ),
        ]:
            await handle_setup_lifecycle(runtime, event, team_id="T_SETUP")
            async with db_session_factory() as session:
                binding = await get_binding(
                    session,
                    tenant_id=tenant.id,
                    platform="slack",
                    parent_channel_id="C_PARENT",
                    thread_id="123.456",
                )
            assert binding is not None, "lifecycle retains conversation identity"
            assert binding.responder_ma_agent_id == responder.id, "Daimon must answer setup"
            assert binding.configuration_target_ma_agent_id == target.id, (
                "target must remain a separate concrete identity"
            )
            assert (binding.archived, binding.deleted) == (archived, deleted), (
                "lifecycle should track channel and root events"
            )
