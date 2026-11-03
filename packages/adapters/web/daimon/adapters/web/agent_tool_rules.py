"""`/api/agents/{id}/tool-rules`: an agent's rules for every channel, with routine scope."""

from __future__ import annotations

from daimon.adapters.web.agent_access import Viewer, actor_of, change_deps, edit_refusal
from daimon.adapters.web.agent_audit import recorded
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import agent_tool_rule_admin
from daimon.core.agent_editing import AgentEditError
from daimon.core.agent_tool_rule_admin import AgentRule
from daimon.core.stores.agent_tool_rules import Rule, Scope
from fastapi import APIRouter
from pydantic import BaseModel


class RulesDoc(BaseModel):
    """`active` is false while tool safety is off: the rules can be read, not changed."""

    active: bool
    rules: list[AgentRule]


class RuleBody(BaseModel):
    tool: str
    rule: Rule | None
    scope: Scope = "chat"
    expected_revision: int


def agent_tool_rules_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/agents")
    safety = runtime.settings.tool_safety

    @router.get("/{agent_id}/tool-rules")
    async def rules(caller: Viewer, agent_id: str) -> RulesDoc:  # pyright: ignore[reportUnusedFunction]
        try:
            rows = await agent_tool_rule_admin.list_agent_rules(
                change_deps(runtime).edit, actor=actor_of(caller), ma_agent_id=agent_id
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc
        return RulesDoc(active=safety.enabled and safety.agent_rules, rules=rows)

    @router.put("/{agent_id}/tool-rules")
    async def put_rule(caller: Viewer, agent_id: str, body: RuleBody) -> AgentRule:  # pyright: ignore[reportUnusedFunction]
        try:
            return await agent_tool_rule_admin.set_agent_rule(
                change_deps(runtime).edit,
                actor=actor_of(caller),
                ma_agent_id=agent_id,
                tool=body.tool,
                rule=body.rule,
                scope=body.scope,
                expected_revision=body.expected_revision,
                enabled=safety.enabled and safety.agent_rules,
                audited=recorded(runtime, caller, "Changed tool rule", body.tool),
            )
        except AgentEditError as exc:
            raise edit_refusal(exc) from exc

    return router
