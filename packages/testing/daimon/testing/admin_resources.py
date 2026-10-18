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
from datetime import UTC, datetime
from typing import Literal

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.authz import Action, Place, Subject, Surface, authorize, build_agent_ref
from daimon.core.channel_copies import archive_channel_copy
from daimon.core.channel_rules import as_readers, as_writers, set_channel_rule
from daimon.core.channel_skills import add_skill_to_channel, may_set_channel_skills
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag, list_agents_by_tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_CHANNEL_COPY
from daimon.core.mux_backend import resource_scope
from daimon.core.neutral_resources import NativeCatalogReadback, NeutralResourceHost
from daimon.core.operation_policy import TargetFacts, decide_operation
from daimon.core.skills.add import add_agent_skill, add_host_agent_skill
from daimon.core.skills.ingest import SkillBundle, SkillIngestError
from daimon.core.specs import SkillRef
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.channel_skills import list_channel_skills, remove_channel_skill
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.testing.admin_bindings import AdminBindings, AdminCommand, AdminReceipt
from mux.contracts._base import Contract
from mux.contracts.ids import ResourceRef, Scope
from mux.contracts.ports import ManagedAgents
from mux.contracts.receipts import CancelReceipt, Operation, SendReceipt, StopObservation
from mux.contracts.resources import Agent
from mux.drivers.anthropic.sessions_lifecycle import SessionReads
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from pydantic import Field, JsonValue, model_validator


@dataclass
class ResourceAdminBindings:
    """The optional resource callback, invoked after AdminBindings authorization.

    The neutral host must use this same real account, tenant and sessionmaker.
    Supplying it does not override unsupported native/admin capabilities.
    """

    binding: AdminBindings
    backend: ManagedAgents | None = None
    resource_catalog_enabled: bool = False
    catalog_adapter: NativeCatalogReadback | None = None
    provider_workspace: str = ""
    operation_key: str = ""
    host: NeutralResourceHost | None = field(default=None, init=False)
    bundles: Mapping[str, SkillBundle] = field(default_factory=dict[str, SkillBundle])
    channel_labels: Mapping[str, str] = field(default_factory=dict[str, str])
    # `load_platform_skills`' map, for a channel skill added on the default agent.
    platform_skills: Mapping[str, tuple[SkillRef, ...]] = field(
        default_factory=dict[str, tuple[SkillRef, ...]]
    )

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
                resource_catalog_enabled=self.resource_catalog_enabled,
                catalog_adapter=self.catalog_adapter,
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
            resource_catalog_enabled=self.resource_catalog_enabled,
            catalog_adapter=self.catalog_adapter,
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
                    platform_skills=self.platform_skills,
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


