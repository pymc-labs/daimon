"""Executable record of Teams' deliberate gaps against Discord and Slack.

Teams group chats have no thread to scope a session to, so the adapter refuses
them and the manifest does not offer the scope. Neither Bot Framework nor
app-only Graph reads a 1:1 chat back, so the boot sweep finds a card whose post
lost its id only in a channel thread; in a 1:1 chat that intent retires
untouched (`packages/adapters/teams/tests/test_boot_sweep.py`).

The rest follows from what a Teams bot can do (see `docs/teams.md`): there is
no message only its sender sees, so a command typed in a channel is answered in
the 1:1 chat and the manifest offers commands there only; the 1:1 chat has no
threads, so a setup conversation is keyed inside it, and since panels live
there a channel admin cannot mint a coding-tool token, which must be minted
inside their channel; files in a channel work only once a tenant admin grants
the app the team's SharePoint site (`Sites.Selected`), because no team-scoped
permission reaches it; a dialog has no file input, so a `.env` file is pasted
rather than uploaded; and once a dialog closes nothing private reaches the
requester, so a GitHub token is checked against its repo before the request is
spent (Discord and Slack spend it first, then say so privately; the Teams
adapter's credential tests assert the unspent request). Removing the app from a
team forgets that team and archives no tenant: a deployment serves one
organisation. A bot cannot react, so a completion ping is an @mention alone and
human support is the `support` form; a post_wizard form has no step images.
Thread participation shares Discord's gates
(`test_thread_participation_platforms.py`) but needs Graph, so without the
consent a followed thread stays mention-only; and where Discord's unprompted
card appears once there is output, Teams posts only the answer, so there is no
running card or Cancel button.

No platform parametrization, no database -- this is a scope check.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import yaml
from daimon.adapters.teams import installations
from daimon.adapters.teams.attachments import (
    ChannelMedia,
    InboundFile,
    SharedFile,
    prepare_attachments,
)
from daimon.adapters.teams.credential_requests import credential_form
from daimon.adapters.teams.identity import GROUP_CHAT_UNSUPPORTED, Refusal, parse_inbound
from daimon.adapters.teams.installations import TeamInstalls
from daimon.adapters.teams.lifecycle import TeamsTurnLifecycle
from daimon.core.credential_requests import ENV_FILE_TARGET
from daimon.core.stores.domain import CredentialRequestRow
from daimon.core.teams_threads import conversation_of, new_setup_thread_id
from daimon.core.turn.state import TextBlock, TurnState
from microsoft_teams.api import MessageActivity, MessageActivityInput, SentActivity
from microsoft_teams.api.activities.install_update import UninstalledActivity

REPO_ROOT = Path(__file__).resolve().parents[2]
TENANT = "00000000-0000-0000-0000-000000000001"
CONTENT_URL = "https://example.sharepoint.com/sites/team/Shared%20Documents/q3.xlsx"


def test_teams_refuses_group_chats() -> None:
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
            "text": "hi",
        }
    )
    refusal = parse_inbound(activity, configured_tenant=TENANT, service_url=None)
    assert refusal == Refusal(GROUP_CHAT_UNSUPPORTED), (
        "Teams group chats are refused on purpose; if they gained support, replace this record"
    )


def test_teams_manifest_does_not_offer_group_chats() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "docs/teams-app-manifest.yaml").read_text())
    assert all("groupChat" not in bot["scopes"] for bot in manifest["bots"]), (
        "the manifest must not offer a scope the adapter refuses"
    )


def test_teams_commands_are_offered_only_in_the_one_to_one_chat() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "docs/teams-app-manifest.yaml").read_text())
    assert all(cl["scopes"] == ["personal"] for cl in manifest["bots"][0]["commandLists"]), (
        "command replies can carry account details; one typed in a channel is answered in the 1:1"
    )


def test_a_teams_setup_conversation_lives_inside_its_chat() -> None:
    chat = "a:chat-1"
    assert conversation_of(new_setup_thread_id(chat)) == chat, (
        "a 1:1 chat has no threads, so its setup conversation is a key within the chat"
    )


def test_teams_channel_files_need_a_site_grant_not_a_manifest_permission() -> None:
    manifest = yaml.safe_load((REPO_ROOT / "docs/teams-app-manifest.yaml").read_text())
    granted = manifest["authorization"]["permissions"]["resourceSpecific"]
    assert [p["name"] for p in granted] == ["ChannelMessage.Read.Group"], (
        "no team permission reaches SharePoint, so files need an admin's Sites.Selected grant"
    )


async def test_a_teams_channel_file_without_a_site_grant_is_named_never_fetched() -> None:
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
        "unresolved (no site grant), a channel file is only named; Discord and Slack fetch it"
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
    assert [a.text for a in sent] == ["Done."], "only the answer is posted"


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
