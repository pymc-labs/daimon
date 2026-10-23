"""Slack /here resolves the card and posts it only to the invoker."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from daimon.adapters.slack.here import (  # pyright: ignore[reportPrivateUsage]
    _setter_display_name,  # pyright: ignore[reportPrivateUsage]
    _visible_channel_ids,  # pyright: ignore[reportPrivateUsage]
    build_here_attachment,
    handle_here_command,
)
from daimon.adapters.slack.outsiders import SLACK_GUESTS_UNCHECKED
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.errors import DaimonError
from daimon.core.here_card import CredentialStatus, HereCard, render_here_card_text
from daimon.core.outsiders import OUTSIDER_CHECK_SKIPPED
from slack_sdk.errors import SlackApiError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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


@pytest.mark.parametrize("connect", [False, True])
async def test_here_posts_ephemeral_card(
    connect: bool, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(
        return_value={"channel": {"is_private": False, "is_ext_shared": connect}}
    )
    client.users_conversations = AsyncMock(return_value={"channels": []})
    client.users_info = AsyncMock(return_value={"user": {}})
    client.chat_postEphemeral = AsyncMock()
    runtime = MagicMock()
    runtime.settings.github.fallback_pat = None
    runtime.settings.github.app_id = None
    runtime.settings.github.app_private_key = None
    runtime.settings.mcp.public_url = None
    runtime.sessionmaker = db_session_factory
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
    assert load.await_args is not None
    assert load.await_args.kwargs["bot_can_view"] is False
    assert load.await_args.kwargs["thread_id"] is None
    assert load.await_args.kwargs["channel_level_only"] is True
    assert callable(load.await_args.kwargs["resolve_setter_display"])
    client.chat_postEphemeral.assert_awaited_once_with(
        channel="C1",
        user="U1",
        text="Channel status",
        attachments=[
            build_here_attachment(
                _card("channel").model_copy(
                    update={
                        "outsiders_can_view": True if connect else None,
                        "outsider_note": SLACK_GUESTS_UNCHECKED,
                        "text": render_here_card_text(
                            _card("channel").model_copy(
                                update={
                                    "outsiders_can_view": True if connect else None,
                                    "outsider_note": SLACK_GUESTS_UNCHECKED,
                                }
                            )
                        ),
                    }
                )
            )
        ],
        parse="none",
        link_names=False,
    )


async def test_private_channel_without_bot_membership_returns_card_by_response_url(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
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
    runtime.sessionmaker = db_session_factory
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
        patch(
            "daimon.adapters.slack.here.load_access_policy",
            new=AsyncMock(return_value=TenantAccessPolicy()),
        ),
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
    assert load.await_args is not None
    assert load.await_args.kwargs["bot_can_view"] is False
    webhook.send_dict.assert_awaited_once_with(
        {
            "text": "Channel status",
            "attachments": [
                build_here_attachment(
                    _card("no_view").model_copy(
                        update={
                            "outsider_note": f"{OUTSIDER_CHECK_SKIPPED}\n{SLACK_GUESTS_UNCHECKED}",
                            "text": render_here_card_text(
                                _card("no_view").model_copy(
                                    update={
                                        "outsider_note": f"{OUTSIDER_CHECK_SKIPPED}\n{SLACK_GUESTS_UNCHECKED}"
                                    }
                                )
                            ),
                        }
                    )
                )
            ],
            "response_type": "ephemeral",
            "parse": "none",
        }
    )
    assert build_here_attachment(_card("no_view"))["color"] == "#ED4245"
    client.chat_postEphemeral.assert_not_awaited()


async def test_daemon_error_uses_error_card(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client = MagicMock()
    client.conversations_info = AsyncMock(
        return_value={"channel": {"is_member": True, "is_ext_shared": False}}
    )
    client.users_conversations = AsyncMock(return_value={"channels": []})
    client.users_info = AsyncMock(return_value={"user": {}})
    runtime = MagicMock()
    runtime.settings.github.fallback_pat = None
    runtime.settings.github.app_id = None
    runtime.settings.github.app_private_key = None
    runtime.settings.mcp.public_url = None
    runtime.sessionmaker = db_session_factory
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
    assert error.await_args is not None
    assert isinstance(error.await_args.args[1], DaimonError)


@pytest.mark.parametrize("admin", [True, False])
@pytest.mark.parametrize("readers", ["any", "inside", "own"])
def test_outsider_warning_and_admin_fix(admin: bool, readers: str) -> None:
    card = _card("channel").model_copy(
        update={
            "outsiders_can_view": True,
            "effective_readers": readers,
            "outsider_note": SLACK_GUESTS_UNCHECKED,
        }
    )
    rendered = str(build_here_attachment(card, is_admin=admin))
    assert ("outside your organisation" in rendered) is (readers != "own")
    assert (SLACK_GUESTS_UNCHECKED in rendered) is (readers != "own")
    assert ("Make confidential" in rendered) is (admin and readers != "own")


def test_unchecked_guest_limitation_is_visible() -> None:
    card = _card("no_agent").model_copy(update={"outsider_note": SLACK_GUESTS_UNCHECKED})
    assert SLACK_GUESTS_UNCHECKED in str(build_here_attachment(card))


@pytest.mark.parametrize("admin", [True, False])
async def test_confidential_click_rechecks_admin(admin: bool) -> None:
    from daimon.adapters.slack.here import handle_make_confidential

    client = MagicMock()
    client.chat_postEphemeral = AsyncMock()
    runtime = MagicMock()
    with (
        patch("daimon.adapters.slack.here.resolve_web_client", AsyncMock(return_value=client)),
        patch("daimon.adapters.slack.here.resolve_is_admin", AsyncMock(return_value=admin)),
        patch(
            "daimon.adapters.slack.agent_setup.rules.change_rule", AsyncMock(return_value="saved")
        ) as change,
    ):
        await handle_make_confidential(
            runtime,
            {
                "team": {"id": "T1"},
                "channel": {"id": "C1"},
                "user": {"id": "U1"},
                "actions": [{"value": "C_OTHER"}],
            },
        )
    assert change.await_count == int(admin)
    if admin:
        assert change.await_args.kwargs["channel_id"] == "C1"
        assert change.await_args.kwargs["readers"] == "own"
    else:
        assert "Only a workspace admin" in client.chat_postEphemeral.await_args.kwargs["text"]


def _review_runtime(db_session_factory: async_sessionmaker[AsyncSession]) -> MagicMock:
    runtime = MagicMock()
    runtime.settings.github_app.app_id = None
    runtime.settings.mcp.public_url = None
    runtime.sessionmaker = db_session_factory
    return runtime


def _review_client(*, connect: bool) -> MagicMock:
    client = MagicMock()
    client.conversations_info = AsyncMock(
        return_value={"channel": {"is_member": True, "is_ext_shared": connect}}
    )
    client.users_conversations = AsyncMock(return_value={"channels": []})
    client.users_info = AsyncMock(
        return_value={"user": {"is_admin": True, "is_restricted": False, "team_id": "T1"}}
    )
    client.chat_postEphemeral = AsyncMock()
    return client


def _wire_review_here(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> AsyncMock:
    from daimon.adapters.slack import here as module
    from daimon.core.access_policy import TenantAccessPolicy

    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(module, "find_platform_principal", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "load_access_policy", AsyncMock(return_value=TenantAccessPolicy()))
    load = AsyncMock(return_value=_card("channel"))
    monkeypatch.setattr(module, "load_here_card", load)
    return load


async def test_whole_here_concurrent_connect_renders_only_check_connect(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import asyncio

    client, runtime = _review_client(connect=True), _review_runtime(db_session_factory)
    _wire_review_here(monkeypatch, client)
    payload = {"team_id": "T1", "channel_id": "C1", "user_id": "U_CALLER"}
    await asyncio.gather(*(handle_here_command(runtime, payload) for _ in range(3)))
    assert client.conversations_info.await_count == 3, "one live Connect fact per render"
    assert client.users_info.await_count == 6, (
        "only live requester/admin lookups, never guest lookups"
    )
    assert all(call.kwargs["user"] == "U_CALLER" for call in client.users_info.await_args_list)
    assert client.chat_postEphemeral.await_count == 3
    for call in client.chat_postEphemeral.await_args_list:
        assert "outside your organisation" in str(call.kwargs["attachments"])


async def test_whole_here_repeated_renders_use_fresh_sharing_facts(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, runtime = _review_client(connect=False), _review_runtime(db_session_factory)
    _wire_review_here(monkeypatch, client)
    for shared in (False, True, False):
        client.conversations_info.return_value = {
            "channel": {"is_member": True, "is_org_shared": shared}
        }
        await handle_here_command(
            runtime, {"team_id": "T1", "channel_id": "C1", "user_id": "U_CALLER"}
        )
    assert client.conversations_info.await_count == 3
    for call, shared in zip(
        client.chat_postEphemeral.await_args_list, (False, True, False), strict=True
    ):
        attachment = str(call.kwargs["attachments"])
        assert ("outside your organisation" in attachment) is shared
        assert ("Make confidential" in attachment) is shared
        assert SLACK_GUESTS_UNCHECKED in attachment
    assert all(call.kwargs["user"] == "U_CALLER" for call in client.users_info.await_args_list)


@pytest.mark.parametrize("phase", ["requester", "listing"])
@pytest.mark.parametrize("readers", ["any", "own"])
async def test_whole_here_retains_connect_warning_on_metadata_failure(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    phase: str,
    readers: str,
) -> None:
    from daimon.adapters.slack import here as module
    from daimon.core.access_policy import TenantAccessPolicy

    client, runtime = _review_client(connect=True), _review_runtime(db_session_factory)
    load = _wire_review_here(monkeypatch, client)
    rule = ChannelRule(readers="own", writers="own") if readers == "own" else ChannelRule()
    monkeypatch.setattr(
        module,
        "load_access_policy",
        AsyncMock(return_value=TenantAccessPolicy(channel_rules={"C1": rule})),
    )
    rate_limit = SlackApiError("ratelimited", MagicMock(data={"error": "ratelimited"}))
    if phase == "requester":
        client.users_info.side_effect = rate_limit
    else:
        client.users_conversations.side_effect = rate_limit
    await handle_here_command(runtime, {"team_id": "T1", "channel_id": "C1", "user_id": "U_CALLER"})
    load.assert_not_awaited()
    module.find_platform_principal.assert_not_awaited()
    client.chat_postEphemeral.assert_awaited_once()
    attachment = client.chat_postEphemeral.await_args.kwargs["attachments"]
    assert ("outside your organisation" in str(attachment)) is (readers != "own")
    assert "Some channel details are unavailable" in str(attachment)
    assert "ResearchBot" not in str(attachment)
    assert "Make confidential" not in str(attachment), (
        "no admin actions survive incomplete metadata"
    )
    assert "actions" not in str(attachment)


async def test_whole_here_repeated_rate_limits_preserve_known_outside_evidence(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, runtime = _review_client(connect=True), _review_runtime(db_session_factory)
    load = _wire_review_here(monkeypatch, client)
    client.users_info.side_effect = SlackApiError(
        "ratelimited", MagicMock(data={"error": "ratelimited"})
    )
    for _ in range(3):
        await handle_here_command(
            runtime, {"team_id": "T1", "channel_id": "C1", "user_id": "U_CALLER"}
        )
    load.assert_not_awaited()
    assert client.conversations_info.await_count == 3
    for call in client.chat_postEphemeral.await_args_list:
        attachment = str(call.kwargs["attachments"])
        assert "outside your organisation" in attachment
        assert SLACK_GUESTS_UNCHECKED in attachment
        assert "Make confidential" not in attachment
        assert "ResearchBot" not in attachment


async def test_whole_here_info_failure_uses_outsider_note_and_hides_details(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    client, runtime = _review_client(connect=True), _review_runtime(db_session_factory)
    load = _wire_review_here(monkeypatch, client)
    client.conversations_info.side_effect = SlackApiError(
        "ratelimited", MagicMock(data={"error": "ratelimited"})
    )
    await handle_here_command(runtime, {"team_id": "T1", "channel_id": "C1", "user_id": "U_CALLER"})
    load.assert_not_awaited()
    attachment = str(client.chat_postEphemeral.await_args.kwargs["attachments"])
    assert "Outsider check skipped" in attachment
    assert "Guest check skipped" not in attachment
    assert "ResearchBot" not in attachment
    assert "Make confidential" not in attachment


async def test_approval_card_rejects_requester_before_write(
    monkeypatch: pytest.MonkeyPatch,
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
            "actions": [{"selected_option": {"value": "off"}}],
        },
    )
    runtime.sessionmaker.assert_not_called()
    assert "admin" in client.chat_postEphemeral.await_args.kwargs["text"]
