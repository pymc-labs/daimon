"""Teams send_message, create_thread, the GitHub App link and credential cards.

The bot is the sender, so a caller could otherwise speak through it in a
conversation they cannot see: the requester's Entra id must be on the target's
roster (the channel's, for a thread), and any failure to confirm that refuses
the post, mirroring the Slack access check.

The credential-request card is rendered by `daimon.core.posted_controls`, the
same renderer the Teams adapter edits it with once the dialog is submitted.
"""

from __future__ import annotations

import re

import httpx
import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.github_app_auth import build_app_install_url
from daimon.core.posted_controls import CardState, RefusalReason
from daimon.core.posted_controls.teams_card import (
    build_adaptive_card,
    card_for_request,
)
from daimon.core.stores.domain import CredentialRequestRow
from daimon.core.teams_threads import conversation_of
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

_log = structlog.get_logger()

# Teams caps a message at about 28 KB; 6,000 four-byte characters stay under it.
_MAX_CONTENT_CHARS = 6_000
# Conversation ids (a:…, 19:…@thread.tacv2;messageid=…) go into URL paths.
_CONVERSATION_ID = re.compile(r"[\w:@.;=+-]+")
_NOT_A_MEMBER = (
    "you are not a member of that Teams conversation, or daimon cannot see it, "
    "so nothing was posted"
)


class TeamsMessageRow(BaseModel):
    """A message daimon posted in Teams."""

    model_config = ConfigDict(frozen=True)

    conversation_id: str
    """Where it landed; pass it to send_message to reply in that thread."""
    activity_id: str
    text: str


def _conversation_id(value: str) -> str:
    if not _CONVERSATION_ID.fullmatch(value):
        raise ToolError("channel_id must be a Teams conversation id, e.g. a:… or 19:…@thread.tacv2")
    return conversation_of(value)


def _check_text(content: str) -> None:
    if not content.strip():
        raise ToolError("content must not be empty")
    if len(content) > _MAX_CONTENT_CHARS:
        raise ToolError(
            f"content is over the {_MAX_CONTENT_CHARS:,}-character Teams limit — "
            "shorten it, or send it in parts with several send_message calls"
        )


async def _authorize(
    runtime: McpRuntime, auth: AuthIdentity, conversation_id: str
) -> TeamsBotClient:
    """The client, once the requester is confirmed on the target's roster."""
    client = runtime.teams_client
    if client is None:
        raise ToolError("Teams tools are not configured on this server")
    if auth.platform_user_id is None:
        raise ToolError("teams tools require a teams-bound identity")
    try:
        is_member = await client.is_member(conversation_id.split(";", 1)[0], auth.platform_user_id)
    except (httpx.HTTPError, ValueError) as err:
        raise ToolError(
            "could not confirm you are in that conversation, so nothing was posted"
        ) from err
    if not is_member:
        raise ToolError(_NOT_A_MEMBER)
    return client


def _post_failed(err: httpx.HTTPError | ValueError) -> ToolError:
    if isinstance(err, httpx.HTTPStatusError) and err.response.status_code in (403, 404):
        return ToolError("daimon cannot post there — add the app to that team or chat first")
    return ToolError(f"posting to Teams failed ({type(err).__name__})")


async def _teams_send_message_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    content: str,
    attachments: list[dict[str, str]] | None = None,
    file_handles: list[str] | None = None,
) -> TeamsMessageRow:
    if attachments or file_handles:
        raise ToolError("file posting is not available on Teams yet — send text only")
    _check_text(content)
    conversation_id = _conversation_id(channel_id)
    client = await _authorize(runtime, auth, conversation_id)
    try:
        activity_id = await client.send(conversation_id, content)
    except (httpx.HTTPError, ValueError) as err:
        raise _post_failed(err) from err
    return TeamsMessageRow(conversation_id=conversation_id, activity_id=activity_id, text=content)


async def _teams_create_thread_impl(  # pyright: ignore[reportUnusedFunction]  # registered by tools/channels.py
    runtime: McpRuntime, auth: AuthIdentity, *, channel_id: str, content: str
) -> TeamsMessageRow:
    _check_text(content)
    channel = _conversation_id(channel_id)
    if ";" in channel:
        raise ToolError(
            "a new post goes to a channel, not into a thread — pass the channel id "
            "(without ;messageid=), or use send_message to reply in a thread"
        )
    client = await _authorize(runtime, auth, channel)
    try:
        thread_id, activity_id = await client.create_thread(channel, content)
    except (httpx.HTTPError, ValueError) as err:
        raise _post_failed(err) from err
    return TeamsMessageRow(conversation_id=thread_id, activity_id=activity_id, text=content)


async def _post_teams_app_install_link_impl(  # pyright: ignore[reportUnusedFunction]  # used by tools/github_app.py
    runtime: McpRuntime, auth: AuthIdentity, *, channel_id: str, slug: str, purpose: str
) -> str:
    """Post the configured GitHub App install link. Returns the activity id."""
    text = (
        f"{purpose}\n\n[Install the GitHub App]({build_app_install_url(slug)}) to choose "
        "repositories it may read. Installing alone does not verify this workspace's access "
        "or bind a working repo. A working GitHub token remains an alternative; existing "
        "bound tokens stay in use."
    )
    row = await _teams_send_message_impl(runtime, auth, channel_id=channel_id, content=text)
    return row.activity_id


async def _post_teams_credential_card_impl(  # pyright: ignore[reportUnusedFunction]  # used by tools/credential_requests.py
    runtime: McpRuntime, auth: AuthIdentity, *, row: CredentialRequestRow
) -> str:
    """Post the `requested` card into the request's origin conversation. Returns its id."""
    conversation_id = conversation_of(row.channel_id)
    client = await _authorize(runtime, auth, conversation_id)
    card = build_adaptive_card(card_for_request(row, state="requested"), token=row.token)
    try:
        return await client.send_card(conversation_id, card)
    except (httpx.HTTPError, ValueError) as err:
        raise _post_failed(err) from err


async def edit_teams_card_state(
    runtime: McpRuntime,
    *,
    row: CredentialRequestRow,
    state: CardState,
    outcome: ConfigurationChange | None = None,
    refusal: RefusalReason | None = None,
) -> None:
    """Edit one posted card into `state`. Never raises.

    The outcome is already recorded when this runs (a newer form replaced the
    card, or an OAuth callback finished), so a failed edit costs feedback only.
    """
    client = runtime.teams_client
    if client is None or row.posted_message_id is None:
        return
    card = card_for_request(row, state=state, outcome=outcome, refusal=refusal)
    try:
        await client.update_card(
            conversation_of(row.channel_id), row.posted_message_id, build_adaptive_card(card)
        )
    except (httpx.HTTPError, ValueError) as err:
        _log.warning(
            "posted_card.edit_failed", kind=row.kind, state=state, err_type=type(err).__name__
        )
