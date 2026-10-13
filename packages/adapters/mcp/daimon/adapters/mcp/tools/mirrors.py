"""Mirror tools: create / list / remove (`daimon.core.mirrors`).

All three are admin-only: the list shows recorded consent. Destinations are
Discord channels or threads of the same server for now;
`tests/parity/test_mirror_delivery_discord_only.py` is the executable record
that Slack and Teams refuse.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import discord
import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import get_verified_origin
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._session_gate import session_card_gap
from daimon.adapters.mcp.tools.discord._client import (
    _require_bot_token,  # pyright: ignore[reportPrivateUsage]
    rest_client,
)
from daimon.core.agent_pins import agent_pin_names
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mirrors import MirrorRefusal, mirror_refusal
from daimon.core.permissions import channel_permissions
from daimon.core.routine_delivery import destination_shape_error, placement_unknown_is_unsafe
from daimon.core.stores import mirrors as mirrors_store
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.domain import MirrorRow
from daimon.core.stores.security_audit import append_event
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

log = structlog.get_logger(__name__)

_REFUSALS: dict[MirrorRefusal, str] = {
    "writers_none": "the destination's rule lets nobody post there",
    "not_a_reader": "the agent's conversations may not be read there",
    "own_agents_only": (
        "a channel kept to its own agents only takes its own agents' conversations, and an "
        "own agent's conversations never leave its channel"
    ),
}


async def _discord_placement(
    runtime: McpRuntime, destination_id: str
) -> tuple[str | None, str | None]:
    """(parent channel, category) of a Discord channel or thread."""
    try:
        async with rest_client(_require_bot_token(runtime)) as client:
            channel = await client.fetch_channel(int(destination_id))
            parent = None
            if isinstance(channel, discord.Thread):
                parent = str(channel.parent_id)
                channel = await client.fetch_channel(channel.parent_id)
            category = getattr(channel, "category_id", None)
    except discord.HTTPException as err:
        raise ToolError(
            f"{destination_id} could not be looked up ({err.status}). Nothing was saved."
        ) from err
    return parent, str(category) if category else None


class MirrorOut(BaseModel):
    name: str
    agent: str
    destination_kind: str
    destination_id: str
    status: str
    paused_reason: str | None
    covers: str
    consent: str | None


_NO_CONSENT_CARD = (
    "Consent is recorded only when the admin presses Approve on a card in a chat "
    "conversation, and {why}. Nothing was saved. Tell the caller; to mirror only "
    "conversations of agent keys minted in a channel, call again without consent_note."
)


async def _require_consent_card(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
) -> None:
    """A consent note is the admin's own word on a card (`CONFIRMED_DAIMON_TOOLS`):
    never an agent key's, a routine's or other headless run's, nor a model's."""
    if auth.agent_id is not None:
        raise ToolError(_NO_CONSENT_CARD.format(why="an agent key has no person to ask"))
    if auth.chat_agent_id is None or not runtime.settings.tool_safety.enabled:
        raise ToolError(_NO_CONSENT_CARD.format(why="this call is not from such a conversation"))
    origin = await get_verified_origin(runtime, auth, origin_context_id)
    if await session_card_gap(runtime, auth, origin, tool_name="create_mirror") is not None:
        raise ToolError(
            _NO_CONSENT_CARD.format(why="this call named no origin_context_id or had no card")
        )


async def _create_mirror_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    name: str,
    agent_name: str,
    destination_id: str,
    destination_kind: Literal["channel", "thread"],
    consent_note: str | None,
    max_answer_chars: int | None,
    origin_context_id: str | None = None,
) -> MirrorOut:
    _require_admin(auth)
    consent_note = (consent_note or "").strip() or None
    if not runtime.settings.mirrors.enabled:
        raise ToolError("Mirrors are switched off on this deployment. Nothing was saved.")
    if auth.platform != "discord":
        raise ToolError(
            f"Mirrors post only into Discord for now, and this workspace is on "
            f"{auth.platform or 'no chat platform'}. Nothing was saved."
        )
    if not name.strip() or len(name) > 80:
        raise ToolError("A mirror needs a name of 1 to 80 characters.")
    if max_answer_chars is not None and max_answer_chars < 100:
        raise ToolError("max_answer_chars must be at least 100.")
    if (
        shape := destination_shape_error(auth.platform, destination_kind, destination_id)
    ) is not None:
        raise ToolError(f"{shape}. Nothing was saved.")
    if consent_note is not None:
        await _require_consent_card(runtime, auth, origin_context_id)
    match = await find_agent_by_daimon_tag(
        runtime.client, tenant_id=auth.tenant_id, name=agent_name
    )
    if match is None:
        raise ToolError(f"no agent named {agent_name!r} found for this tenant")
    names = tuple(dict.fromkeys(n for n in agent_pin_names(match.name, match.metadata) if n))
    now = datetime.now(UTC)
    async with runtime.session_factory() as session:
        try:
            policy = await load_access_policy(session, tenant_id=auth.tenant_id)
        except AccessPolicyUnreadable as err:
            raise ToolError(
                "the workspace access policy could not be read; nothing was saved"
            ) from err
    # Looked up only when a protected category could hold it; the poster
    # re-checks the full placement before every post.
    parent, category = (
        await _discord_placement(runtime, destination_id)
        if placement_unknown_is_unsafe(policy, platform=auth.platform, kind=destination_kind)
        else (None, None)
    )
    dest = channel_permissions(
        policy, channel_id=destination_id, parent_channel_id=parent, category_id=category
    )
    refusal = mirror_refusal(policy, agent_names=names, dest=dest)
    if refusal is not None:
        raise ToolError(f"Refused: {_REFUSALS[refusal]}. Nothing was saved.")
    async with runtime.session_factory() as session, session.begin():
        row = await mirrors_store.upsert_mirror(
            session,
            tenant_id=auth.tenant_id,
            name=name.strip(),
            source_agent_id=derive_agent_uuid(tenant_id=auth.tenant_id, ma_agent_id=match.id),
            source_agent_name=agent_name,
            source_agent_names=names,
            dest_platform=auth.platform,
            dest_kind=destination_kind,
            dest_id=destination_id,
            consent_note=consent_note,
            max_answer_chars=max_answer_chars,
            account_id=auth.account_id,
            now=now,
        )
        await append_event(
            session,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            agent_id=row.source_agent_id,
            agent_name=agent_name,
            platform=auth.platform,
            platform_user_id=auth.platform_user_id,
            tool_name="create_mirror",
            operation="attest_consent" if row.consent_basis else "create",
            outcome="allowed",
            reason=(
                f"mirror {row.name} -> {destination_kind} {destination_id}"
                + (f"; consent attested: {row.consent_note[:500]}" if row.consent_note else "")
            ),
            target_channel_id=destination_id,
        )
    log.info("mirror.created", mirror_id=str(row.id), consent=row.consent_basis)
    return _out(row)


def _out(row: MirrorRow) -> MirrorOut:
    return MirrorOut(
        name=row.name,
        agent=row.source_agent_name,
        destination_kind=row.dest_kind,
        destination_id=row.dest_id,
        status=row.status,
        paused_reason=row.paused_reason,
        covers=(
            "its agent keys and hub conversations"
            if row.consent_basis
            else "only agent keys minted in a channel (no recorded consent for personal sessions)"
        ),
        consent=(
            f"attested {row.consent_at:%Y-%m-%d}: {row.consent_note}"
            if row.consent_basis and row.consent_at
            else None
        ),
    )


async def _list_mirrors_impl(runtime: McpRuntime, auth: AuthIdentity) -> list[MirrorOut]:
    _require_admin(auth)
    async with runtime.session_factory() as session:
        rows = await mirrors_store.list_for_tenant(session, auth.tenant_id)
    return [_out(row) for row in rows]


async def _remove_mirror_impl(runtime: McpRuntime, auth: AuthIdentity, name: str) -> bool:
    _require_admin(auth)
    async with runtime.session_factory() as session, session.begin():
        removed = await mirrors_store.delete_mirror(session, tenant_id=auth.tenant_id, name=name)
        if removed:
            await append_event(
                session,
                tenant_id=auth.tenant_id,
                account_id=auth.account_id,
                agent_id=None,
                platform=auth.platform,
                platform_user_id=auth.platform_user_id,
                tool_name="remove_mirror",
                operation="remove",
                outcome="allowed",
                reason=f"mirror {name}",
            )
    if removed:
        log.info("mirror.removed", tenant_id=str(auth.tenant_id))
    return removed


def register_mirror_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin"})
    async def create_mirror(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        name: str,
        agent_name: str,
        destination_id: str,
        destination_kind: Literal["channel", "thread"] = "channel",
        consent_note: str | None = None,
        max_answer_chars: int | None = None,
        origin_context_id: str | None = None,
    ) -> MirrorOut:
        """Copy an agent's agent-chat questions and answers into a Discord channel or
        thread of this server. Admin-only.

        Every answered ``ask`` to ``agent_name`` (an agent key or the hub) is
        posted by Daimon: in a channel, one thread per conversation; in a
        thread, straight in. Turns run with ``start_turn``/``continue_turn``
        are not copied. Only the question and the final answer are
        copied, never tool calls. Conversations of an agent key minted in a
        channel follow that channel's rules. A person's own conversations are
        copied only with ``consent_note``: the admin's record of the askers'
        agreement (who agreed, how). It is audited, and taken only from a chat
        conversation where the admin approves it on a card: pass this turn's
        ``origin_context_id``. A channel kept to its own
        agents only takes its own agents' conversations. Calling it again with
        the same ``name`` replaces that mirror and resumes it if paused.
        ``name`` is shown to askers, so name it for where it posts.
        ``max_answer_chars`` cuts long text.
        """
        return await _create_mirror_impl(
            runtime,
            await _auth(ctx),
            name=name,
            agent_name=agent_name,
            destination_id=destination_id,
            destination_kind=destination_kind,
            consent_note=consent_note,
            max_answer_chars=max_answer_chars,
            origin_context_id=origin_context_id,
        )

    @mcp.tool(tags={"admin"})
    async def list_mirrors(ctx: Context) -> list[MirrorOut]:  # pyright: ignore[reportUnusedFunction]
        """List this workspace's mirrors: what each copies where, whether it is paused and
        why, and its recorded consent. Admin-only."""
        return await _list_mirrors_impl(runtime, await _auth(ctx))

    @mcp.tool(tags={"admin"})
    async def remove_mirror(ctx: Context, name: str) -> bool:  # pyright: ignore[reportUnusedFunction]
        """Remove a mirror by name and drop its queued posts. Admin-only. Messages
        already posted stay."""
        return await _remove_mirror_impl(runtime, await _auth(ctx), name)
