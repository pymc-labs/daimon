"""An agent's own tool rules, the ones that apply in every channel and, by scope, in routines.

Admin only. A channel's rules are set in its settings (`channel_settings`), not here.
A tool is `connection/tool` on a connection the agent has, Daimon's own included.
"""

from __future__ import annotations

from daimon.core.agent_admin import require_admin
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_target import ChangeRecorder, load_target
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.channel_settings import tool_server_url
from daimon.core.stores import agent_tool_rules as store
from pydantic import BaseModel, ConfigDict

_OFF = "Tool rules are off on this install."


class AgentRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: str
    rule: store.Rule | None
    scope: store.Scope
    revision: int


def _doc(row: store.AgentToolRule) -> AgentRule:
    return AgentRule(tool=row.tool_key, rule=row.rule, scope=row.scope, revision=row.revision)


async def list_agent_rules(
    deps: AgentEditDeps, *, actor: AgentActor, ma_agent_id: str
) -> list[AgentRule]:
    """The agent's rules that hold in every channel."""
    require_admin(actor, "Tool rules")
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    async with deps.session_factory() as session:
        rows = await store.list_rules(
            session, tenant_id=actor.tenant_id, agent_id=target.agent.id, channel_id=None
        )
    return [_doc(r) for r in rows if r.channel_id is None]


async def set_agent_rule(
    deps: AgentEditDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    tool: str,
    rule: store.Rule | None,
    scope: store.Scope,
    expected_revision: int,
    enabled: bool,
    audited: ChangeRecorder,
) -> AgentRule:
    """Set (None: clear) the agent's rule on `tool`; `expected_revision` 0 means no rule yet."""
    require_admin(actor, "Tool rules")
    if not enabled:
        raise AgentEditError(_OFF, kind="invalid")
    target = await load_target(deps, actor=actor, ma_agent_id=ma_agent_id)
    async with deps.session_factory.begin() as session:
        existing = await store.get_rule(
            session,
            tenant_id=actor.tenant_id,
            agent_id=target.agent.id,
            tool_key=tool,
            channel_id=None,
        )
        try:
            url = (
                existing.server_url
                if existing is not None and rule is None
                else tool_server_url(target.agent, tool)
            )
        except ChannelChangeRefused as exc:
            raise AgentEditError(str(exc), kind="invalid") from exc
        try:
            row = await store.set_rule(
                session,
                tenant_id=actor.tenant_id,
                agent_id=target.agent.id,
                agent_name=target.agent.name,
                tool_key=tool,
                channel_id=None,
                server_url=url,
                rule=rule,
                scope=scope,
                set_by=str(actor.account_id),
                actor=actor.subject,
                expected_revision=expected_revision,
            )
        except store.StaleAgentToolRule as exc:
            raise AgentEditError(
                "This rule changed since you opened it. Reload and try again.", kind="stale"
            ) from exc
        await audited(session, target.agent.name)
    return _doc(row)
