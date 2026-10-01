"""Post the credential-request card to a Discord channel.

Posted from the MCP process (Cloud Run); dispatched later by the Discord bot
process (worker VM) via `CredentialRequestButton`, a `discord.ui.DynamicItem`
matching `CUSTOM_ID_TEMPLATE`, and edited in place by that same process once
the form comes back. The two processes cannot import each other
(import-linter's independence contract), so the copy the card wears is built
by `daimon.core.posted_controls` and drawn by `_posted_card.build_card_view`
— never spelled out here. A divergent copy would silently desync the posted
card from the one the bot edits it into.

The card is a components-v2 `LayoutView`, which is why nothing here sends
`content`: such a message may not carry any. The footer's requester mention
is the one and only ping in a request's whole lifecycle — every later edit
goes out with mentions suppressed.

Reuses `_send_message_impl`'s exact require/resolve/permission-check order
(`tools/discord/_send.py`) rather than re-implementing any of it, so posting
a credential card carries the same channel-visibility discipline as
`send_message`.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

import discord
import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    _require_discord_identity,  # pyright: ignore[reportPrivateUsage]
    _require_guild_channel,  # pyright: ignore[reportPrivateUsage]
    _require_guild_id,  # pyright: ignore[reportPrivateUsage]
    _resolve_channel,  # pyright: ignore[reportPrivateUsage]
    _resolve_member,  # pyright: ignore[reportPrivateUsage]
    rest_client,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._posted_card import build_card_view
from daimon.adapters.mcp.tools.discord._visibility import (
    _check_send_permission,  # pyright: ignore[reportPrivateUsage]
    _ensure_thread_parent_cached,  # pyright: ignore[reportPrivateUsage]
    _require_discord_channel_writable,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.credential_requests import (
    CredentialRequestKind,
    split_skill_repo_target,
)
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.posted_controls import CardKind, CardState, RefusalReason, build_posted_card
from daimon.core.stores.domain import CredentialRequestRow
from fastmcp.exceptions import ToolError

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


async def _post_credential_button_impl(  # pyright: ignore[reportUnusedFunction]
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
    branch: str | None = None,
    mcp_server_url: str | None = None,
) -> str:
    """Post the `requested` card for one credential request.

    Returns the sent message id, which the caller records on the row so the
    bot process can find this exact message to edit later.

    `purpose` is the agent's reason for asking. It is deliberately not
    rendered: the card's own facts already say what the value is for and who
    can use it, and a model-authored sentence on a public card is the one
    place an injected mention could reach the channel.

    `branch` is required for the two repo kinds and `mcp_server_url` for
    `mcp` — the card names both — and `build_posted_card` raises for a
    request that arrives without its own.
    """
    requester_id = _require_discord_identity(auth)
    guild_id = _require_guild_id(auth)
    bot_token = _require_bot_token(runtime)

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
    view = build_card_view(card)

    async with rest_client(bot_token) as c:
        _, member = await _resolve_member(c, guild_id, requester_id)
        raw_channel = await _resolve_channel(c, channel_id)
        channel = _require_guild_channel(raw_channel, guild_id)
        if isinstance(channel, discord.Thread):
            # Thread.permissions_for needs the parent in the guild cache;
            # the per-call REST client starts with an empty one.
            await _ensure_thread_parent_cached(channel)
        _check_send_permission(channel, member)
        await _require_discord_channel_writable(runtime, auth, channel)
        if not isinstance(channel, discord.abc.Messageable):
            raise ToolError("channel does not support sending messages")
        sent = await channel.send(
            view=view,
            allowed_mentions=discord.AllowedMentions(
                users=True, everyone=False, roles=False, replied_user=False
            ),
        )
        return str(sent.id)


async def edit_card_state(
    runtime: McpRuntime,
    *,
    row: CredentialRequestRow,
    state: CardState,
    outcome: ConfigurationChange | None = None,
    refusal: RefusalReason | None = None,
) -> None:
    """Edit one card into `state` from the mcp process. Never raises.

    The OAuth callback lands here rather than in the bot process, so the
    card's final state has to be written from this side. Same feedback-only
    contract as `edit_card_replaced`: the durable outcome is already recorded
    when this runs, so a process with no Discord bot token (the mcp service
    can be deployed without one) or an unreachable message only costs the
    card its final words.
    """
    if row.origin_thread_id is None or row.posted_message_id is None:
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
    view = build_card_view(card)
    try:
        async with rest_client(_require_bot_token(runtime)) as c:
            message = c.get_partial_messageable(int(row.origin_thread_id)).get_partial_message(
                int(row.posted_message_id)
            )
            await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
    except (ToolError, ValueError, discord.DiscordException) as err:
        _log.warning(
            "posted_card.edit_failed", err_type=type(err).__name__, kind=row.kind, state=state
        )


async def edit_card_replaced(runtime: McpRuntime, *, row: CredentialRequestRow) -> None:
    """Edit one retired card into the `replaced` state. Never raises.

    Runs right after a newer form for the same person, thread and agent was
    posted, so the older card stops offering a button nobody should press.
    The row is already spent by then: a card that cannot be edited (message
    deleted, thread archived, permissions lost) costs feedback, not
    correctness, so a Discord refusal is logged and the mint continues.

    A row with no thread or message id never had a card to edit.
    """
    if row.origin_thread_id is None or row.posted_message_id is None:
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
    view = build_card_view(card)
    async with rest_client(_require_bot_token(runtime)) as c:
        message = c.get_partial_messageable(int(row.origin_thread_id)).get_partial_message(
            int(row.posted_message_id)
        )
        try:
            await message.edit(view=view, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as err:
            _log.warning("posted_card.replace_failed", err_type=type(err).__name__, kind=row.kind)
