from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from aioresponses import aioresponses
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import direct_messages
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DirectMessagePolicy,
    DiscordSettings,
    Settings,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from slack_sdk.web.async_client import AsyncWebClient


def runtime_and_auth(platform="discord", policy=None):
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform=platform,
        external_id="111" if platform == "discord" else "T123",
        platform_user_id="42" if platform == "discord" else "U123",
        is_admin=False,
    )
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),
        anthropic=AnthropicSettings(api_key=SecretStr("test")),
        discord=DiscordSettings(bot_token=SecretStr("test")),
        direct_message_policies={} if policy is None else {str(auth.tenant_id): policy},
        _env_file=None,
    )
    runtime = McpRuntime(
        settings=settings,
        session_factory=MagicMock(),
        client=MagicMock(),
        deployment_default=DeploymentDefault(),
    )
    return runtime, auth


@pytest.mark.parametrize(
    "policy",
    [
        DirectMessagePolicy(mode="disabled"),
        DirectMessagePolicy(mode="allowlist", recipient_ids=["999"]),
    ],
)
async def test_policy_denies_before_network(policy):
    runtime, auth = runtime_and_auth(policy=policy)
    with pytest.raises(ToolError, match="denied"):
        await direct_messages.send_direct_message_impl(
            runtime, auth, recipient_id="123", content="hello"
        )


@pytest.mark.parametrize("recipient", ["<@123>", "", "123,456", "C123"])
async def test_invalid_recipient_rejected(recipient):
    runtime, auth = runtime_and_auth()
    with pytest.raises(ToolError, match="user ID"):
        await direct_messages.send_direct_message_impl(
            runtime, auth, recipient_id=recipient, content="hello"
        )


@pytest.mark.parametrize("member", [True, False])
@pytest.mark.parametrize("fail_delivery", [True, False])
@pytest.mark.parametrize("with_connect_button", [True, False])
async def test_discord_live_membership_precedes_dm_and_splits(
    monkeypatch, member, fail_delivery, with_connect_button
):
    runtime, auth = runtime_and_auth()
    calls = []

    async def login(self, token):
        return {"id": "1", "username": "bot", "discriminator": "0", "avatar": None, "bot": True}

    async def request(self, route, **kwargs):
        calls.append((route.method, route.path, kwargs))
        if route.path == "/guilds/{guild_id}":
            return {
                "id": "111",
                "name": "Tenant",
                "roles": [],
                "emojis": [],
                "features": [],
                "owner_id": "42",
            }
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            if route.url.rsplit("/", 1)[1] == "123" and not member:
                from types import SimpleNamespace

                raise discord.NotFound(
                    SimpleNamespace(status=404, reason="Not Found"),
                    {"code": 10007, "message": "Unknown Member"},
                )
            return {
                "user": {
                    "id": route.url.rsplit("/", 1)[1],
                    "username": "member",
                    "discriminator": "0",
                    "avatar": None,
                },
                "roles": [],
                "joined_at": "2026-01-01T00:00:00+00:00",
                "flags": 0,
            }
        if route.path == "/users/@me/channels":
            return {
                "id": "555",
                "type": 1,
                "recipients": [
                    {"id": "123", "username": "member", "discriminator": "0", "avatar": None}
                ],
            }
        if route.path == "/channels/{channel_id}/messages":
            if fail_delivery and sum(path == route.path for _, path, _ in calls) == 2:
                from types import SimpleNamespace

                raise discord.Forbidden(
                    SimpleNamespace(status=403, reason="Forbidden"),
                    {"code": 50007, "message": "Cannot send messages to this user"},
                )
            data = kwargs["json"]
            return {
                "id": str(1000 + len(calls)),
                "channel_id": "555",
                "author": {"id": "1", "username": "bot", "discriminator": "0", "avatar": None},
                "content": data["content"],
                "timestamp": "2026-01-01T00:00:00+00:00",
                "type": 0,
                "attachments": [],
                "embeds": [],
                "mentions": [],
                "mention_roles": [],
                "pinned": False,
                "tts": False,
            }
        raise AssertionError(route.path)

    monkeypatch.setattr(discord.http.HTTPClient, "static_login", login)
    monkeypatch.setattr(discord.http.HTTPClient, "request", request)
    if not member:
        with pytest.raises(ToolError, match="Unknown Member"):
            await direct_messages.send_direct_message_impl(
                runtime, auth, recipient_id="123", content="hello"
            )
        assert not any(path == "/users/@me/channels" for _, path, _ in calls)
        return
    if fail_delivery:
        with pytest.raises(ToolError, match="after 1 message"):
            await direct_messages.send_direct_message_impl(
                runtime, auth, recipient_id="123", content="x" * 4000
            )
        return
    result = await direct_messages.send_direct_message_impl(
        runtime,
        auth,
        recipient_id="123",
        content="Connect GitHub for ResearchBot." if with_connect_button else "x" * 4000,
    )
    assert len(result.message_ids) == (1 if with_connect_button else 3)
    sent = [
        kwargs["json"] for _, path, kwargs in calls if path == "/channels/{channel_id}/messages"
    ]
    assert "".join(body["content"] for body in sent) == (
        "Connect GitHub for ResearchBot." if with_connect_button else "x" * 4000
    )
    assert all(body["allowed_mentions"]["parse"] == [] for body in sent)
    assert all(not body.get("components") for body in sent)


