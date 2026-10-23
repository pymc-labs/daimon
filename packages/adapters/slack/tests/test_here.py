"""Slack /here resolves the card and posts it only to the invoker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from daimon.adapters.slack.here import (  # pyright: ignore[reportPrivateUsage]
    _setter_display_name,
    _visible_channel_ids,
    build_here_attachment,
    handle_here_command,
)
from daimon.core.access_policy import ChannelRule
from daimon.core.errors import DaimonError
from daimon.core.here_card import CredentialStatus, HereCard, render_here_card_text
from slack_sdk.errors import SlackApiError


async def test_member_sees_public_bot_channels_and_shared_private_channels() -> None:
    client = MagicMock()
    client.users_conversations = AsyncMock(
        side_effect=[
            {
                "channels": [{"id": "C1", "is_private": False}],
                "response_metadata": {"next_cursor": "next"},
            },
            {
                "channels": [{"id": "G1", "is_private": True}],
                "response_metadata": {"next_cursor": ""},
            },
            {
                "channels": [{"id": "G1", "is_private": True}],
                "response_metadata": {"next_cursor": ""},
            },
        ]
    )
    client.users_info = AsyncMock(return_value={"user": {}})
    assert await _visible_channel_ids(client, "U1") == {"C1", "G1"}
    assert client.users_conversations.await_args_list[2].kwargs["types"] == "private_channel"


async def test_guest_sees_only_shared_channels() -> None:
    client = MagicMock()
    client.users_conversations = AsyncMock(
        side_effect=[
            {"channels": [{"id": "C1", "is_private": False}, {"id": "C2", "is_private": False}]},
            {"channels": [{"id": "C2", "is_private": False}]},
        ]
    )
    client.users_info = AsyncMock(return_value={"user": {"is_restricted": True}})
    assert await _visible_channel_ids(client, "U1") == {"C2"}


async def test_setter_uses_plain_display_name() -> None:
    client = MagicMock()
    client.users_info = AsyncMock(return_value={"user": {"profile": {"display_name": "Alex"}}})
    assert await _setter_display_name(client, "U123") == "Alex"


CREDENTIALS = (
    CredentialStatus(name="OPENAI_API_KEY", kind="agent key", configured=True),
    CredentialStatus(name="sk-live-fake-value", kind="MCP token", configured=True),
    CredentialStatus(
        name="GitHub (https://github.com/acme/private-repo)",
        kind="GitHub installation token",
        configured=True,
    ),
)
ANSWERING = {"blocked", "channel", "thread"}


def _card(state: str) -> HereCard:
    card = HereCard(
        platform="slack",
        channel_rule=ChannelRule(),
        who_may_answer="",
        reads_kept_inside=False,
        agent_name=None if state == "no_agent" else "ResearchBot",
        tier="thread" if state == "thread" else "channel",
        bot_can_view=state != "no_view",
        effective_writers="none" if state == "no_replies" else "any",
        agent_can_answer_here=state != "blocked",
        effective_readers="any",
        publishing_needs_approval=False,
        bot_can_read_history=True,
        credentials=CREDENTIALS,
        text="",
    )
    return card.model_copy(update={"text": render_here_card_text(card)})


@pytest.mark.parametrize(
    ("state", "title", "colour", "subline"),
    [
        ("no_view", "Daimon isn't in this channel", "#ED4245", "Invite it with /invite."),
        (
            "no_replies",
            "Replies are off here",
            "#ED4245",
            "An admin can turn them on in /agent-setup.",
        ),
        ("no_agent", "No agent answers here", "#95A5A6", "An admin can pick one in /agent-setup."),
        (
            "blocked",
            "ResearchBot can't answer here",
            "#F0B429",
            "An admin can change where it answers in /agent-setup.",
        ),
        ("channel", "ResearchBot answers here", "#2ECC71", None),
        ("thread", "ResearchBot answers in this thread", "#2ECC71", None),
    ],
)
def test_slack_block_kit_states(state: str, title: str, colour: str, subline: str | None) -> None:
    attachment = build_here_attachment(_card(state))
    blocks = attachment["blocks"]
    assert attachment["color"] == colour
    assert attachment["fallback"] == _card(state).text
    assert blocks[0] == {"type": "header", "text": {"type": "plain_text", "text": title}}
    assert [
        block["text"]["text"] for block in blocks if block["type"] == "section" and "text" in block
    ] == ([subline] if subline is not None else [])
    assert {
        field["text"]
        for block in blocks
        if block["type"] == "section"
        for field in block.get("fields", [])
    } == (
        {"*Reading*\nAny conversation", "*Publishing*\nNo approval"}
        if state in ANSWERING
        else set()
    )
    assert blocks[-1] == {
        "type": "context",
        "elements": [{"type": "plain_text", "text": "Channel setting. Threads can differ."}],
    }
    assert " · " not in str(blocks)
    for credential in CREDENTIALS:
        assert credential.name not in str(attachment)
        assert credential.kind not in str(attachment)


def test_slack_extra_lines_and_no_credentials() -> None:
    card = _card("channel").model_copy(
        update={"effective_writers": "own", "bot_can_read_history": False}
    )
    blocks = build_here_attachment(card)["blocks"]
    assert blocks[-2] == {
        "type": "context",
        "elements": [
            {
                "type": "plain_text",
                "text": "Only own agents answer here\nNo access to earlier messages",
            }
        ],
    }
    for credential in CREDENTIALS:
        assert credential.name not in str(blocks)


def test_slack_unknown_publishing_and_history_drop_their_lines() -> None:
    card = _card("channel").model_copy(
        update={"publishing_needs_approval": None, "bot_can_read_history": None}
    )
    blocks = build_here_attachment(card)["blocks"]
    assert [field["text"] for block in blocks for field in block.get("fields", [])] == [
        "*Reading*\nAny conversation"
    ]
    assert "No access to earlier messages" not in str(blocks)


async def test_here_posts_ephemeral_card() -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(return_value={"channel": {"is_private": False}})
    client.users_conversations = AsyncMock(return_value={"channels": []})
    client.users_info = AsyncMock(return_value={"user": {}})
    client.chat_postEphemeral = AsyncMock()
    runtime = MagicMock()
    runtime.settings.github.fallback_pat = None
    runtime.settings.github.app_id = None
    runtime.settings.github.app_private_key = None
    runtime.settings.mcp.public_url = None
    runtime.sessionmaker.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    runtime.sessionmaker.return_value.__aexit__ = AsyncMock(return_value=None)
    with (
        patch("daimon.adapters.slack.here.resolve_web_client", new=AsyncMock(return_value=client)),
        patch("daimon.adapters.slack.here.resolve_is_admin", new=AsyncMock(return_value=False)),
        patch(
            "daimon.adapters.slack.here.find_platform_principal", new=AsyncMock(return_value=None)
        ),
        patch(
            "daimon.adapters.slack.here.load_here_card",
            new=AsyncMock(return_value=_card("channel")),
        ) as load,
    ):
        await handle_here_command(runtime, {"team_id": "T1", "channel_id": "C1", "user_id": "U1"})
    assert load.await_count == 1
    assert load.await_args.kwargs["bot_can_view"] is False
    assert load.await_args.kwargs["thread_id"] is None
    assert load.await_args.kwargs["channel_level_only"] is True
    assert callable(load.await_args.kwargs["resolve_setter_display"])
    client.chat_postEphemeral.assert_awaited_once_with(
        channel="C1",
        user="U1",
        text="Channel status",
        attachments=[build_here_attachment(_card("channel"))],
        parse="none",
        link_names=False,
    )


async def test_private_channel_without_bot_membership_returns_card_by_response_url() -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(
        side_effect=SlackApiError("not found", MagicMock(data={"error": "channel_not_found"}))
    )
    client.users_conversations = AsyncMock(return_value={"channels": []})
    client.users_info = AsyncMock(return_value={"user": {}})
    client.chat_postEphemeral = AsyncMock()
    runtime = MagicMock()
    runtime.settings.github.fallback_pat = None
    runtime.settings.github.app_id = None
    runtime.settings.github.app_private_key = None
    runtime.settings.mcp.public_url = None
    runtime.sessionmaker.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    runtime.sessionmaker.return_value.__aexit__ = AsyncMock(return_value=None)
    webhook = MagicMock()
    webhook.send_dict = AsyncMock()
    with (
        patch("daimon.adapters.slack.here.resolve_web_client", new=AsyncMock(return_value=client)),
        patch("daimon.adapters.slack.here.resolve_is_admin", new=AsyncMock(return_value=False)),
        patch(
            "daimon.adapters.slack.here.find_platform_principal", new=AsyncMock(return_value=None)
        ),
        patch(
            "daimon.adapters.slack.here.load_here_card",
            new=AsyncMock(return_value=_card("no_view")),
        ) as load,
        patch("daimon.adapters.slack.here.AsyncWebhookClient", return_value=webhook),
    ):
        await handle_here_command(
            runtime,
            {
                "team_id": "T1",
                "channel_id": "G1",
                "user_id": "U1",
                "response_url": "https://example.test/reply",
            },
        )
    assert load.await_args.kwargs["bot_can_view"] is False
    webhook.send_dict.assert_awaited_once_with(
        {
            "text": "Channel status",
            "attachments": [build_here_attachment(_card("no_view"))],
            "response_type": "ephemeral",
            "parse": "none",
        }
    )
    assert build_here_attachment(_card("no_view"))["color"] == "#ED4245"
    client.chat_postEphemeral.assert_not_awaited()


async def test_daemon_error_uses_error_card() -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(return_value={"channel": {"is_member": True}})
    client.users_conversations = AsyncMock(return_value={"channels": []})
    client.users_info = AsyncMock(return_value={"user": {}})
    runtime = MagicMock()
    runtime.settings.github.fallback_pat = None
    runtime.settings.github.app_id = None
    runtime.settings.github.app_private_key = None
    runtime.settings.mcp.public_url = None
    runtime.sessionmaker.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    runtime.sessionmaker.return_value.__aexit__ = AsyncMock(return_value=None)
    with (
        patch("daimon.adapters.slack.here.resolve_web_client", new=AsyncMock(return_value=client)),
        patch("daimon.adapters.slack.here.resolve_is_admin", new=AsyncMock(return_value=False)),
        patch(
            "daimon.adapters.slack.here.find_platform_principal", new=AsyncMock(return_value=None)
        ),
        patch(
            "daimon.adapters.slack.here.load_here_card",
            new=AsyncMock(side_effect=DaimonError("not available")),
        ),
        patch("daimon.adapters.slack.here.surface_command_error", new=AsyncMock()) as error,
    ):
        await handle_here_command(runtime, {"team_id": "T1", "channel_id": "C1", "user_id": "U1"})
    error.assert_awaited_once()
    assert isinstance(error.await_args.args[1], DaimonError)


@pytest.mark.parametrize("scope", ["server", "channel", "thread"])
async def test_approval_card_rejects_requester_before_write(
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    from daimon.adapters.slack import here
    from daimon.core.authz import Subject

    runtime = MagicMock()
    runtime.sessionmaker = MagicMock()
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    monkeypatch.setattr(here, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(
        here, "approval_admin", AsyncMock(return_value=Subject(platform_user_id="U1"))
    )
    await here.handle_here_approvals(
        runtime,
        {
            "team": {"id": "T1"},
            "channel": {"id": "C1"},
            "user": {"id": "U1"},
            "actions": [{"selected_option": {"value": f"{scope}:off"}}],
        },
    )
    runtime.sessionmaker.assert_not_called()
    assert "admin" in client.chat_postEphemeral.await_args.kwargs["text"]


@pytest.mark.parametrize("scope", ["server", "channel", "thread"])
@pytest.mark.parametrize("value", ["on", "off", "inherit"])
async def test_native_admin_controls_cover_all_approval_scopes(
    db_session_factory,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    value: str,
) -> None:
    import json

    from daimon.adapters.slack import here
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.authz import Subject
    from daimon.core.stores.access_policy import load_access_policy
    from daimon.core.stores.security_audit import list_events
    from daimon.core.tool_safety import ToolSafetyPolicy
    from daimon.testing.factories import make_tenant

    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id="T1")
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    runtime.settings.tool_safety = ToolSafetyPolicy(enabled=True, allow_gate_overrides=True)
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    client.views_open = AsyncMock()
    root = "1700000000.000001"
    client.conversations_replies = AsyncMock(return_value={"ok": True, "messages": [{"ts": root}]})
    monkeypatch.setattr(here, "resolve_web_client", AsyncMock(return_value=client))
    fresh = AsyncMock(return_value=Subject(is_admin=True, platform_user_id="native-admin"))
    monkeypatch.setattr(here, "approval_admin", fresh)
    await here.handle_here_approvals(
        runtime,
        {
            "team": {"id": "T1"},
            "user": {"id": "native-admin"},
            "channel": {"id": "C1"},
            "trigger_id": "native-trigger",
            "actions": [{"selected_option": {"value": f"{scope}:{value}"}}],
        },
    )
    if scope == "thread":
        async with db_session_factory() as session:
            assert await load_access_policy(session, tenant_id=tenant.id) == TenantAccessPolicy()
            assert not await list_events(session, tenant_id=tenant.id)
        modal = client.views_open.await_args.kwargs["view"]
        assert modal["callback_id"] == "here_approvals_thread"
        await here.handle_here_approval_submission(
            runtime,
            {
                "team": {"id": "T1"},
                "user": {"id": "native-admin"},
                "view": {
                    "private_metadata": modal["private_metadata"],
                    "state": {
                        "values": {
                            "thread": {
                                "root": {
                                    "value": "https://workspace.slack.com/archives/C1/p1700000000000001"
                                }
                            }
                        }
                    },
                },
            },
        )
        assert fresh.await_count == 2
        assert json.loads(modal["private_metadata"])["team"] == "T1"
    else:
        fresh.assert_awaited_once()
    target = {"server": None, "channel": "C1", "thread": f"C1:{root}"}[scope]
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
        events = await list_events(session, tenant_id=tenant.id)
    assert (
        policy.approvals
        if target is None
        else policy.channel_rules.get(target, ChannelRule()).approvals
    ) == value
    assert len(events) == 1 and events[0].platform_user_id == "native-admin"


@pytest.mark.parametrize(
    "failure", ["demoted", "foreign_team", "foreign_channel", "missing_root", "reply"]
)
async def test_thread_approval_submission_refuses_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import json

    from daimon.adapters.slack import here
    from daimon.core.authz import Subject

    runtime = MagicMock()
    runtime.sessionmaker = MagicMock()
    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    client.conversations_replies = AsyncMock(return_value={"ok": True, "messages": []})
    if failure == "reply":
        client.conversations_replies.return_value = {
            "ok": True,
            "messages": [{"ts": "1700000000.000001", "thread_ts": "1600000000.000001"}],
        }
    monkeypatch.setattr(here, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(
        here,
        "approval_admin",
        AsyncMock(
            return_value=Subject(is_admin=failure != "demoted", platform_user_id="requester")
        ),
    )
    await here.handle_here_approval_submission(
        runtime,
        {
            "team": {"id": "T1"},
            "user": {"id": "requester"},
            "view": {
                "private_metadata": json.dumps(
                    {
                        "team": "T2" if failure == "foreign_team" else "T1",
                        "channel": "C1",
                        "value": "off",
                    }
                ),
                "state": {
                    "values": {
                        "thread": {
                            "root": {
                                "value": (
                                    "https://workspace.slack.com/archives/C2/p1700000000000001"
                                    if failure == "foreign_channel"
                                    else "1700000000.000001"
                                )
                            }
                        }
                    }
                },
            },
        },
    )
    runtime.sessionmaker.assert_not_called()
    if failure in {"demoted", "foreign_team", "foreign_channel"}:
        client.conversations_replies.assert_not_awaited()


def test_here_offers_explicit_server_channel_and_thread_choices() -> None:
    card = _card("channel").model_copy(update={"approval_gate_admin": True})
    blocks = build_here_attachment(card)["blocks"]
    select = next(block for block in blocks if block["type"] == "actions")["elements"][0]
    assert {option["value"] for option in select["options"]} == {
        f"{scope}:{value}"
        for scope in ("server", "channel", "thread")
        for value in ("on", "off", "inherit")
    }
