"""Offline resource admin callbacks over the actual scoped product services.

Attach to AdminBindings.resource_edge. Input bundles and channel labels are
explicit fixture manifests; no shell, operator config or arbitrary file reads.
Unsupported product edges stay pending, including neutral copy teardown.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.authz import Action, Place, Subject, Surface, authorize, build_agent_ref
from daimon.core.channel_copies import archive_channel_copy
from daimon.core.channel_rules import as_readers, as_writers, set_channel_rule
from daimon.core.channel_skills import add_skill_to_channel, may_set_channel_skills
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_CHANNEL_COPY
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.core.skills.add import add_agent_skill, add_host_agent_skill
from daimon.core.skills.ingest import SkillBundle, SkillIngestError
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.channel_skills import list_channel_skills, remove_channel_skill
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.testing.admin_bindings import AdminBindings, AdminCommand, AdminReceipt
from mux.contracts.ports import ManagedAgents
from mux.contracts.resources import Agent
from mux.errors import ScopeViolation
from pydantic import JsonValue


@dataclass
class ResourceAdminBindings:
    """The optional resource callback, invoked after AdminBindings authorization.

    The neutral host must use this same real account, tenant and sessionmaker.
    Supplying it does not override unsupported native/admin capabilities.
    """

    binding: AdminBindings
    backend: ManagedAgents | None = None
    provider_workspace: str = ""
    operation_key: str = ""
    host: NeutralResourceHost | None = field(default=None, init=False)
    bundles: Mapping[str, SkillBundle] = field(default_factory=dict[str, SkillBundle])
    channel_labels: Mapping[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        if self.backend is not None:
            if self.backend.capabilities().provider != self.binding.backend:
                raise ScopeViolation("qa-admin-resources", "resource provider differs from the run")
            self.host = NeutralResourceHost(
                self.backend,
                self.binding.sessions,
                scope=self.binding.scope,
                account_scope_id=self.provider_workspace,
                operation_key=self.operation_key,
            )

    def pending(self, command: AdminCommand, code: str) -> AdminReceipt:
        return AdminReceipt(
            status="pending", backend=self.binding.backend, operation=command.operation, code=code
        )

    def operation_host(self, command: AdminCommand) -> NeutralResourceHost | None:
        if self.backend is None:
            return None
        bundle = self.bundles.get(command.args[0]) if command.args else None
        digest = hashlib.sha256(
            json.dumps(
                {
                    "operation": command.operation,
                    "channel": command.channel_id,
                    "args": command.args,
                    "options": dict(command.options),
                    "bundle": bundle.preview.content_hash if bundle is not None else None,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return NeutralResourceHost(
            self.backend,
            self.binding.sessions,
            scope=self.binding.scope,
            account_scope_id=self.provider_workspace,
            operation_key=self.operation_key + ":" + digest,
        )

    async def __call__(self, command: AdminCommand) -> AdminReceipt:
        await self.binding.authorize_command(command)
        if self.binding.backend != "anthropic" and self.host is None:
            return self.pending(command, "ADMIN_NEUTRAL_RESOURCE_HOST_UNBOUND")
        if command.operation == "channel_rule_set":
            return await self._fork(command)
        if command.operation == "skills_add":
            return await self._add(command)
        if command.operation == "archive_channel_forks":
            if self.host is not None:
                return self.pending(
                    command,
                    "ADMIN_GEMINI_AGENT_DELETE_UNSUPPORTED"
                    if self.binding.backend == "gemini"
                    else "ADMIN_NEUTRAL_COPY_ARCHIVE_UNBOUND",
                )
            return await self._archive(command)
        if command.operation in {"channel_skills_add", "channel_skills_remove"}:
            if self.host is not None:
                return self.pending(command, "ADMIN_NEUTRAL_CHANNEL_SKILLS_UNBOUND")
            return await self._channel_skill(command)
        return self.pending(
            command,
            {
                "skills_delete": "ADMIN_SKILL_DELETE_PRODUCT_EDGE_UNBOUND",
                "agents_create": "ADMIN_AGENT_SPEC_FIXTURE_UNBOUND",
                "agents_archive": "ADMIN_AGENT_ARCHIVE_UNBOUND",
            }.get(command.operation, "ADMIN_RESOURCE_OPERATION_UNBOUND"),
        )

    async def _fork(self, command: AdminCommand) -> AdminReceipt:
        readers = command.options.get("--readers")
        writers = command.options.get("--writers")
        if (
            (readers is not None and as_readers(readers) is None)
            or (writers is not None and as_writers(writers) is None)
            or readers is writers is None
        ):
            return self.pending(command, "ADMIN_RULE_VALUE_UNBOUND")
        binding = self.binding
        channel = command.channel_id or ""
        host = self.operation_host(command)
        change = await set_channel_rule(
            binding.anthropic,
            binding.sessions,
            tenant_id=uuid.UUID(binding.scope.tenant_id),
            platform=binding.platform,
            channel_id=channel,
            readers=as_readers(readers),
            writers=as_writers(writers),
            subject=Subject(is_admin=True),
            default=binding.default,
            actor_account_id=uuid.UUID(binding.scope.account_id),
            copy="--copy-from" in command.options,
            copy_from=command.options.get("--copy-from"),
            channel_label=self.channel_labels.get(channel),
            resource_host=host,
        )
        async with binding.sessions() as session:
            policy = await load_access_policy(session, tenant_id=uuid.UUID(binding.scope.tenant_id))
            _, channels = await list_propagations_for_tenant(
                session, tenant_id=uuid.UUID(binding.scope.tenant_id)
            )
        if policy.channel_rules.get(channel, type(change.rule)()) != change.rule:
            raise RuntimeError("resource rule readback differs")
        values: dict[str, str] = {}
        name = change.agent_name
        if name is None and change.rule.readers == "own":
            name = next((row.agent_name for row in channels if row.channel_id == channel), None)
            rule = policy.agent_rules.get(name or "")
            if rule is None or rule.runs_in != (channel,):
                name = None
        if name is not None:
            if not any(row.channel_id == channel and row.agent_name == name for row in channels):
                raise RuntimeError("resource copy config readback differs")
            values["fork.name"] = name
            if host is not None:
                copied = await host.find(name)
                if copied is None:
                    raise RuntimeError("resource copy is absent from the retained catalog")
                values.update(
                    {
                        "fork.id": copied.id,
                        "fork.catalog_id": copied.revision.catalog_id,
                        "fork.catalog_revision": str(copied.revision.local_revision),
                    }
                )
        return binding.receipt(
            command,
            {
                "readers": change.rule.readers,
                "writers": change.rule.writers,
                "copied_from": change.copied_from,
                "dropped_skills": list(change.dropped_skills),
            },
        ).model_copy(update={"values": values})

    async def _gate(self, *, name: str, metadata: Mapping[str, str]) -> None:
        # The operator CLI's skill_add gate: managed agents remain immutable,
        # followed by the same fresh policy CONFIGURE decision.
        if (
            decide_operation(
                "skill_add",
                is_admin=True,
                target=TargetFacts(
                    is_daimon_managed=metadata.get("daimon_managed") == "true",
                    is_reachable_in_tenant=False,
                ),
            )
            != "allow"
            or metadata.get(MA_METADATA_KEY_ACCOUNT) is None
        ):
            raise SkillIngestError("Built-in agents take their skills from defaults.")
        async with self.binding.sessions() as session:
            policy = await load_access_policy(
                session, tenant_id=uuid.UUID(self.binding.scope.tenant_id)
            )
        if not authorize(
            policy,
            subject=Subject(is_admin=True),
            action=Action.CONFIGURE,
            surface=Surface.CONFIG,
            place=Place(),
            agent=build_agent_ref(name, metadata),
        ):
            raise SkillIngestError("The agent cannot be configured by this operator.")

    async def _add(self, command: AdminCommand) -> AdminReceipt:
        name = command.options.get("--agent")
        bundle = self.bundles.get(command.args[0])
        if command.options.get("--yes") != "true" or name is None or bundle is None:
            return self.pending(command, "ADMIN_CONFIRMED_SKILL_FIXTURE_UNBOUND")
        binding = self.binding
        host = self.operation_host(command)
        values = {"fork.name": name}
        if host is not None:
            row = await host.find(name)
            if row is None:
                raise SkillIngestError("There is no agent with that name in the owned catalog.")

            async def neutral_gate(agent: Agent) -> None:
                await self._gate(name=agent.spec.name, metadata=agent.spec.metadata or {})

            result = await add_host_agent_skill(
                host, catalog_id=row.revision.catalog_id, bundle=bundle, recheck=neutral_gate
            )
            current = await host.entry(row.revision.catalog_id)
            values.update(
                {
                    "fork.id": current.agent.ref.id,
                    "fork.catalog_id": current.catalog_id,
                    "fork.catalog_revision": str(current.local_revision),
                }
            )
        else:
            agent = await find_agent_by_daimon_tag(
                binding.anthropic, tenant_id=uuid.UUID(binding.scope.tenant_id), name=name
            )
            if agent is None:
                raise SkillIngestError("There is no agent with that name in the owned workspace.")

            async def anthropic_gate(fresh: BetaManagedAgentsAgent) -> None:
                await self._gate(name=fresh.name, metadata=fresh.metadata)

            result = await add_agent_skill(
                binding.anthropic,
                binding.sessions,
                tenant_id=uuid.UUID(binding.scope.tenant_id),
                agent=agent,
                agent_name=name,
                bundle=bundle,
                origin="qa offline fixture",
                added_by_account_id=uuid.UUID(binding.scope.account_id),
                recheck=anthropic_gate,
            )
            values["fork.id"] = agent.id
        return binding.receipt(
            command,
            {
                "name": result.name,
                "skill_id": result.skill_id,
                "version": result.version,
                "action": result.action,
                "newly_attached": result.newly_attached,
            },
        ).model_copy(update={"values": values})

    async def _archive(self, command: AdminCommand) -> AdminReceipt:
        binding = self.binding
        tenant = uuid.UUID(binding.scope.tenant_id)
        channel = command.channel_id or ""
        copies = [
            agent
            for agent in await list_agents_by_tenant(binding.anthropic, tenant_id=tenant)
            if agent.metadata.get(MA_METADATA_KEY_CHANNEL_COPY) == channel
        ]
        archived: list[JsonValue] = []
        for agent in copies:
            result = await archive_channel_copy(
                binding.anthropic,
                binding.sessions,
                tenant_id=tenant,
                name=agent.metadata.get("daimon_name", agent.name),
                closing_channel_id=channel,
                subject=Subject(is_admin=True),
                default=binding.default,
                expected_ma_agent_id=agent.id,
            )
            archived.append(result.agent_id)
        return binding.receipt(command, {"archived": archived})

    async def _channel_skill(self, command: AdminCommand) -> AdminReceipt:
        binding = self.binding
        tenant = uuid.UUID(binding.scope.tenant_id)
        channel, skill = command.channel_id or "", command.args[0]
        if not may_set_channel_skills(Subject(is_admin=True), channel):
            raise ScopeViolation(channel, "operator cannot change channel skills")
        async with binding.sessions() as session, session.begin():
            if command.operation == "channel_skills_add":
                row = await add_skill_to_channel(
                    session,
                    binding.anthropic,
                    tenant_id=tenant,
                    platform=binding.platform,
                    channel_id=channel,
                    skill=skill,
                    default=binding.default,
                    actor_account_id=uuid.UUID(binding.scope.account_id),
                )
                if isinstance(row, str):
                    return AdminReceipt(
                        status="refused",
                        backend=binding.backend,
                        operation=command.operation,
                        code="ADMIN_CHANNEL_SKILL_" + row.upper(),
                    )
                skill_id = row.skill_id
            else:
                rows = await list_channel_skills(
                    session, tenant_id=tenant, platform=binding.platform, channel_id=channel
                )
                match = next(
                    (row for row in rows if skill.strip() in (row.skill_id, row.name)), None
                )
                skill_id = match.skill_id if match is not None else skill
                if match is not None:
                    await remove_channel_skill(
                        session,
                        tenant_id=tenant,
                        platform=binding.platform,
                        channel_id=channel,
                        skill_id=skill_id,
                    )
        async with binding.sessions() as session:
            rows = await list_channel_skills(
                session, tenant_id=tenant, platform=binding.platform, channel_id=channel
            )
        held = any(row.skill_id == skill_id for row in rows)
        if held != (command.operation == "channel_skills_add"):
            raise RuntimeError("channel skill readback differs")
        return binding.receipt(command, {"skill_id": skill_id, "attached": held})