@pytest.mark.parametrize("recipient_team", ["T123", "TOTHER"])
async def test_slack_checks_workspace_before_opening_dm(monkeypatch, recipient_team):
    runtime, auth = runtime_and_auth("slack")

    async def client_factory(runtime, *, team_id):
        assert team_id == "T123"
        return AsyncWebClient(token="xoxb-test")

    monkeypatch.setattr(direct_messages, "slack_web_client", client_factory)
    with aioresponses() as http:
        http.get(
            re.compile(r"https://slack\.com/api/users\.info.*user=U123.*"),
            payload={"ok": True, "user": {"id": "U123", "team_id": "T123"}},
        )
        http.get(
            re.compile(r"https://slack\.com/api/users\.info.*user=U456.*"),
            payload={"ok": True, "user": {"id": "U456", "team_id": recipient_team}},
        )
        http.post(
            re.compile(r"https://slack\.com/api/conversations\.open.*"),
            payload={"ok": True, "channel": {"id": "D123"}},
        )
        http.post("https://slack.com/api/chat.postMessage", payload={"ok": True, "ts": "123.456"})
        if recipient_team != "T123":
            with pytest.raises(ToolError, match="active human members"):
                await direct_messages.send_direct_message_impl(
                    runtime, auth, recipient_id="U456", content="private"
                )
            assert not any("conversations.open" in str(url) for _, url in http.requests)
        else:
            result = await direct_messages.send_direct_message_impl(
                runtime, auth, recipient_id="U456", content="private"
            )
            assert result.channel_id == "D123"
            assert result.message_ids == ["123.456"]


def test_allowlist_never_expands_to_unlisted_recipients():
    policy = DirectMessagePolicy(mode="allowlist", recipient_ids=["U123"])
    assert policy.allows("U123")
    assert not policy.allows("U456")
    assert DirectMessagePolicy().allows("U456")


@pytest.mark.parametrize(
    "policy",
    [
        DirectMessagePolicy(mode="disabled"),
        DirectMessagePolicy(mode="allowlist", recipient_ids=["U999"]),
    ],
)
async def test_slack_policy_denies_before_building_client(monkeypatch, policy):
    runtime, auth = runtime_and_auth("slack", policy=policy)
    factory = AsyncMock(side_effect=AssertionError("policy denial must precede network access"))
    monkeypatch.setattr(direct_messages, "slack_web_client", factory)
    with pytest.raises(ToolError, match="denied"):
        await direct_messages.send_direct_message_impl(
            runtime, auth, recipient_id="U456", content="private"
        )
    factory.assert_not_called()


@pytest.mark.parametrize(
    "error", ["missing_scope", "token_revoked", "invalid_auth", "token_expired"]
)
@pytest.mark.parametrize("after_messages", [0, 1])
async def test_slack_install_errors_explain_reauthorization(monkeypatch, error, after_messages):
    runtime, auth = runtime_and_auth("slack")

    async def client_factory(runtime, *, team_id):
        return AsyncWebClient(token="xoxb-test")

    monkeypatch.setattr(direct_messages, "slack_web_client", client_factory)
    with aioresponses() as http:
        for user_id in ("U123", "U456"):
            http.get(
                re.compile(r"https://slack\.com/api/users\.info.*user=" + user_id + r".*"),
                payload={"ok": True, "user": {"id": user_id, "team_id": "T123"}},
            )
        failure = {"ok": False, "error": error, "needed": "im:write"}
        http.post(
            re.compile(r"https://slack\.com/api/conversations\.open.*"),
            payload={"ok": True, "channel": {"id": "D123"}} if after_messages else failure,
        )
        if after_messages:
            http.post(
                "https://slack.com/api/chat.postMessage", payload={"ok": True, "ts": "123.456"}
            )
            http.post("https://slack.com/api/chat.postMessage", payload=failure)
        with pytest.raises(ToolError) as raised:
            await direct_messages.send_direct_message_impl(
                runtime, auth, recipient_id="U456", content="x" * 4000
            )
        message = str(raised.value)
        assert f"after {after_messages} message(s)" in message
        assert "workspace admin" in message
        assert "reinstall" in message and "install link" in message
        if error == "missing_scope":
            assert "im:write" in message
        else:
            assert "no longer valid" in message
        posts = sum(
            len(calls)
            for (_, url), calls in http.requests.items()
            if "chat.postMessage" in str(url)
        )
        assert posts == (2 if after_messages else 0)


@pytest.mark.parametrize("platform", ["discord", "slack"])
@pytest.mark.parametrize("key_format", ["uppercase", "unhyphenated"])
@pytest.mark.parametrize("mode", ["disabled", "allowlist"])
async def test_noncanonical_policy_denies_before_client_creation(
    monkeypatch, platform, key_format, mode
):
    runtime, auth = runtime_and_auth(platform)
    key = str(auth.tenant_id).upper() if key_format == "uppercase" else auth.tenant_id.hex
    monkeypatch.setenv(
        "DAIMON_DIRECT_MESSAGE_POLICIES",
        json.dumps({key: {"mode": mode, "recipient_ids": ["999", "U999"]}}),
    )
    settings = Settings(
        database=runtime.settings.database,
        anthropic=runtime.settings.anthropic,
        discord=runtime.settings.discord,
        _env_file=None,
    )
    runtime = replace(runtime, settings=settings)
    discord_client = MagicMock(side_effect=AssertionError("Discord client must not be created"))
    slack_client = MagicMock(side_effect=AssertionError("Slack client must not be created"))
    monkeypatch.setattr(direct_messages, "rest_client", discord_client)
    monkeypatch.setattr(direct_messages, "slack_web_client", slack_client)
    with pytest.raises(ToolError, match="denied"):
        await direct_messages.send_direct_message_impl(
            runtime, auth, recipient_id="123" if platform == "discord" else "U456", content="hello"
        )
    discord_client.assert_not_called()
    slack_client.assert_not_called()
