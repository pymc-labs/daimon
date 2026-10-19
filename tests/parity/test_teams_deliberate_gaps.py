"""Executable record of Teams' deliberate gaps against Discord and Slack.

A Teams group chat is one session; it takes no
files, not even image previews, and routines and agent tools do not reach it.
Neither Bot Framework nor
app-only Graph reads a 1:1 chat back, so the boot sweep finds a card whose post
lost its id only in a channel thread; in a 1:1 chat that intent retires
untouched (`packages/adapters/teams/tests/test_boot_sweep.py`).

The rest follows from what a Teams bot can do (see `docs/teams.md`): a
private message in a channel expires after 24 hours, so `setup` and `support`,
whose flows outlast that, answer in the 1:1 chat while other commands answer
privately in the thread, and a Connect GitHub link goes to the requester's 1:1
chat rather than the thread; the 1:1 chat has no threads, so a setup conversation is keyed inside it, and since panels live
there only a server admin can mint a coding-tool token; files in a channel work
only once a tenant admin consents to the application permission `Files.ReadWrite.All`, because no
team-scoped permission reaches SharePoint; a dialog has no file input, so a `.env` file is pasted
rather than uploaded; and once a dialog closes nothing private reaches the
requester, so a GitHub token is checked against its repo before the request is
spent (Discord and Slack spend it first, then say so privately; the Teams
adapter's credential tests assert the unspent request). Removing the app from a
team forgets that team and archives no tenant: a deployment serves one
organisation. A completion ping is an @mention plus Discord's 👀 and ✅ on the
asker's message, and Ask a person is a button on the card below the answer (and
the `support` form); a post_wizard form has no step images. An agent edits or deletes its own
posts but closes no thread (`delete_thread`, `archive_thread`).
Thread participation shares Discord's gates
(`test_thread_participation_platforms.py`) but needs Graph, so without the
consent a followed thread stays mention-only; and where Discord's unprompted
card appears once there is output, Teams posts only the answer and its controls,
so there is no running card or Cancel button. The `here` card counts only the place it was
typed in as one the caller can see: naming their other channels would take
Graph lookups per team, so an agent rule's other channels stay unnamed. Where Teams
will not deliver `memory` privately in the channel it answers in the 1:1 chat,
so for a channel whose readers are limited it says why instead, as `/dm-me`
refuses to carry such a channel's work out. Teams bot commands are typed
words (the manifest's command lists and `/` only fill them in), so a message that is
exactly a command word runs it: a reply of just `here` gets the card, where
Slack and Discord take `here` as a slash command no reply can trip. The word
stays `here` to match them.

Teams opens no 1:1 chat across organisations: people from another one are
answered as members in channels and group chats, but a 1:1 chat naming their
tenant is refused, and a command answered in the 1:1 chat tells them so.

No platform parametrization, no database -- this is a scope check.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import yaml
from daimon.adapters.mcp.tools import github_connect as connect_tool
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.teams import here, installations, memory
from daimon.adapters.teams.attachments import (
    ChannelMedia,
    InboundFile,
    SharedFile,
    prepare_attachments,
)
from daimon.adapters.teams.commands import CommandContext, parse_command
from daimon.adapters.teams.credential_requests import credential_form
from daimon.adapters.teams.help import COMMAND_HELP
from daimon.adapters.teams.identity import TeamsInbound, parse_inbound
from daimon.adapters.teams.installations import TeamInstalls
from daimon.adapters.teams.lifecycle import TeamsTurnLifecycle
from daimon.adapters.teams.private_replies import IN_THREAD
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.credential_requests import ENV_FILE_TARGET
from daimon.core.github_connect_cards import build_connect_card
from daimon.core.here_card import assemble_here_card
from daimon.core.stores.domain import CredentialRequestRow
from daimon.core.teams_threads import conversation_of, new_setup_thread_id
from daimon.core.turn.state import TextBlock, TurnState
from microsoft_teams.api import MessageActivity, MessageActivityInput, SentActivity
from microsoft_teams.api.activities.install_update import UninstalledActivity

REPO_ROOT = Path(__file__).resolve().parents[2]
TENANT = "00000000-0000-0000-0000-000000000001"
CONTENT_URL = "https://example.sharepoint.com/sites/team/Shared%20Documents/q3.xlsx"


def test_a_teams_group_chat_is_one_session() -> None:
    activity = MessageActivity.model_validate(
        {
            "type": "message",
            "id": "a-1",
            "channelId": "msteams",
            "serviceUrl": "https://smba.example.test",
            "from": {"id": "29:u", "aadObjectId": "00000000-0000-0000-0000-000000000002"},
            "recipient": {"id": "28:bot"},
            "conversation": {
                "id": "19:g@thread.v2",
                "conversationType": "groupChat",
                "tenantId": TENANT,
            },
            "channelData": {"tenant": {"id": TENANT}},
            "text": "<at>daimon</at> hi",
            "entities": [
                {"type": "mention", "text": "<at>daimon</at>", "mentioned": {"id": "28:bot"}}
            ],
        }
    )
    inbound = parse_inbound(activity, configured_tenant=TENANT, service_url=None)
    assert isinstance(inbound, TeamsInbound)
    assert (inbound.kind, inbound.conversation_id) == ("group", "19:g@thread.v2")


def test_a_teams_one_to_one_chat_across_organisations_is_refused() -> None:
    def _from_elsewhere(conversation_type: str) -> MessageActivity:
        return MessageActivity.model_validate(
            {
                "type": "message",
                "id": "a-1",
                "channelId": "msteams",
                "serviceUrl": "https://smba.example.test",
                "from": {
                    "id": "29:u",
                    "aadObjectId": "00000000-0000-0000-0000-000000000002",
                    "tenantId": str(uuid.UUID(int=99)),
                },
                "recipient": {"id": "28:bot"},
                "conversation": {
                    "id": "a:chat" if conversation_type == "personal" else "19:g@thread.v2",
                    "conversationType": conversation_type,
                    "tenantId": TENANT,
                },
                "channelData": {"tenant": {"id": TENANT}},
                "text": "<at>daimon</at> hi",
                "entities": [
                    {"type": "mention", "text": "<at>daimon</at>", "mentioned": {"id": "28:bot"}}
                ],
            }
        )

    group = parse_inbound(_from_elsewhere("groupChat"), configured_tenant=TENANT, service_url=None)
    assert isinstance(group, TeamsInbound) and group.is_foreign, "answered, as a member"
    personal = parse_inbound(
        _from_elsewhere("personal"), configured_tenant=TENANT, service_url=None
    )
    assert not isinstance(personal, TeamsInbound), "Teams opens no 1:1 chat across organisations"


def test_teams_channels_and_group_chats_offer_only_the_commands_answered_there() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "docs/teams-app-manifest.yaml").read_text())
    offered = {
        command["title"]
        for cl in manifest["bots"][0]["commandLists"]
        if "team" in cl["scopes"] and "groupChat" in cl["scopes"]
        for command in cl["commands"]
        if command.get("type", "basic") == "basic"
    }
    assert offered == IN_THREAD and not offered & {"setup", "support"}, (
        "setup and support keep their flows in the 1:1 chat, past a private message's 24 hours"
    )


def test_teams_commands_are_bare_words_and_here_is_one() -> None:
    names = {name: AsyncMock() for name in COMMAND_HELP}
    assert parse_command("here", names) == ("here", ""), (
        "a message that is exactly `here` runs the command, named as Slack's and Discord's are; "
        "they are slash commands, so no reply there trips one"
    )
    assert parse_command("here is the file", names) is None, "anything longer is a turn"


async def test_the_teams_here_card_sees_only_the_place_it_was_typed_in() -> None:
    channel = "19:ops@thread.tacv2"
    asked = TeamsInbound(
        kind="channel",
        entra_tenant_id=TENANT,
        user_id=str(uuid.UUID(int=2)),
        conversation_id=f"{channel};messageid=1",
        channel_id=channel,
        activity_id="1",
        text="here",
        service_url=None,
    )
    card = assemble_here_card(
        channel_id=channel,
        agent_name=None,
        tier=None,
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(),
        details=None,
    )
    runtime = MagicMock()
    runtime.settings.mcp.public_url = None
    context = CommandContext(
        inbound=dataclasses.replace(
            asked, kind="dm", conversation_id="a:chat", channel_id="a:chat"
        ),
        tenant_id=uuid.uuid4(),
        args="",
        is_admin=False,
        runtime=runtime,
        send=AsyncMock(),
        asked_in=asked,
    )
    with (
        patch.object(here, "find_platform_principal", AsyncMock(return_value=None)),
        patch.object(here, "load_here_card", AsyncMock(return_value=card)) as load,
    ):
        await here.show_here(context)
    assert load.await_args.kwargs["visible_channel_ids"] == {channel}, (
        "Teams names no other channel the caller may see; if it learned to, replace this record"
    )


async def test_teams_memory_from_a_limited_readers_channel_is_not_shown_in_the_chat() -> None:
    channel = "19:vault@thread.tacv2"
    asked = TeamsInbound(
        kind="channel",
        entra_tenant_id=TENANT,
        user_id=str(uuid.UUID(int=2)),
        conversation_id=f"{channel};messageid=1",
        channel_id=channel,
        activity_id="1",
        text="memory",
        service_url=None,
    )
    send = AsyncMock()
    context = CommandContext(
        inbound=dataclasses.replace(
            asked, kind="dm", conversation_id="a:chat", channel_id="a:chat"
        ),
        tenant_id=uuid.uuid4(),
        args="",
        is_admin=False,
        runtime=MagicMock(),
        send=send,
        asked_in=asked,
    )
    policy = TenantAccessPolicy(channel_rules={channel: ChannelRule(readers="inside")})
    with (
        patch.object(memory, "load_access_policy", AsyncMock(return_value=policy)),
        patch.object(memory, "get_channel_memory_store", AsyncMock()) as store,
    ):
        await memory.show_memory(context)
    store.assert_not_awaited()
    assert json.dumps(memory.KEPT_INSIDE)[1:-1] in send.await_args.args[0].model_dump_json(), (
        "answered in the 1:1 chat, a limited channel's memory stays inside it"
    )


def test_a_teams_setup_conversation_lives_inside_its_chat() -> None:
    chat = "a:chat-1"
    assert conversation_of(new_setup_thread_id(chat)) == chat, (
        "a 1:1 chat has no threads, so its setup conversation is a key within the chat"
    )


def test_teams_channel_files_need_a_tenant_permission_not_a_manifest_permission() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "docs/teams-app-manifest.yaml").read_text())
    granted = manifest["authorization"]["permissions"]["resourceSpecific"]
    assert [p["name"] for p in granted] == [
        "ChannelMessage.Read.Group",
        "TeamMember.Read.Group",
        "ChatMessage.Read.Chat",
        "TeamsActivity.Send.Group",
    ], "no team permission reaches SharePoint, so files need admin consent to Files.ReadWrite.All"


async def test_a_teams_channel_file_without_the_files_permission_is_named_never_fetched() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request for a channel file: {request.url.host}")

    async def token() -> str:
        return "t"

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
        prepared = await prepare_attachments(
            http,
            [InboundFile("embedded_file", "file")],
            bot_token=token,
            service_url=None,
            channel_media=ChannelMedia(files=(SharedFile("q3.xlsx", CONTENT_URL, refused=True),)),
            graph_token=token,
        )
    assert "`q3.xlsx` was shared but can't be opened: daimon has no access" in prepared.prefix, (
        "unresolved (no Files.ReadWrite.All), a channel file is only named; Discord and Slack fetch it"
    )


def test_a_teams_env_file_is_pasted_not_uploaded() -> None:
    now = datetime(2026, 5, 14, tzinfo=UTC)
    row = CredentialRequestRow(
        token="t",
        kind="env_file",
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        target=ENV_FILE_TARGET,
        mcp_server_url=None,
        requester_platform_user_id="u",
        channel_id="a:chat-1",
        idempotency_key=uuid.uuid4(),
        created_at=now,
        expires_at=now,
        used_at=None,
    )
    card = credential_form(row).model_dump(by_alias=True, exclude_none=True)
    body = card["task"]["value"]["card"]["content"]["body"]
    inputs = [item for item in body if str(item.get("type", "")).startswith("Input.")]
    assert [(i["type"], i.get("isMultiline")) for i in inputs] == [("Input.Text", True)], (
        "a dialog has no file input; if Teams gains one, replace this record"
    )


async def test_an_unprompted_teams_turn_shows_no_running_card() -> None:
    sent: list[MessageActivityInput] = []

    class _Sender:
        async def send(
            self, conversation_id: str, activity: MessageActivityInput, *, service_url: str | None
        ) -> SentActivity:
            sent.append(activity)
            return SentActivity(id=f"m-{len(sent)}", activity_params=activity)

    lifecycle = TeamsTurnLifecycle(
        sender=_Sender(),
        conversation_id="19:a@thread.tacv2;messageid=1",
        service_url=None,
        cancel_key="k",
        clock=lambda: 0.0,
        unprompted=True,
    )
    await lifecycle.post_initial()
    await lifecycle.on_render(TurnState(content=[TextBlock(kind="text", text="draft")]))
    assert sent == [], "no card while it runs, so no Cancel button"
    await lifecycle.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="Done.")]))
    assert [a.text for a in sent] == ["Done.", None], "the answer, then its controls card"


async def test_removing_the_teams_app_forgets_the_team_and_archives_nothing() -> None:
    removal = UninstalledActivity.model_validate(
        {
            "type": "installationUpdate",
            "action": "remove",
            "id": "a-1",
            "channelId": "msteams",
            "from": {"id": "29:u"},
            "recipient": {"id": "28:bot"},
            "conversation": {"id": "19:general@thread.tacv2", "tenantId": TENANT},
            "channelData": {"team": {"id": "19:general@thread.tacv2"}, "tenant": {"id": TENANT}},
        }
    )
    session = AsyncMock()
    sessionmaker = MagicMock()
    sessionmaker.begin.return_value.__aenter__.return_value = session
    installs = TeamInstalls(
        sessionmaker, MagicMock(), tenant_id=uuid.uuid4(), entra_tenant_id=TENANT
    )
    with patch.object(installations, "delete_teams_installation", new_callable=AsyncMock) as forget:
        await installs.on_uninstall(cast(Any, SimpleNamespace(activity=removal)))
    forget.assert_awaited_once()
    assert session.method_calls == [], (
        "only the team's row goes; if removal ever archives, run the uninstall parity scenario"
    )


async def test_a_teams_connect_github_link_goes_to_the_requesters_one_to_one_chat() -> None:
    """Slack shows the link only to the requester in the thread; Teams keeps it in the 1:1."""
    posts: list[str] = []

    def bot_framework(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if "/members/" in request.url.path:
            return httpx.Response(200, json={"id": "29:u", "aadObjectId": "aad-u"})
        posts.append(request.url.path.split("/v3/", 1)[1])
        return httpx.Response(200, json={"id": "a:dm-1"})

    client = TeamsBotClient(
        httpx.AsyncClient(transport=httpx.MockTransport(bot_framework)),
        client_id="app",
        client_secret="secret",
        tenant_id=TENANT,
    )
    card = build_connect_card(
        agent_name="Agent",
        identity_enabled=False,
        avatar_url=None,
        public_base_url="https://mcp.example.test",
    )
    runtime = SimpleNamespace(teams_client=client, session_factory=None, settings=None)
    auth = SimpleNamespace(tenant_id=uuid.uuid4(), external_id=TENANT, platform_user_id="aad-u")
    with patch.object(connect_tool, "resolve_connect_card", AsyncMock(return_value=card)):
        await connect_tool._post_teams_connect_card(  # pyright: ignore[reportPrivateUsage]
            cast(Any, runtime),
            cast(Any, auth),
            channel_id="19:c@thread.tacv2",
            url="https://mcp.example.test/oauth/github/connect/secret",
            agent_name="Agent",
        )
    assert posts == ["conversations", "conversations/a:dm-1/activities"], (
        "the link goes to a 1:1 chat, never the channel; if it moves to a private reply "
        "in the thread, replace this record"
    )
