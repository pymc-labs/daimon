"""Post the credential-request card to a Slack channel.

Posted from the MCP process (Cloud Run); dispatched later by the Slack bot
process via `handle_credential_request_click`, and edited in place by that
same process via `daimon.adapters.slack.posted_controls.edit_posted_card`.
The two processes cannot import each other (import-linter's independence
contract), so the card itself is built in core
(`daimon.core.posted_controls`) and both sides render the same four slots
from it. A divergent copy here would silently drift the posted card from the
one the bot edits it into, and drop the button's `action_id` out of
dispatch range.

Unlike Discord's custom_id encoding, Slack routes block_actions by
`action_id` and a button carries a free-form `value`, so the opaque
single-use token rides in `value` under the fixed `SLACK_ACTION_ID`
(`build_card_blocks` puts it there).

Reuses the read tools' channel-visibility discipline (`conversations.info` →
`check_channel_access`) so posting a credential card proves the requester
may see the channel before anything lands in it.

The validated turn origin supplies the parent channel and thread timestamp.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import require_channel_writable
from daimon.adapters.mcp.tools.slack._client import (
    _require_slack_identity,  # pyright: ignore[reportPrivateUsage]
    _require_team_id,  # pyright: ignore[reportPrivateUsage]
    slack_web_client,
)
from daimon.adapters.mcp.tools.slack._visibility import check_channel_access
from daimon.core.authz import Place
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.credential_requests import (
    CredentialRequestKind,
    split_skill_repo_target,
)
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.posted_controls import (
    CardKind,
    CardState,
    RefusalReason,
    build_card_blocks,
    build_posted_card,
    card_notification_text,
)
from daimon.core.stores.domain import CredentialRequestRow
from daimon.core.stores.tenants import get_tenant
from fastmcp.exceptions import ToolError
from slack_sdk.errors import SlackApiError

_log = structlog.get_logger()

_REPO_KINDS: frozenset[CredentialRequestKind] = frozenset({"repo", "skill_repo"})


def _repo_display(kind: CredentialRequestKind, target: str) -> str | None:
    """`owner/repo` for the two repo kinds, `None` for every other kind.

    The repo kinds pack `repo_url@branch#path` into `target`; the card names
    the repo, not the packed string.
    """
    if kind not in _REPO_KINDS:
        return None
    repo_url, _, _ = split_skill_repo_target(target)
    return normalize_owner_repo(repo_url)


async def _post_slack_credential_button_impl(  # pyright: ignore[reportUnusedFunction]
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    channel_id: str,
    kind: CredentialRequestKind,
    target: str,
    token: str,
    agent_name: str,
    purpose: str,
    expires_at: datetime,
    responder_name: str,
    mcp_server_url: str | None = None,
    branch: str | None = None,
    thread_ts: str | None = None,
    origin: Place | None = None,
) -> str:
    """Post the `requested` card for one credential request. Returns its ts.

    `purpose` is the agent's reason for asking; it is deliberately not
    rendered — the card's facts say what the value is for in the product's
    own words, and the free-text reason belongs in the turn that asked.
    """
    requester_id = _require_slack_identity(auth)
    team_id = _require_team_id(auth)
    client = await slack_web_client(runtime, team_id=team_id)

    card = build_posted_card(
        kind=kind,
        state="requested",
        agent_name=agent_name,
        responder_name=responder_name,
        target=target,
        requester_platform_user_id=requester_id,
        expires_at=expires_at,
        token=token,
        mcp_server_url=mcp_server_url,
        repo=_repo_display(kind, target),
        branch=branch,
    )

    try:
        info = await client.conversations_info(channel=channel_id)  # pyright: ignore[reportUnknownMemberType]
        channel: dict[str, Any] = dict(info.get("channel") or {})  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        await check_channel_access(client, channel=channel, user_id=requester_id, allow_own_im=True)
        await require_channel_writable(runtime, auth, channel_id=channel_id, origin=origin)
        sent = await client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            thread_ts=thread_ts,
            text=card_notification_text(card),
            blocks=build_card_blocks(card, token=token),
        )
    except SlackApiError as err:
        code = str(err.response.get("error", "slack_api_error"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
        raise ToolError(f"posting to the channel failed ({code})") from err
    return str(sent.get("ts") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


async def edit_card_state_for_tenant(
    runtime: McpRuntime,
    *,
    row: CredentialRequestRow,
    state: CardState,
    outcome: ConfigurationChange | None = None,
    refusal: RefusalReason | None = None,
) -> None:
    """Edit one card into `state` with no caller identity at hand. Never raises.

    The OAuth callback has a row and a tenant, not an `AuthIdentity`; a Slack
    tenant's `external_id` is its team id, which is all the web client needs.
    """
    if row.posted_message_id is None:
        return
    async with runtime.session_factory() as session:
        tenant = await get_tenant(session, row.tenant_id)
    if tenant is None or tenant.platform != "slack":
        return
    card = build_posted_card(
        kind=cast("CardKind", row.kind),
        state=state,
        agent_name=row.target_name or "the agent",
        responder_name=row.responder_name or "Daimon",
        target=row.target,
        requester_platform_user_id=row.requester_platform_user_id,
        expires_at=row.expires_at,
        token=row.token,
        mcp_server_url=row.mcp_server_url,
        outcome=outcome,
        refusal=refusal,
    )
    try:
        client = await slack_web_client(runtime, team_id=tenant.external_id)
        await client.chat_update(  # pyright: ignore[reportUnknownMemberType]
            channel=row.parent_channel_id or row.channel_id,
            ts=row.posted_message_id,
            text=card_notification_text(card),
            blocks=build_card_blocks(card),
        )
    except (SlackApiError, ToolError) as err:
        _log.warning("posted_card.edit_failed", kind=row.kind, state=state, error=str(err)[:200])


async def edit_card_replaced(
    runtime: McpRuntime, auth: AuthIdentity, *, row: CredentialRequestRow
) -> None:
    """Edit one retired card into the `replaced` state. Never raises.

    Runs right after a newer form for the same person, thread and agent was
    posted, so the older card stops offering a button nobody should press.
    The row is already spent by then, so a Slack refusal is logged rather
    than raised: the failure costs feedback, not correctness.

    A row with no recorded `ts` never had a card to edit.
    """
    if row.posted_message_id is None:
        return
    card = build_posted_card(
        kind=cast("CardKind", row.kind),
        state="replaced",
        agent_name=row.target_name or "the agent",
        responder_name=row.responder_name or "Daimon",
        target=row.target,
        requester_platform_user_id=row.requester_platform_user_id,
        expires_at=row.expires_at,
        token=row.token,
    )
    client = await slack_web_client(runtime, team_id=_require_team_id(auth))
    try:
        await client.chat_update(  # pyright: ignore[reportUnknownMemberType]
            channel=row.parent_channel_id or row.channel_id,
            ts=row.posted_message_id,
            text=card_notification_text(card),
            blocks=build_card_blocks(card),
        )
    except SlackApiError as err:
        _log.warning(
            "posted_card.replace_failed",
            kind=row.kind,
            error=str(err.response.get("error", "slack_api_error")),  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]  # slack_sdk response is dict-like
        )