class ResourceManifestEntry(Contract):
    """Safe owner-captured identities, never a cleanup permission or raw SDK body."""

    scope: Scope
    run_id: uuid.UUID
    binding_id: str = Field(min_length=1)
    resource: ResourceRef | None = None
    creation_key: str | None = None
    creation: Operation | None = None
    creation_ack: ResourceRef | None = None
    actions: tuple[Literal["cancel", "archive", "delete"], ...] = ()
    input: SendReceipt | None = None
    agent_id: str | None = None
    environment_id: str | None = None

    @model_validator(mode="after")
    def scoped(self) -> ResourceManifestEntry:
        if (
            not self.binding_id.strip()
            or self.scope.is_platform
            or self.scope.is_legacy_host_authorized
            or not all(
                value and value.strip()
                for value in (
                    self.scope.tenant_id,
                    self.scope.account_id,
                    self.scope.principal_id,
                    self.scope.authorization_id,
                )
            )
            or len(set(self.actions)) != len(self.actions)
        ):
            raise ValueError("manifest scope or actions are invalid")
        for ref in (
            self.resource,
            self.creation_ack,
            self.creation.resource if self.creation else None,
        ):
            if ref is not None and (
                ref.tenant_id != self.scope.tenant_id
                or not ref.id
                or ref.account_id
                not in (
                    self.scope.account_id,
                    resource_scope(tenant_id=self.scope.tenant_id).account_id,
                )
            ):
                raise ValueError("manifest reference is outside its tenant")
        if self.creation is not None and (
            self.creation.created_at.utcoffset() is None
            or self.creation.updated_at.utcoffset() is None
        ):
            raise ValueError("creation receipt timestamps must be aware")
        if (
            self.creation is not None
            and self.creation.status in ("accepted", "processed")
            and (
                not self.creation_key
                or self.creation.key != self.creation_key
                or self.creation.resource not in (None, self.resource)
                or self.creation_ack != self.resource
                or self.resource is None
                or self.resource.account_id != self.scope.account_id
                or not self.resource.account_scope_id
            )
        ):
            raise ValueError("creation lacks its exact scoped acknowledgement")
        if (
            self.creation is not None
            and self.creation.status not in ("accepted", "processed")
            and (self.resource is not None or self.creation_ack is not None)
        ):
            raise ValueError("unknown creation has no acknowledged native identity")
        return self

    @property
    def ownership(self) -> Literal["created", "borrowed", "unknown"]:
        if self.creation is not None:
            return "created" if self.creation.status in ("accepted", "processed") else "unknown"
        return "borrowed" if self.resource is not None else "unknown"

    @property
    def eligible_actions(self) -> tuple[Literal["cancel", "archive", "delete"], ...]:
        """Signed ceiling narrowed by observed ownership; not live authorization."""
        if self.ownership == "unknown":
            return ()
        return tuple(
            action
            for action in self.actions
            if (
                action == "cancel"
                and self.resource is not None
                and self.resource.kind == "session"
                and self.input is not None
                and self.input.status != "outcome_unknown"
                and self.input.turn_id is not None
            )
            or (action != "cancel" and self.ownership == "created")
        )


def freeze_resource_manifest(
    entries: tuple[ResourceManifestEntry, ...],
) -> tuple[ResourceManifestEntry, ...]:
    """Validate one run without inventing IDs for unknown native allocations."""
    checked = tuple(ResourceManifestEntry.model_validate(row.model_dump()) for row in entries)
    if not checked:
        raise ValueError("manifest is empty")
    root = checked[0]
    seen: set[tuple[str, str, str, str]] = set()
    for row in checked:
        if (row.run_id, row.scope.tenant_id, row.scope.account_id, row.scope.principal_id) != (
            root.run_id,
            root.scope.tenant_id,
            root.scope.account_id,
            root.scope.principal_id,
        ):
            raise ValueError("manifest entries belong to different runs or principals")
        ref = row.resource
        if ref is not None:
            key = ref.provider, ref.account_scope_id, ref.kind, ref.id
            if key in seen:
                raise ValueError("manifest repeats a native resource")
            seen.add(key)
    return checked


@dataclass(frozen=True)
class CleanupReadback:
    status: Literal["verified", "pending", "retained"]
    reason: str
    resource: ResourceRef | None
    stopped: bool | None = None
    archived: bool | None = None
    stop: StopObservation | None = None
    observed_at: datetime | None = None
    archived_at: datetime | None = None


def _cleanup_target(
    backend: ManagedAgents, entry: ResourceManifestEntry, action: str
) -> ResourceRef | None:
    row = freeze_resource_manifest((entry,))[0]
    if action not in row.actions:
        raise ScopeViolation("cleanup", "action is outside the pinned manifest")
    if row.ownership == "unknown":
        return None
    if action != "cancel" and row.ownership != "created":
        return None
    ref = row.resource
    if (
        ref is None
        or ref.kind != "session"
        or ref.account_id != row.scope.account_id
        or ref.provider != backend.capabilities().provider
    ):
        raise ScopeViolation("cleanup", "session is outside the pinned account")
    return ref


