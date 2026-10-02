"""Channel skill tools: extra skills one channel's turns run with. Admin-only.

`authorize` (SET_CHANNEL_SKILLS) allows a server admin and never a channel
admin. Operator tokens read them with ``tenant:read`` and change them with
``channels:write``. What a channel may add is `daimon.core.channel_skills`'s.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._channel_target import resolve_channel
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._isolation import load_caller_isolation
from daimon.adapters.mcp.tools._scopes import require_scope, scope_tags
from daimon.core.authz import Action
from daimon.core.channel_skills import REFUSALS, add_skill_to_channel, may_set_channel_skills
from daimon.core.errors import SkillsListTruncatedError
from daimon.core.security_audit import record_authz_denial, record_policy_decision
from daimon.core.stores import channel_skills as store
from daimon.core.stores.domain import ChannelSkillRow
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

_PLATFORMS = ("discord", "slack", "teams")
_NEEDS_SKILLS_ADMIN = (
    "Changing a channel's skills needs a workspace or server admin, and the caller is not "
    "one; a channel's own admins can't change them either. Tell them who can, and give "
    "them a sentence that admin can say. Do not retry."
)


@dataclass(frozen=True)
class ChannelSkillResult:
    """One extra skill a channel's turns run with."""

    skill_id: str
    name: str
    version: str
    """The version added; adding the skill again picks up a newer one."""
    agent_name: str | None
    """The agent it was uploaded to, which it applies with only; None for a library skill."""


@dataclass(frozen=True)
class ChannelSkillsResult:
    channel_id: str
    skills: list[ChannelSkillResult]
    summary: str


def _result(row: ChannelSkillRow) -> ChannelSkillResult:
    return ChannelSkillResult(
        skill_id=row.skill_id, name=row.name, version=row.version, agent_name=row.owner_agent_name
    )


async def _shown(
    runtime: McpRuntime, auth: AuthIdentity, target: str, rows: Sequence[ChannelSkillRow]
) -> ChannelSkillsResult:
    """The result, leaving out uploads of agents an isolated channel hides from the caller."""
    caller = await load_caller_isolation(runtime, auth)
    seen = [
        row for row in rows if row.owner_agent_name is None or caller.sees(row.owner_agent_name)
    ]
    return ChannelSkillsResult(target, [_result(row) for row in seen], _summary(target, seen))


def _summary(channel_id: str, rows: Sequence[ChannelSkillRow]) -> str:
    if not rows:
        return f"Channel {channel_id} adds no skills to its agent."
    names = ", ".join(row.name for row in rows)
    return f"Channel {channel_id} adds {names} to its agent, from the next message."


async def _target(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> tuple[str, str]:
    if auth.platform not in _PLATFORMS:
        raise ToolError("channel skills exist only for Discord, Slack and Teams channels")
    if not channel_id.strip():
        raise ToolError("channel_id is required: the channel the user named.")
    target = (await resolve_channel(runtime, auth, channel_id.strip())).channel_id
    return cast(str, auth.platform), target


def _require_write(auth: AuthIdentity, channel_id: str) -> None:
    decision = may_set_channel_skills(mcp_subject(auth, is_admin=auth.is_admin), channel_id)
    if not decision:
        record_authz_denial(Action.SET_CHANNEL_SKILLS, decision.reason)
        raise ToolError(_NEEDS_SKILLS_ADMIN)
    record_policy_decision(Action.SET_CHANNEL_SKILLS, "allow")


async def _list(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> ChannelSkillsResult:
    require_scope(auth, "tenant:read")
    _require_admin(auth)
    platform, target = await _target(runtime, auth, channel_id)
    async with runtime.session_factory() as session:
        rows = await store.list_channel_skills(
            session, tenant_id=auth.tenant_id, platform=platform, channel_id=target
        )
    return await _shown(runtime, auth, target, rows)


async def _add(
    runtime: McpRuntime, auth: AuthIdentity, channel_id: str, skill: str
) -> ChannelSkillsResult:
    require_scope(auth, "channels:write")
    platform, target = await _target(runtime, auth, channel_id)
    _require_write(auth, target)
    async with runtime.session_factory.begin() as session:
        try:
            added = await add_skill_to_channel(
                session,
                runtime.client,
                tenant_id=auth.tenant_id,
                platform=platform,
                channel_id=target,
                skill=skill,
                default=runtime.deployment_default,
                actor_account_id=auth.account_id,
            )
        except SkillsListTruncatedError as exc:
            raise ToolError(
                "This workspace's skills could not all be read. Nothing changed."
            ) from exc
        if isinstance(added, str):
            raise ToolError(REFUSALS[added])
        rows = await store.list_channel_skills(
            session, tenant_id=auth.tenant_id, platform=platform, channel_id=target
        )
    return await _shown(runtime, auth, target, rows)


async def _remove(
    runtime: McpRuntime, auth: AuthIdentity, channel_id: str, skill: str
) -> ChannelSkillsResult:
    require_scope(auth, "channels:write")
    platform, target = await _target(runtime, auth, channel_id)
    _require_write(auth, target)
    wanted = skill.strip()
    async with runtime.session_factory.begin() as session:
        rows = await store.list_channel_skills(
            session, tenant_id=auth.tenant_id, platform=platform, channel_id=target
        )
        shown = (await _shown(runtime, auth, target, rows)).skills
        match = next((r for r in shown if wanted in (r.skill_id, r.name)), None)
        if match is None:
            raise ToolError(f"Channel {target} doesn't add that skill. Nothing changed.")
        await store.remove_channel_skill(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            channel_id=target,
            skill_id=match.skill_id,
        )
    return await _shown(runtime, auth, target, [r for r in rows if r.skill_id != match.skill_id])


def register_channel_skill_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"admin", *scope_tags("tenant:read")})
    async def list_channel_skills(ctx: Context, channel_id: str) -> ChannelSkillsResult:  # pyright: ignore[reportUnusedFunction]
        """List the extra skills a channel's turns run with, on top of its agent's. Admin-only."""
        return await _list(runtime, await _auth(ctx), channel_id)

    @mcp.tool(tags={"admin", *scope_tags("channels:write")})
    async def add_channel_skill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, channel_id: str, skill: str
    ) -> ChannelSkillsResult:
        """Add a skill to whatever agent answers in one channel, there only. Admin-only.

        ``skill`` is a skill id, a workspace library skill's name, or
        ``agent/name`` for one uploaded to the agent that answers in this
        channel; another agent's upload is refused. The latest version is
        added; add it again to pick up a newer one. It applies from the next
        message in each conversation there. A thread id resolves to its parent.
        """
        return await _add(runtime, await _auth(ctx), channel_id, skill)

    @mcp.tool(tags={"admin", *scope_tags("channels:write")})
    async def remove_channel_skill(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, channel_id: str, skill: str
    ) -> ChannelSkillsResult:
        """Remove an extra skill from a channel, by id or name. Admin-only."""
        return await _remove(runtime, await _auth(ctx), channel_id, skill)


__all__ = ["register_channel_skill_tools"]
