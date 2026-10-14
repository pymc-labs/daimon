"""Native attached-delete refusal and read-only Anthropic catalog observations.

Attach to AdminBindings.resource_edge, optionally delegating other operations.
This edge never sends a DELETE, fabricates a provider receipt or certifies a
whole scenario. Native unattached deletion remains unavailable at the host edge.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.core.channel_skills import (
    NativeSkillDeleteGuard,
    NativeSkillDeleteUnavailable,
    NativeSkillStillAttached,
    refuse_native_skill_delete,
)
from daimon.core.defaults.ma_index import list_agents_by_tenant, list_skills_strict
from daimon.core.defaults.metadata import strip_tenant_prefix
from daimon.core.stores.channel_skills import list_channel_skills
from daimon.testing.admin_bindings import AdminBindings, AdminCommand, AdminReceipt, ResourceEdge
from mux.errors import ScopeViolation
from pydantic import JsonValue


@dataclass
class SkillDeleteAdminBindings:
    binding: AdminBindings
    provider_workspace: str = ""
    operator_enabled: bool = False
    delegate: ResourceEdge | None = None

    def pending(
        self, command: AdminCommand, code: str, observations: dict[str, JsonValue] | None = None
    ) -> AdminReceipt:
        return AdminReceipt(
            status="pending",
            backend=self.binding.backend,
            operation=command.operation,
            code=code,
            observations=observations or {},
        )

    async def __call__(self, command: AdminCommand) -> AdminReceipt:
        await self.binding.authorize_command(command)
        if command.operation != "skills_delete":
            if self.delegate is None:
                return self.pending(command, "ADMIN_RESOURCE_EDGE_UNBOUND")
            result = await self.delegate(command)
            if result.backend != self.binding.backend or result.operation != command.operation:
                raise ScopeViolation(
                    "skill-delete-admin", "delegate receipt differs from owned operation"
                )
            return result
        if len(command.args) != 1 or dict(command.options) != {"--yes": "true"}:
            return self.pending(command, "ADMIN_SKILL_DELETE_SHAPE_UNBOUND")
        backend = self.binding.backend
        if backend == "anthropic":
            return await self._observe_anthropic(command)
        guard = NativeSkillDeleteGuard(
            scope=self.binding.scope,
            provider=backend,
            account_scope_id=self.provider_workspace,
            operator_enabled=self.operator_enabled,
        )
        try:
            await refuse_native_skill_delete(
                self.binding.sessions, guard=guard, skill=command.args[0]
            )
        except NativeSkillStillAttached as refusal:
            return AdminReceipt(
                status="refused",
                backend=backend,
                operation=command.operation,
                code="ADMIN_NATIVE_SKILL_STILL_ATTACHED",
                observations={
                    "message": str(refusal),
                    "exit_code": 1,
                    "deleted": False,
                    "skill_id": refusal.skill_id,
                    "agent_revisions": [[id_, revision] for id_, revision in refusal.agents],
                    "channels": list(refusal.channels),
                },
            )
        except NativeSkillDeleteUnavailable as unavailable:
            return self.pending(
                command,
                "ADMIN_NATIVE_SKILL_DELETE_" + unavailable.reason.upper(),
                {"message": str(unavailable), "deleted": False},
            )

    async def _observe_anthropic(self, command: AdminCommand) -> AdminReceipt:
        # Observation only: do not add a product guard to the Anthropic path or
        # substitute these reads for the authored delete operation.
        tenant_id = uuid.UUID(self.binding.scope.tenant_id)
        skill = command.args[0]
        rows = await list_skills_strict(self.binding.anthropic)
        matches: set[str] = set()
        for row in rows:
            body = strip_tenant_prefix(tenant_id=tenant_id, display_title=row.display_title or "")
            if (
                row.source == "custom"
                and body is not None
                and skill in (row.id, body, body.rsplit("/", 1)[-1])
            ):
                matches.add(row.id)
        agents = await list_agents_by_tenant(self.binding.anthropic, tenant_id=tenant_id)
        held = [
            agent.id
            for agent in agents
            if any(pin.skill_id in matches for pin in agent.skills or ())
        ]
        async with self.binding.sessions() as session:
            channels = await list_channel_skills(
                session, tenant_id=tenant_id, platform=self.binding.platform
            )
        observations: dict[str, JsonValue] = {
            "skill_ids": [id_ for id_ in sorted(matches)],
            "agents": [id_ for id_ in held],
            "channels": [row.channel_id for row in channels if row.skill_id in matches],
            "observation_only": True,
            "deleted": False,
        }
        return self.pending(
            command,
            "ADMIN_ANTHROPIC_SKILL_DELETE_OBSERVATION_ONLY",
            observations,
        )