async def verify_cancel_readback(
    backend: ManagedAgents,
    entry: ResourceManifestEntry,
    receipt: CancelReceipt,
    *,
    operation_id: str,
    deadline: datetime,
) -> CleanupReadback:
    """Read the actual owner stop observation; never perform cancellation."""
    ref = _cleanup_target(backend, entry, "cancel")
    if ref is None:
        return CleanupReadback("retained", "creation_unknown", entry.resource)
    if (
        receipt.operation_id != operation_id
        or not operation_id
        or receipt.session != ref
        or entry.input is None
        or entry.input.status == "outcome_unknown"
        or not entry.input.turn_id
        or receipt.turn_id != entry.input.turn_id
        or receipt.requested_at.utcoffset() is None
        or deadline.utcoffset() is None
    ):
        raise ScopeViolation("cleanup", "cancel receipt differs from the admitted input")
    try:
        observed = await backend.events.wait_stopped(entry.scope, receipt, deadline=deadline)
    except (ProviderError, UnsupportedCapability):
        return CleanupReadback("pending", "stop_readback_unavailable", ref)
    if (
        observed.receipt_operation_id != receipt.operation_id
        or observed.observed_at.utcoffset() is None
        or observed.observed_at < receipt.requested_at
    ):
        raise ScopeViolation("cleanup", "stop observation differs from the cancel operation")
    if not observed.stopped or observed.outcome is None:
        return CleanupReadback(
            "pending", "native_stop_unobserved", ref, stopped=False, stop=observed
        )
    return CleanupReadback("verified", "native_stop_observed", ref, stopped=True, stop=observed)


async def verify_archive_readback(
    backend: ManagedAgents,
    entry: ResourceManifestEntry,
    receipt: Operation,
    *,
    operation_id: str,
) -> CleanupReadback:
    """Read independently archived state; never infer native stop or billing finality."""
    ref = _cleanup_target(backend, entry, "archive")
    if ref is None:
        return CleanupReadback("retained", entry.ownership + "_resource", entry.resource)
    if (
        not operation_id
        or receipt.id != operation_id
        or receipt.key != operation_id
        or receipt.resource != ref
    ):
        raise ScopeViolation("cleanup", "archive receipt differs from the pinned resource")
    if receipt.status not in ("accepted", "processed"):
        return CleanupReadback("pending", "archive_ack_unknown", ref)
    try:
        reader = backend.extension(SessionReads, namespace="anthropic.session_reads", version=1)
    except UnsupportedCapability:
        return CleanupReadback("retained", "archive_readback_unsupported", ref)
    try:
        native = await reader.read_native(entry.scope, ref)
    except ProviderError:
        return CleanupReadback("pending", "archive_readback_unavailable", ref)
    metadata, agent = native.get("metadata"), native.get("agent")
    if (
        native.get("id") != ref.id
        or not isinstance(metadata, dict)
        or metadata.get("daimon_tenant") != entry.scope.tenant_id
        or metadata.get("daimon_account") != entry.scope.account_id
        or not entry.agent_id
        or not entry.environment_id
        or not isinstance(agent, dict)
        or agent.get("id") != entry.agent_id
        or native.get("environment_id") != entry.environment_id
    ):
        raise ScopeViolation("cleanup", "session readback differs from pinned ownership")
    archived = native.get("archived_at")
    try:
        at = datetime.fromisoformat(archived) if isinstance(archived, str) else None
    except ValueError:
        at = None
    if at is None or at.utcoffset() is None:
        return CleanupReadback("pending", "archive_unobserved", ref, archived=False)
    if entry.creation is None or at < entry.creation.created_at:
        return CleanupReadback("pending", "archive_timestamp_stale", ref, archived=False)
    return CleanupReadback(
        "verified",
        "archive_observed",
        ref,
        archived=True,
        observed_at=datetime.now(UTC),
        archived_at=at,
    )
