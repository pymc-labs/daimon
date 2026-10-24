"""Tenant-scoped agent tool rules. Callers own and commit write transactions."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal, NamedTuple

import structlog
from daimon.core._models import AgentToolRuleHistoryRecord, AgentToolRuleRecord, Tenant
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, authorize
from daimon.core.tool_safety import (
    DAIMON_SERVER_NAME,
    WILDCARD_TOOL,
    ResolvedAgentToolRule,
    stricter_agent_tool_rule,
)
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

_log = structlog.get_logger(__name__)

Rule = Literal["allow", "ask", "deny"]
Scope = Literal["chat", "chat_and_routines"]


class StaleAgentToolRule(Exception):
    """The rule revision changed since the caller read it."""


class AgentToolRule(BaseModel):
    tenant_id: uuid.UUID
    agent_id: str
    agent_name: str
    tool_key: str
    channel_id: str | None
    server_url: str
    rule: Rule | None
    scope: Scope
    revision: int
    set_by: str
    set_at: datetime


def _view(row: AgentToolRuleRecord) -> AgentToolRule:
    return AgentToolRule(
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        agent_name=row.agent_name,
        tool_key=row.tool_key,
        channel_id=row.channel_id,
        server_url=row.server_url,
        rule=row.rule,  # type: ignore[arg-type]
        scope=row.scope,  # type: ignore[arg-type]
        revision=row.revision,
        set_by=row.set_by,
        set_at=row.set_at,
    )


async def list_rules(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str | None = None,
    channel_id: str | None = None,
) -> list[AgentToolRule]:
    query = select(AgentToolRuleRecord).where(AgentToolRuleRecord.tenant_id == tenant_id)
    if agent_id is not None:
        query = query.where(AgentToolRuleRecord.agent_id == agent_id)
    if channel_id is not None:
        query = query.where(AgentToolRuleRecord.channel_id == channel_id)
    rows = (
        await session.scalars(
            query.order_by(
                AgentToolRuleRecord.agent_name,
                AgentToolRuleRecord.tool_key,
                AgentToolRuleRecord.channel_id,
            )
        )
    ).all()
    return [_view(row) for row in rows]


async def get_rule(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    tool_key: str,
    channel_id: str | None = None,
) -> AgentToolRule | None:
    row = await session.scalar(
        select(AgentToolRuleRecord).where(
            AgentToolRuleRecord.tenant_id == tenant_id,
            AgentToolRuleRecord.agent_id == agent_id,
            AgentToolRuleRecord.tool_key == tool_key,
            AgentToolRuleRecord.channel_id.is_not_distinct_from(channel_id),
        )
    )
    return _view(row) if row is not None else None


async def get_for_call(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    tool_key: str,
    server_url: str,
    channel_id: str | None = None,
) -> ResolvedAgentToolRule | None:
    channel_match = (
        AgentToolRuleRecord.channel_id.is_(None)
        if channel_id is None
        else or_(
            AgentToolRuleRecord.channel_id == channel_id,
            AgentToolRuleRecord.channel_id.is_(None),
        )
    )
    rows = (
        await session.scalars(
            select(AgentToolRuleRecord).where(
                AgentToolRuleRecord.tenant_id == tenant_id,
                AgentToolRuleRecord.agent_id == agent_id,
                AgentToolRuleRecord.tool_key == tool_key,
                AgentToolRuleRecord.server_url == server_url,
                AgentToolRuleRecord.rule.is_not(None),
                channel_match,
            )
        )
    ).all()
    resolved: ResolvedAgentToolRule | None = None
    for row in rows:
        resolved = stricter_agent_tool_rule(
            resolved, ResolvedAgentToolRule.model_validate({"rule": row.rule, "scope": row.scope})
        )
    return resolved


async def deny_rules_for_trusted_tool(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    tool_keys: Sequence[str],
) -> list[AgentToolRule]:
    """Every agent-wide `deny` row for any of `tool_keys` in a tenant, whichever agent holds it.

    The MCP server cannot turn a chat credential's derived agent uuid back into
    the Managed Agents id the rows are keyed by, so it reads the tenant's few
    deny rows for these keys (the tool's own and the `daimon-mcp/*` wildcard)
    and compares each by `derive_agent_uuid`. One query on
    `uq_agent_tool_rules_global` (leading `tenant_id`). Channel rules are left
    out: the server does not know the channel. Tombstones (`rule IS NULL`),
    `allow` and `ask` rows are left out too. A deny holds in every scope, so
    scope is not read.
    """
    rows = (
        await session.scalars(
            select(AgentToolRuleRecord).where(
                AgentToolRuleRecord.tenant_id == tenant_id,
                AgentToolRuleRecord.tool_key.in_(list(tool_keys)),
                AgentToolRuleRecord.channel_id.is_(None),
                AgentToolRuleRecord.rule == "deny",
            )
        )
    ).all()
    return [_view(row) for row in rows]


class DaimonRuleSet(NamedTuple):
    """What an agent's rules on daimon's own tools ask of a session."""

    #: Tool names the session must ask on (`WILDCARD_TOOL` stands for the
    #: agent-wide deny-all). Empty when the agent has no applicable rule.
    tools: frozenset[str]
    #: Some rule only the turn driver can apply: any `ask`, or a channel `deny`.
    #: An agent-wide `deny` (a tool's own or `*`) is also enforced by the MCP
    #: server, so a backend with no approval pause still honours it.
    needs_driver: bool


#: A tool name MA accepts in a toolset config, and a daimon tool name in practice.
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def valid_tool_name(name: str) -> bool:
    """A plain tool name, or `*` (only meaningful as `daimon-mcp/*`)."""
    return name == WILDCARD_TOOL or _TOOL_NAME.fullmatch(name) is not None


async def daimon_rule_set(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str
) -> DaimonRuleSet:
    """The daimon tools this agent has a non-null ask or deny rule on.

    Any scope, agent-wide or channel, any channel: a chat session names no
    channel until a call arrives, so the session asks on every tool any channel
    could restrict and the driver applies the rule that fits the call. One
    indexed read per bind. A row whose key is not `daimon-mcp/<tool>` is not
    daimon's and is not counted. `*` appears only for an agent-wide `deny` of
    `daimon-mcp/*` (`WILDCARD_TOOL`), the one wildcard that means something;
    any other `*` row is ignored. A name that is not a plain tool name becomes
    a toolset config MA would refuse, so it is logged and skipped.
    """
    prefix = f"{DAIMON_SERVER_NAME}/"
    rows = await session.execute(
        select(
            AgentToolRuleRecord.tool_key, AgentToolRuleRecord.channel_id, AgentToolRuleRecord.rule
        ).where(
            AgentToolRuleRecord.tenant_id == tenant_id,
            AgentToolRuleRecord.agent_id == agent_id,
            AgentToolRuleRecord.rule.in_(("ask", "deny")),
            AgentToolRuleRecord.tool_key.startswith(prefix),
        )
    )
    names: set[str] = set()
    needs_driver = False
    for key, channel_id, rule in rows:
        name = key.removeprefix(prefix)
        if name == WILDCARD_TOOL:
            if rule != "deny" or channel_id is not None:
                continue
        elif not valid_tool_name(name):
            _log.warning("tool_safety.rule_name_skipped", tool=key[:160])
            continue
        names.add(name)
        needs_driver = needs_driver or rule == "ask" or channel_id is not None
    return DaimonRuleSet(frozenset(names), needs_driver)


async def daimon_rule_tools(
    session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str
) -> frozenset[str]:
    """`daimon_rule_set(...).tools`: the names a session must ask on."""
    return (await daimon_rule_set(session, tenant_id=tenant_id, agent_id=agent_id)).tools


async def set_rule(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    agent_name: str,
    tool_key: str,
    channel_id: str | None = None,
    server_url: str,
    rule: Rule | None,
    scope: Scope = "chat",
    set_by: str,
    actor: Subject,
    expected_revision: int | None = None,
) -> AgentToolRule:
    """Write a rule or tombstone and append history, serialized per tenant.

    `expected_revision=None` is an unconditional admin edit. For CAS, zero
    means no row yet. A clear of a missing row still writes revision one.
    """
    action = (
        Action.SET_AGENT_TOOL_RULE if channel_id is None else Action.SET_CHANNEL_AGENT_TOOL_RULE
    )
    if not authorize(
        TenantAccessPolicy(),
        subject=actor,
        action=action,
        place=Place(channel_id=channel_id),
    ):
        raise PermissionError("only an admin may edit its tool rules")
    if channel_id is not None and not channel_id:
        raise ValueError("channel_id must name a parent channel")
    server, separator, tool = tool_key.partition("/")
    if (
        not separator
        or not server
        or not tool
        or "/" in tool
        or not server_url
        # Only daimon's own names become tool settings in a session; a third
        # party's keep the shape-only check.
        or (server == DAIMON_SERVER_NAME and not valid_tool_name(tool))
    ):
        raise ValueError("tool_key must be exactly server/tool with a server URL")
    if rule not in (None, "allow", "ask", "deny") or scope not in ("chat", "chat_and_routines"):
        raise ValueError("invalid agent tool rule or scope")
    await session.scalar(
        select(Tenant.id).where(Tenant.id == tenant_id).with_for_update(key_share=True)
    )
    row = await session.scalar(
        select(AgentToolRuleRecord).where(
            AgentToolRuleRecord.tenant_id == tenant_id,
            AgentToolRuleRecord.agent_id == agent_id,
            AgentToolRuleRecord.tool_key == tool_key,
            AgentToolRuleRecord.channel_id.is_not_distinct_from(channel_id),
        )
    )
    revision = row.revision if row is not None else 0
    if expected_revision is not None and revision != expected_revision:
        raise StaleAgentToolRule(f"expected revision {expected_revision}, found {revision}")
    before_rule = row.rule if row is not None else None
    before_scope = row.scope if row is not None else None
    before_agent_name = row.agent_name if row is not None else None
    before_server_url = row.server_url if row is not None else None
    now = datetime.now(UTC)
    if row is None:
        row = AgentToolRuleRecord(
            tenant_id=tenant_id,
            agent_id=agent_id,
            agent_name=agent_name,
            tool_key=tool_key,
            channel_id=channel_id,
            server_url=server_url,
            rule=rule,
            scope=scope,
            revision=1,
            set_by=set_by,
            set_at=now,
        )
        session.add(row)
    else:
        row.agent_name = agent_name
        row.server_url = server_url
        row.rule = rule
        row.scope = scope
        row.revision += 1
        row.set_by = set_by
        row.set_at = now
    session.add(
        AgentToolRuleHistoryRecord(
            tenant_id=tenant_id,
            agent_id=agent_id,
            tool_key=tool_key,
            channel_id=channel_id,
            revision=row.revision,
            set_by=set_by,
            set_at=now,
            before_rule=before_rule,
            before_scope=before_scope,
            before_agent_name=before_agent_name,
            before_server_url=before_server_url,
            after_rule=rule,
            after_scope=scope,
            after_agent_name=agent_name,
            after_server_url=server_url,
        )
    )
    await session.flush()
    return _view(row)


async def clear_rule(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    agent_name: str,
    tool_key: str,
    channel_id: str | None = None,
    server_url: str,
    set_by: str,
    actor: Subject,
    expected_revision: int | None = None,
) -> AgentToolRule:
    return await set_rule(
        session,
        tenant_id=tenant_id,
        agent_id=agent_id,
        agent_name=agent_name,
        tool_key=tool_key,
        channel_id=channel_id,
        server_url=server_url,
        rule=None,
        set_by=set_by,
        actor=actor,
        expected_revision=expected_revision,
    )
