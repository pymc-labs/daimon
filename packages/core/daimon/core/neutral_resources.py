"""Authorized host catalog edits over immutable native resource ports.

Local catalog revisions never become provider revision pins. Uploaded bundles
are retained before attachment; a new native agent is published with catalog
CAS. Existing sessions keep their old native agent and workspace binding.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, cast
from urllib.parse import urlsplit

import structlog
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import (
    Action,
    AgentRef,
    DenyReason,
    FleetFacts,
    Subject,
    Surface,
    authorize,
    build_agent_ref,
)
from daimon.core.constants import AGENT_SKILL_CAP
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_ISOLATED,
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_READER_SOURCE,
    build_metadata,
    strip_tenant_prefix,
    tenant_scoped_display_title,
)
from daimon.core.errors import DaimonError
from daimon.core.mux_backend import resource_scope
from daimon.core.skills.ingest import SkillBundle, SkillIngestError, bundle_from_upload
from daimon.core.skills.service import AddedSkill, StoredSkillUpload, UploadedSkill, add_skill
from daimon.core.stores import security_audit
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.neutral_catalog import (
    AgentRevision,
    CatalogConflict,
    PostgresNeutralCatalog,
    SkillVersion,
)
from mux.contracts._base import Contract, FrozenMap
from mux.contracts.config import BackendConfig, ConfigRevision, ResolvedBackend, resolve_default
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ModelRef, Provider, ResourceRef, Revision, Scope, SkillRef
from mux.contracts.ports import Agents, ManagedAgents
from mux.contracts.profile import SUPPORTED, Capability, Profile
from mux.contracts.resources import (
    Agent,
    AgentPatch,
    AgentSpec,
    EnvironmentSpec,
    MCPConnection,
    ResourceBinding,
    SkillUpload,
    SkillUploadFile,
    ToolSpec,
)
from mux.errors import InvalidConfig, ProviderError, ScopeViolation, UnsupportedCapability
from mux.profiles import (
    MANAGED_AGENTS,
    get_profile,
    native_agent_tools,
    neutral_catalog_profile_for,
)
from mux.state.lease import Slot
from mux.state.operations import request_digest
from mux.state.store import StateStore
from pydantic import Field, JsonValue, TypeAdapter, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    from daimon.core.turn.admission import AdmissionGrant
    from daimon.core.turn.persistence import TurnPersistence

AgentGate = Callable[[Agent], Awaitable[None]]
PinResolver = Callable[[Scope, SkillRef], Awaitable[SkillRef]]
_log = structlog.get_logger(__name__)


class NativeCatalogReadback(Agents, Protocol):
    """Explicit provider-owned readback, without SDK or credential access."""

    @property
    def provider(self) -> str: ...

    @property
    def profile_id(self) -> str: ...

    @property
    def account_scope_id(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    async def verify(self, scope: Scope, retained: Agent) -> Agent: ...


@dataclass(frozen=True)
class HostAgentView:
    """Policy-facing identity, without a provider SDK model."""

    revision: AgentRevision

    @property
    def id(self) -> str:
        return self.revision.agent.ref.id

    @property
    def name(self) -> str:
        return self.revision.agent.spec.name

    @property
    def metadata(self) -> Mapping[str, str]:
        return self.revision.agent.spec.metadata or {}


@dataclass(frozen=True)
class HostAgentCopy:
    agent: HostAgentView
    dropped_skills: tuple[str, ...]
    copied_skills: tuple[str, ...] = ()


def skill_upload(bundle: SkillBundle) -> SkillUpload:
    checked = bundle_from_upload(bundle.zip_bytes, filename="SKILL.zip")
    if checked != bundle:
        raise SkillIngestError("The skill preview doesn't match its checked upload.")
    with zipfile.ZipFile(io.BytesIO(bundle.zip_bytes)) as archive:
        return SkillUpload(
            files=tuple(
                SkillUploadFile(path=path, content=archive.read(f"{bundle.preview.name}/{path}"))
                for path in bundle.preview.files
            )
        )


class NeutralResourceHost:
    """Explicit tenant/account/workspace edge, supplied by the authorized host.

    An SDK client is never accepted. The catalog holds authored intent, which
    may include harness tools compiled out of the provider agent definition.
    All resource operations use the injected driver and this exact Scope.
    """

    def __init__(
        self,
        backend: ManagedAgents,
        sessions: async_sessionmaker[AsyncSession],
        *,
        scope: Scope,
        account_scope_id: str,
        operation_key: str,
        resolve_pin: PinResolver | None = None,
        resource_catalog_enabled: bool = False,
        catalog_adapter: NativeCatalogReadback | None = None,
    ) -> None:
        if (
            scope.is_platform
            or scope.is_legacy_host_authorized
            or not scope.principal_id.strip()
            or not scope.authorization_id.strip()
            or not account_scope_id.strip()
            or not operation_key.strip()
        ):
            raise ScopeViolation("resource-host", "explicit scoped host authorization required")
        self.backend = backend
        self.scope = scope
        self.workspace = account_scope_id
        self.operation_key = operation_key
        self.resolve_pin = resolve_pin
        capabilities = backend.capabilities()
        profile = neutral_catalog_profile_for(capabilities.provider)
        if profile is None:
            raise UnsupportedCapability(
                ("neutral_resource_host_provider",), capabilities.profile_id
            )
        self.profile = profile
        self._catalog_adapter: NativeCatalogReadback | None = None
        if profile.catalog_readback == "verify" and (
            resource_catalog_enabled is not False
            or catalog_adapter is not None
            or getattr(backend, "catalog_adapter", None) is not None
        ):
            if (
                resource_catalog_enabled is not True
                or catalog_adapter is None
                or catalog_adapter.enabled is not True
                or catalog_adapter.provider != profile.provider
                or catalog_adapter.profile_id != profile.profile_id
                or capabilities.profile_id != profile.profile_id
                or catalog_adapter.account_scope_id != account_scope_id
                or backend.agents is not catalog_adapter
            ):
                raise UnsupportedCapability(
                    ("neutral_resource_host_after_g2",), capabilities.profile_id
                )
            self._catalog_adapter = catalog_adapter
        elif capabilities.profile_id != profile.profile_id:
            raise UnsupportedCapability(("neutral_resource_host_profile",), capabilities.profile_id)
        self.catalog = PostgresNeutralCatalog(
            sessions, provider=profile.provider, account_scope_id=account_scope_id
        )

    def check(self, agent: Agent) -> None:
        ref = agent.ref
        if (
            ref.provider != self.backend.capabilities().provider
            or agent.spec.model.provider != ref.provider
            or ref.kind != "agent"
            or ref.account_scope_id != self.workspace
            or ref.tenant_id != self.scope.tenant_id
            or ref.account_id != self.scope.account_id
        ):
            raise ScopeViolation(ref.id, "agent is outside the authorized resource host")

    async def entry(self, catalog_id: str) -> AgentRevision:
        # Catalog account authorization precedes the first native request.
        row = await self.catalog.get_agent(self.scope, catalog_id)
        if row is None or row.agent.lifecycle != "active":
            raise SkillIngestError("That agent is no longer in the authorized catalog.")
        self.check(row.agent)
        return row

    async def read(
        self,
        catalog_id: str,
        *,
        store: StateStore | None = None,
        key: str | None = None,
        worker: ResolvedWorkerSpec | None = None,
        source: Agent | None = None,
    ) -> AgentRevision:
        row = await self.entry(catalog_id)
        if any(value is not None for value in (store, key, worker, source)):
            from daimon.core.turn.persistence import UncertainSend

            if store is None or key is None or worker is None or source is None:
                raise ScopeViolation(catalog_id, "worker readback needs the owned operation")
            record = await store.get_operation(self.scope, key)
            if record is None or record.slot is None or record.operation.status != "processed":
                raise UncertainSend("worker publication has no committed acknowledgement")
            payload = self._publication_request(
                catalog_id, worker.spec, 0, worker, source, record.slot
            )
            acknowledged = Agent.model_validate(dict(record.result))
            if (
                key != catalog_id.removesuffix(":agent") + ":agent_create:0"
                or record.operation.request_digest
                != request_digest({"session": None, "kind": "agent_create", "request": payload})
                or record.operation.resource is not None
                or acknowledged.ref != row.agent.ref
                or acknowledged.revision != row.agent.revision
                or row.local_revision != 1
                or row.agent.spec != worker.spec
            ):
                raise CatalogConflict("worker differs from its owned publication acknowledgement")
        if self._catalog_adapter is not None:
            fresh = await self._catalog_adapter.verify(self.scope, row.agent)
            self.check(fresh)
            if (
                fresh.spec != row.agent.spec
                or fresh.ref != row.agent.ref
                or fresh.revision != row.agent.revision
                or fresh.lifecycle != row.agent.lifecycle
            ):
                raise CatalogConflict("native agent differs from the retained catalog revision")
            return row
        fresh = await self.backend.agents.retrieve(self.scope, row.agent.ref)
        self.check(fresh)
        tools = native_agent_tools(self.profile, row.agent.spec)
        if (
            fresh.ref != row.agent.ref
            or fresh.spec.name != row.agent.spec.name
            or fresh.spec.model != row.agent.spec.model
            or fresh.spec.system != row.agent.spec.system
            or fresh.spec.skills != row.agent.spec.skills
            or tuple(fresh.spec.tools or ()) != tools
            or tuple(fresh.spec.mcp_servers or ()) != tuple(row.agent.spec.mcp_servers or ())
            or (
                worker is not None
                and (
                    fresh.revision != row.agent.revision
                    or fresh.spec.description != row.agent.spec.description
                    or any(
                        (fresh.spec.metadata or {}).get(name) != value
                        for name, value in (row.agent.spec.metadata or {}).items()
                    )
                )
            )
            or any(
                (fresh.spec.metadata or {}).get(key) != (row.agent.spec.metadata or {}).get(key)
                for key in (
                    MA_METADATA_KEY_NAME,
                    MA_METADATA_KEY_MANAGED,
                    MA_METADATA_KEY_ISOLATED,
                    MA_METADATA_KEY_READER_SOURCE,
                )
            )
        ):
            raise CatalogConflict("native agent differs from the retained catalog revision")
        return row

    def _publication_request(
        self,
        catalog_id: str,
        spec: AgentSpec,
        expected_revision: int,
        worker: ResolvedWorkerSpec,
        source: Agent,
        slot: Slot,
    ) -> dict[str, JsonValue]:
        fleet = worker.permissions.fleet
        prefix = f"fleet/{fleet.run_id}/"
        ordinal = slot.thread.thread_id.removeprefix(prefix)
        if (
            not slot.thread.thread_id.startswith(prefix)
            or not ordinal.isdecimal()
            or str(int(ordinal)) != ordinal
            or catalog_id != f"fleet:{fleet.run_id}:spawn:{ordinal}:agent"
            or expected_revision != 0
            or slot.thread.channel != fleet.root.channel
            or slot.account_id != self.scope.account_id
            or fleet.scope != self.scope
            or fleet.depth != 1
            or not fleet.enabled
            or not fleet.active
            or worker.spec != spec
            or worker.permissions.spec != spec
            or worker.backend.profile != self.profile.profile_id
            or spec.model.provider != self.profile.provider
            or source.ref.kind != "agent"
            or source.ref.provider != source.spec.model.provider
            or source.ref.tenant_id != self.scope.tenant_id
            or source.lifecycle != "active"
            or source.archived_at is not None
            or (
                worker.source_agent is not None
                and worker.source_agent != (source.ref, source.revision)
            )
        ):
            raise ScopeViolation(catalog_id, "publication differs from the canonical worker claim")
        return cast(
            dict[str, JsonValue],
            {
                "catalog": catalog_id,
                "workspace": self.workspace,
                "slot": slot.model_dump(mode="json"),
                "scope": self.scope.model_dump(mode="json"),
                "origin": str(fleet.origin_id),
                "root": fleet.root.model_dump(mode="json"),
                "place": asdict(fleet.place),
                "run": str(fleet.run_id),
                "agent": str(worker.permissions.agent_id),
                "backend": worker.backend.model_dump(mode="json"),
                "spec": spec.model_dump(mode="json", exclude_unset=True),
                "source": (
                    [part.model_dump(mode="json") for part in worker.source_agent]
                    if worker.source_agent is not None
                    else None
                ),
                "captured_source": request_digest(
                    cast(JsonValue, source.model_dump(mode="json", exclude_unset=True))
                ),
                "resources": [item.model_dump(mode="json") for item in worker.resources],
                "environment": (
                    worker.environment.model_dump(mode="json")
                    if worker.environment is not None
                    else None
                ),
                "limits": worker.limits.model_dump(mode="json"),
                "seals": sorted(worker.permissions.seal_ids),
                "memory_read_only": worker.permissions.memory_read_only,
            },
        )

    async def agents(self) -> tuple[AgentRevision, ...]:
        rows = await self.catalog.list_agents(self.scope)
        for row in rows:
            self.check(row.agent)
        return tuple(row for row in rows if row.agent.lifecycle == "active")

    async def find(self, name: str) -> HostAgentView | None:
        matches = [
            row
            for row in await self.agents()
            if (row.agent.spec.metadata or {}).get(MA_METADATA_KEY_NAME, row.agent.spec.name)
            == name
        ]
        if len(matches) > 1:
            raise CatalogConflict("more than one catalog agent has that name")
        return None if not matches else HostAgentView(matches[0])

    async def publish(
        self,
        catalog_id: str,
        spec: AgentSpec,
        *,
        expected_revision: int,
        key: str,
        persistence: TurnPersistence | None = None,
        guard: Callable[[], Awaitable[None]] | None = None,
        worker: ResolvedWorkerSpec | None = None,
        source: Agent | None = None,
    ) -> AgentRevision:
        """Publish authored intent through the worker's existing native mutation owner."""
        owned = any(value is not None for value in (persistence, guard, worker, source))
        if owned:
            if persistence is None or guard is None or worker is None or source is None:
                raise ScopeViolation(catalog_id, "publication needs complete owned authority")
            if persistence.scope != self.scope or persistence.operation_key + ":agent" != key:
                raise ScopeViolation(catalog_id, "publication differs from the owned invocation")
            payload = self._publication_request(
                catalog_id, spec, expected_revision, worker, source, persistence.slot
            )
            await guard()

            async def preallocate() -> None:
                await guard()
                if await self.catalog.get_agent(self.scope, catalog_id) is not None:
                    raise CatalogConflict("agent changed before native creation")

            async def create(native_key: str) -> Agent:
                actual = await self.backend.agents.create(self.scope, spec, key=native_key)
                self.check(actual)
                return actual

            created = await persistence.mutate(
                None,
                "agent_create",
                payload,
                create,
                Agent,
                lambda _: "processed",
                guard=preallocate,
                retry_failed=False,
            )
            current = await self.catalog.get_agent(self.scope, catalog_id)
            if current is not None:
                if (
                    current.local_revision != 1
                    or current.agent.ref != created.ref
                    or current.agent.revision != created.revision
                    or current.agent.spec != spec
                ):
                    raise CatalogConflict("catalog differs from the acknowledged publication")
                return current
        else:
            current = await self.catalog.get_agent(self.scope, catalog_id)
            if (current.local_revision if current is not None else 0) != expected_revision:
                raise CatalogConflict("agent changed before native creation")
            created = await self.backend.agents.create(self.scope, spec, key=key)
        self.check(created)
        # Keep authored intent, rather than losing compiled tool declarations on edits.
        authored = created.model_copy(update={"spec": spec})
        published = False
        try:
            row = await self.catalog.put_agent(
                self.scope, catalog_id, authored, expected_revision=expected_revision
            )
            published = True
            return row
        finally:
            # A lost CAS or commit ACK never proves the native identity unpublished.
            if not published:
                _log.warning(
                    "neutral_resources.agent_publication_failed_native_retained",
                    catalog_id=catalog_id,
                    native_agent_id=created.ref.id,
                )

    async def concrete(self, pin: SkillRef) -> SkillRef:
        if pin.version is not None:
            return pin
        if self.resolve_pin is None:
            raise UnsupportedCapability(
                ("neutral_concrete_skill_pin",), self.backend.capabilities().profile_id
            )
        resolved = await self.resolve_pin(self.scope, pin)
        if resolved.id != pin.id or resolved.version in (None, "latest"):
            raise CatalogConflict("native skill resolver did not return the exact concrete pin")
        return resolved

    async def retained(self, pin: SkillRef) -> SkillVersion:
        concrete = await self.concrete(pin)
        if concrete.version is None:
            raise CatalogConflict("a concrete retained version is required")
        row = await self.catalog.get_skill(self.scope, concrete.id, concrete.version)
        if row is None:
            raise UnsupportedCapability(
                ("neutral_retained_skill_bundle",), self.backend.capabilities().profile_id
            )
        return row

    async def add(self, catalog_id: str, bundle: SkillBundle, *, recheck: AgentGate) -> AddedSkill:
        row = await self.read(catalog_id)
        await recheck(row.agent)
        host = _NeutralSkillAddHost(self, catalog_id, row.agent.spec.name, recheck)
        return await add_skill(host, agent=row.agent, agent_name=row.agent.spec.name, bundle=bundle)

    async def fork(
        self,
        source_id: str,
        new_name: str,
        *,
        recheck: AgentGate,
        public_url: str | None,
        extra_metadata: Mapping[str, str] | None = None,
    ) -> HostAgentCopy:
        from daimon.core.agent_guidance import apply_credential_guidance

        source = await self.read(source_id)
        await recheck(source.agent)
        if await self.find(new_name) is not None:
            raise SkillIngestError(f"An agent named {new_name} already exists. Pick another name.")
        metadata = build_metadata(
            tenant_id=uuid.UUID(self.scope.tenant_id),
            account_id=uuid.UUID(self.scope.account_id),
            name=new_name,
        )
        if any(
            key in metadata and metadata[key] != value
            for key, value in (extra_metadata or {}).items()
        ):
            raise ScopeViolation(new_name, "copy metadata cannot override its scoped identity")
        metadata.update(extra_metadata or {})
        owned: list[tuple[SkillRef, SkillVersion]] = []
        kept: list[SkillRef] = []
        dropped: list[str] = []
        catalog = await self.catalog.list_skills(self.scope)
        names = {row.agent.spec.name for row in await self.agents()}
        for pin in source.agent.spec.skills or ():
            records = [row for row in catalog if row.skill_id == pin.id]
            if not records:
                raise UnsupportedCapability(
                    ("neutral_retained_skill_ownership",), self.backend.capabilities().profile_id
                )
            owners = {row.agent_name for row in records}
            if owners == {source.agent.spec.name}:
                owned.append((pin, await self.retained(pin)))
            elif all(
                row.agent_name not in names
                and (
                    body := strip_tenant_prefix(
                        tenant_id=uuid.UUID(self.scope.tenant_id), display_title=row.display_title
                    )
                )
                and "/" not in body
                for row in records
            ):
                # A retained unscoped library skill preserves its original pin.
                kept.append(pin)
            else:
                dropped.append(records[0].bundle.preview.name)
        copied: list[str] = []
        for position, (_pin, record) in enumerate(owned):
            fresh = await self.read(source_id)
            await recheck(fresh.agent)
            if fresh.local_revision != source.local_revision:
                raise CatalogConflict("source changed while its skills were being copied")
            try:
                created = await self.backend.skills.create(
                    self.scope,
                    skill_upload(record.bundle),
                    key=f"{self.operation_key}:copy:skill:{position}",
                )
                concrete = created.latest_version
                if (
                    concrete is None
                    or concrete.id != created.id
                    or concrete.version in (None, "latest")
                ):
                    raise CatalogConflict("copied skill has no concrete native version")
                await self.catalog.put_skill(
                    self.scope,
                    skill_id=created.id,
                    version=concrete.version,
                    agent_name=new_name,
                    display_title=tenant_scoped_display_title(
                        tenant_id=uuid.UUID(self.scope.tenant_id),
                        name=record.bundle.preview.name,
                        agent_name=new_name,
                    ),
                    bundle=record.bundle,
                )
            except (ProviderError, SkillIngestError, CatalogConflict):
                _log.warning("neutral_resources.skill_copy_failed")
                dropped.append(record.bundle.preview.name)
                continue
            kept.append(concrete)
            copied.append(f"{new_name}/{record.bundle.preview.name}")
        fresh = await self.read(source_id)
        await recheck(fresh.agent)
        if fresh.local_revision != source.local_revision:
            raise CatalogConflict("source changed before native copy creation")
        original = source.agent.spec
        servers = tuple(
            server for server in original.mcp_servers or () if server.credential_ref is None
        )
        if public_url is not None:
            servers = tuple(server for server in servers if server.name != "daimon-mcp") + (
                MCPConnection(name="daimon-mcp", url=public_url),
            )
        server_names = {server.name for server in servers}
        tools = tuple(
            tool
            for tool in original.tools or ()
            if tool.kind != "mcp_toolset" or tool.name in server_names
        )
        execution = self.profile.execution_tool
        if not any(tool.name == execution and tool.kind == "builtin" for tool in tools):
            tools += (ToolSpec(name=execution, kind="builtin"),)
        system = original.system
        if (original.metadata or {}).get(MA_METADATA_KEY_ISOLATED) != "true":
            system = apply_credential_guidance(system or "")
        spec = original.model_copy(
            update={
                "name": new_name,
                "metadata": metadata,
                "skills": tuple(kept),
                "mcp_servers": servers,
                "tools": tools,
                "system": system,
            }
        )
        created_agent = await self.publish(
            new_name, spec, expected_revision=0, key=self.operation_key + ":copy:agent"
        )
        return HostAgentCopy(HostAgentView(created_agent), tuple(dropped), tuple(copied))

    async def retire(self, catalog_id: str) -> None:
        row = await self.entry(catalog_id)
        if self.profile.support_for("agent_delete") not in SUPPORTED:
            # No fictional native deletion or local tombstone on a refused delete.
            raise UnsupportedCapability(("agent_delete",), self.backend.capabilities().profile_id)
        await self.catalog.put_agent(
            self.scope,
            catalog_id,
            row.agent.model_copy(update={"lifecycle": "deleted"}),
            expected_revision=row.local_revision,
        )
        await self.backend.agents.delete(
            self.scope, row.agent.ref, key=self.operation_key + ":retire:" + catalog_id
        )


class _NeutralSkillAddHost:
    def __init__(
        self, host: NeutralResourceHost, catalog_id: str, agent_name: str, recheck: AgentGate
    ) -> None:
        self.host = host
        self.catalog_id = catalog_id
        self.agent_name = agent_name
        self._recheck = recheck
        self.uploaded: UploadedSkill | None = None

    def attached_ids(self, agent: Agent) -> frozenset[str]:
        return frozenset(pin.id for pin in agent.spec.skills or ())

    def skill_count(self, agent: Agent) -> int:
        return len(agent.spec.skills or ())

    async def load_upload(self, name: str) -> StoredSkillUpload | None:
        title = tenant_scoped_display_title(
            tenant_id=uuid.UUID(self.host.scope.tenant_id), name=name, agent_name=self.agent_name
        )
        rows = [
            row
            for row in await self.host.catalog.list_skills(self.host.scope)
            if row.agent_name == self.agent_name and row.display_title == title
        ]
        if not rows:
            return None
        if any(row.principal_id != self.host.scope.principal_id for row in rows):
            raise ScopeViolation("skill", "upload belongs to another principal")
        ids = {row.skill_id for row in rows}
        if len(ids) != 1:
            raise CatalogConflict("ambiguous owned upload; no automatic duplicate upload")
        id_ = next(iter(ids))
        native = await self.host.backend.skills.retrieve(self.host.scope, id_)
        latest = native.latest_version
        if latest is None or latest.id != id_ or latest.version in (None, "latest"):
            raise CatalogConflict("native skill has no concrete latest version")
        current = await self.host.catalog.get_skill(self.host.scope, id_, latest.version)
        if current is None or current not in rows:
            raise CatalogConflict("native skill version was not retained; reconcile before upload")
        return StoredSkillUpload(
            id_, current.version, current.bundle.preview.content_hash, "upload", ""
        )

    async def find_own_skill(self, name: str) -> str | None:
        # load_upload already searched durable ownership. Do not perform a
        # workspace-wide native list or adopt an unretained native id.
        return None

    async def refuse_mount_clash(self, agent: Agent, name: str, own_id: str | None) -> None:
        for pin in agent.spec.skills or ():
            if pin.id == own_id:
                continue
            if pin.version is None:
                # This read consumes only a mount name. Do not demand a native
                # version when all retained versions have that same name.
                rows = [
                    row
                    for row in await self.host.catalog.list_skills(self.host.scope)
                    if row.skill_id == pin.id
                ]
                names = {row.bundle.preview.name for row in rows}
                if len(names) != 1:
                    names = {(await self.host.retained(pin)).bundle.preview.name}
            else:
                names = {(await self.host.retained(pin)).bundle.preview.name}
            if name in names:
                raise SkillIngestError(
                    f"{self.agent_name} already has a skill that loads as '{name}'. "
                    "Rename this one."
                )

    async def retrieve_agent(self) -> Agent:
        return (await self.host.read(self.catalog_id)).agent

    async def recheck(self, agent: Agent) -> None:
        await self._recheck(agent)

    async def refuse_shared(self, agent: Agent, name: str, skill_id: str) -> None:
        if any(
            other.catalog_id != self.catalog_id
            and any(pin.id == skill_id for pin in other.agent.spec.skills or ())
            for other in await self.host.agents()
        ):
            raise SkillIngestError(
                f"'{name}' on {self.agent_name} is also attached to another agent. "
                "Add this under a new name."
            )

    async def create_skill(self, bundle: SkillBundle) -> UploadedSkill:
        native = await self.host.backend.skills.create(
            self.host.scope, skill_upload(bundle), key=self.host.operation_key + ":skill:create"
        )
        latest = native.latest_version
        if latest is None or latest.id != native.id or latest.version in (None, "latest"):
            raise CatalogConflict("created skill has no concrete version")
        return UploadedSkill(native.id, latest.version)

    async def publish_skill(self, skill_id: str, bundle: SkillBundle) -> UploadedSkill:
        native = await self.host.backend.skills.publish_version(
            self.host.scope,
            skill_id,
            skill_upload(bundle),
            key=self.host.operation_key + ":skill:publish",
        )
        return UploadedSkill(native.ref.id, native.version)

    async def save_upload(self, bundle: SkillBundle, uploaded: UploadedSkill) -> None:
        if uploaded.version in (None, "latest"):
            raise CatalogConflict("cannot retain an upload without its native version")
        await self.host.catalog.put_skill(
            self.host.scope,
            skill_id=uploaded.id,
            version=uploaded.version,
            agent_name=self.agent_name,
            display_title=tenant_scoped_display_title(
                tenant_id=uuid.UUID(self.host.scope.tenant_id),
                name=bundle.preview.name,
                agent_name=self.agent_name,
            ),
            bundle=bundle,
        )
        self.uploaded = uploaded

    async def attach(self, skill_id: str) -> bool:
        uploaded = self.uploaded
        if uploaded is None or uploaded.id != skill_id or uploaded.version is None:
            raise CatalogConflict("upload must be retained before attachment")
        row = await self.host.read(self.catalog_id)
        await self.recheck(row.agent)
        pins = row.agent.spec.skills or ()
        present = skill_id in self.attached_ids(row.agent)
        replacement: dict[str, object] = {"version": uploaded.version}
        if self.host.profile.skill_digest == "version":
            replacement["digest"] = uploaded.version
        wanted = tuple(
            pin.model_copy(update=replacement) if pin.id == skill_id else pin for pin in pins
        )
        if not present:
            wanted += (
                SkillRef(
                    id=skill_id,
                    version=uploaded.version,
                    digest=uploaded.version
                    if self.host.profile.skill_digest == "version"
                    else None,
                ),
            )
        if len(wanted) > AGENT_SKILL_CAP:
            raise SkillIngestError(
                "Uploaded, but not attached: remove a skill, then add this again."
            )
        retained = await self.host.retained(SkillRef(id=skill_id, version=uploaded.version))
        await self.refuse_mount_clash(row.agent, retained.bundle.preview.name, skill_id)
        if wanted == pins:
            return False
        await self.host.publish(
            self.catalog_id,
            row.agent.spec.model_copy(update={"skills": wanted}),
            expected_revision=row.local_revision,
            key=self.host.operation_key + ":agent:attach",
        )
        return not present


WorkerField = Literal[
    "origin",
    "scope",
    "overrides",
    "spec",
    "backend",
    "tools",
    "mcp",
    "skills",
    "files",
    "sandbox",
    "effort",
    "extensions",
    "system",
    "memory",
    "budget",
    "deadline",
    "max_turns",
    "max_model_requests",
    "max_input_tokens",
    "max_output_tokens",
    "provider_limits_usd",
    "native_budget_usd",
    "declared_drops",
]
WorkerReason = DenyReason | Literal["not_subset", "unbound", "unsupported", "ambiguous", "stale"]


class WorkerSpecRefused(ValueError):
    """Closed permission refusal without spec values or credential material."""

    def __init__(self, field: WorkerField, reason: WorkerReason) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"worker {field}: {reason}")


_NativeBudgetUSD = Annotated[
    Decimal, Field(gt=0, strict=True, allow_inf_nan=False, max_digits=12, decimal_places=2)
]


class WorkerLimits(Contract):
    budget_usd: Decimal = Field(ge=0, allow_inf_nan=False, strict=True)
    deadline: datetime
    max_turns: int = Field(gt=0, strict=True)
    max_model_requests: int | None = Field(
        default=None, gt=0, strict=True, exclude_if=lambda value: value is None
    )
    max_input_tokens: int | None = Field(
        default=None, gt=0, strict=True, exclude_if=lambda value: value is None
    )
    max_output_tokens: int | None = Field(
        default=None, gt=0, strict=True, exclude_if=lambda value: value is None
    )
    native_budget_usd: _NativeBudgetUSD | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    provider_limits_usd: (
        FrozenMap[
            Provider,
            Annotated[
                Decimal,
                Field(ge=0, strict=True, allow_inf_nan=False, max_digits=12, decimal_places=6),
            ],
        ]
        | None
    ) = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def _aware_deadline(self) -> WorkerLimits:
        if self.deadline.utcoffset() is None:
            raise ValueError("worker deadline must be aware")
        return self

    def require_profile(self, profile: Profile) -> None:
        """Refuse requested ceilings before a driver can allocate native work."""
        if (
            self.native_budget_usd is not None
            and profile.support_for("session_spend_control") != "native"
        ):
            raise WorkerSpecRefused("native_budget_usd", "unsupported")
        if self.provider_limits_usd is not None and (
            self.provider_limits_usd.get(profile.provider, Decimal(0)) <= 0
        ):
            raise WorkerSpecRefused("provider_limits_usd", "not_subset")
        capabilities: tuple[tuple[int | None, Capability], ...] = (
            (self.max_model_requests, "model_request_limit"),
            (self.max_input_tokens, "input_token_limit"),
            (self.max_output_tokens, "output_token_limit"),
        )
        for value, capability in capabilities:
            allowed = ("native",) if capability == "output_token_limit" else SUPPORTED
            if value is not None and profile.support_for(capability) not in allowed:
                raise WorkerSpecRefused("budget", "unsupported")

    def require_native_dispatch(self, now: datetime) -> None:
        """Soft native budgets still require a bounded, single-input host attempt."""
        if self.native_budget_usd is None:
            return
        if self.native_budget_usd > self.budget_usd:
            raise WorkerSpecRefused("native_budget_usd", "not_subset")
        if self.max_turns != 1:
            raise WorkerSpecRefused("max_turns", "not_subset")
        if not 0 < (self.deadline - now).total_seconds() <= 120:
            raise WorkerSpecRefused("deadline", "not_subset")


class AgentFleetGrant(Contract):
    """Finite admin ceilings; an owner assigns the persisted setting epoch."""

    epoch: int = Field(default=0, ge=0, strict=True)
    max_workers: int = Field(gt=0, strict=True)
    choices: tuple[tuple[ResolvedBackend, ModelRef], ...] = Field(min_length=1)
    limits: WorkerLimits
    max_run_seconds: int = Field(gt=0, strict=True)
    max_worker_seconds: int = Field(gt=0, strict=True)
    max_root_turns: int = Field(gt=0, strict=True)
    native_root_budget_usd: _NativeBudgetUSD | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    root_cash_budget_usd: Decimal | None = Field(
        default=None, gt=0, strict=True, allow_inf_nan=False, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _root_budgets(self) -> AgentFleetGrant:
        if (self.native_root_budget_usd is None) != (self.root_cash_budget_usd is None):
            raise ValueError("root native and cash budgets must be supplied together")
        return self

    @property
    def expires_at(self) -> datetime:
        return self.limits.deadline

    @property
    def backends(self) -> tuple[ResolvedBackend, ...]:
        retained: list[ResolvedBackend] = []
        for backend, _model in self.choices:
            if backend not in retained:
                retained.append(backend)
        return tuple(retained)

    @property
    def models(self) -> tuple[ModelRef, ...]:
        return tuple(model for _backend, model in self.choices)

    @model_validator(mode="after")
    def _finite_choices(self) -> AgentFleetGrant:
        models = tuple((model.provider, model.id) for _backend, model in self.choices)
        if (
            self.limits.budget_usd <= 0
            or len(set(models)) != len(models)
            or self.max_worker_seconds > self.max_run_seconds
            or any(
                backend.backend != model.provider
                or backend.thread_mode != "per_caller"
                or (backend.model is not None and backend.model != model.id)
                or bool(model.options)
                or "*" in model.id
                for backend, model in self.choices
            )
        ):
            raise ValueError("fleet ceilings require finite unique profile/model choices")
        return self


@dataclass(frozen=True)
class WorkerPermissions:
    """Actual root facts supplied by the host; never parsed from worker JSON."""

    fleet: FleetFacts
    subject: Subject
    agent: AgentRef
    spec: AgentSpec = dataclass_field(repr=False)
    base_system: str = dataclass_field(repr=False)
    backend_grants: tuple[ResolvedBackend, ...]
    models: tuple[ModelRef, ...] = dataclass_field(repr=False)
    limits: WorkerLimits
    resources: tuple[ResourceBinding, ...] = dataclass_field(default=(), repr=False)
    environment: EnvironmentSpec | None = dataclass_field(default=None, repr=False)
    source_agents: tuple[ResourceRef, ...] = ()
    source_revisions: tuple[tuple[ResourceRef, Revision], ...] = ()
    seal_ids: frozenset[str] = frozenset()
    memory_read_only: bool = True
    agent_id: uuid.UUID | None = None


@dataclass(frozen=True)
class ResolvedWorkerSpec:
    spec: AgentSpec = dataclass_field(repr=False)
    backend: ResolvedBackend
    resources: tuple[ResourceBinding, ...] = dataclass_field(repr=False)
    environment: EnvironmentSpec | None = dataclass_field(repr=False)
    limits: WorkerLimits
    permissions: WorkerPermissions = dataclass_field(repr=False)
    digest: str
    source_agent: tuple[ResourceRef, Revision] | None = None

    @property
    def declared_drops(
        self,
    ) -> tuple[Literal["memory", "sandbox", "skills", "mcp", "tools", "files", "repository"], ...]:
        return self.spec.declared_drops


def _worker_subset(
    field: WorkerField, requested: tuple[Contract, ...], granted: tuple[Contract, ...]
) -> None:
    if any(item not in granted for item in requested):
        raise WorkerSpecRefused(field, "not_subset")


def _worker_tools(requested: AgentSpec, granted: AgentSpec) -> None:
    available = {(tool.kind, tool.name): tool for tool in granted.tools or ()}
    if len(available) != len(granted.tools or ()):
        raise WorkerSpecRefused("tools", "ambiguous")
    seen: set[tuple[str, str]] = set()
    for tool in requested.tools or ():
        key = tool.kind, tool.name
        old = available.get(key)
        if key in seen or old is None:
            raise WorkerSpecRefused("tools", "not_subset")
        seen.add(key)
        if (
            tool.input_schema != old.input_schema
            or tool.description != old.description
            or (old.permission == "ask" and tool.permission != "ask")
        ):
            raise WorkerSpecRefused("tools", "not_subset")


def _worker_mcp(requested: AgentSpec, granted: AgentSpec, profile: Profile) -> None:
    if requested.mcp_servers and not profile.mcp_policy_keys:
        raise WorkerSpecRefused("mcp", "unsupported")
    if requested.mcp_servers and (
        len(requested.mcp_servers) != 1 or requested.mcp_servers[0].credential_ref is None
    ):
        raise WorkerSpecRefused("mcp", "unsupported")
    available = {connection.name: connection for connection in granted.mcp_servers or ()}
    if len(available) != len(granted.mcp_servers or ()):
        raise WorkerSpecRefused("mcp", "ambiguous")
    seen: set[str] = set()
    for connection in requested.mcp_servers or ():
        old = available.get(connection.name)
        try:
            url = urlsplit(connection.url)
        except ValueError:
            raise WorkerSpecRefused("mcp", "unsupported") from None
        if (
            old is None
            or connection.name in seen
            or url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or connection.transport != "streamable_http"
            or any(char.isspace() for char in connection.url)
            or "\\" in connection.url
            or "%" in url.netloc
            or "?" in connection.url
            or "#" in connection.url
            or (connection.url, connection.transport, connection.credential_ref)
            != (old.url, old.transport, old.credential_ref)
        ):
            raise WorkerSpecRefused("mcp", "unbound")
        seen.add(connection.name)
        # Policy semantics must be declared, not arbitrary SDK pass-through.
        policy, allowed = dict(connection.tool_policy), dict(old.tool_policy)
        if set(policy) - set(profile.mcp_policy_keys):
            raise WorkerSpecRefused("mcp", "unsupported")
        tools, ceiling = policy.get("allowed_tools"), allowed.get("allowed_tools")
        if (
            not isinstance(tools, list)
            or not isinstance(ceiling, list)
            or not all(isinstance(name, str) and name for name in tools + ceiling)
            or not tools
            or len(set(tools)) != len(tools)
            or not set(tools) <= set(ceiling)
            or any(
                policy.get(key) != allowed.get(key)
                for key in set(policy) | set(allowed)
                if key != "allowed_tools"
            )
            or ("required" in policy and type(policy["required"]) is not bool)
        ):
            raise WorkerSpecRefused("mcp", "not_subset")
    servers = set(available)
    for tool in requested.tools or ():
        if tool.kind == "mcp_toolset" and (tool.name not in seen or tool.name not in servers):
            raise WorkerSpecRefused("mcp", "unbound")


def _worker_environment(requested: EnvironmentSpec | None, granted: EnvironmentSpec | None) -> None:
    if requested is None:
        return
    if granted is None:
        raise WorkerSpecRefused("sandbox", "not_subset")
    if requested.native_config is not None or granted.native_config is not None:
        raise WorkerSpecRefused("sandbox", "unsupported")
    if requested.execution != granted.execution:
        raise WorkerSpecRefused("sandbox", "unsupported")
    if (
        requested.scope != granted.scope
        or requested.packages != granted.packages
        or requested.metadata != granted.metadata
    ):
        raise WorkerSpecRefused("sandbox", "not_subset")
    _worker_subset("files", tuple(requested.sources or ()), tuple(granted.sources or ()))
    for source in requested.sources or ():
        if source.repository_url is not None:
            try:
                url = urlsplit(source.repository_url)
            except ValueError:
                raise WorkerSpecRefused("files", "unsupported") from None
            if url.username or url.password or url.query or url.fragment:
                raise WorkerSpecRefused("files", "unbound")
    network, ceiling = requested.network, granted.network
    if requested.execution == "hosted" and (network is None or ceiling is None):
        raise WorkerSpecRefused("sandbox", "unsupported")
    if network is None or ceiling is None:
        if network != ceiling:
            raise WorkerSpecRefused("sandbox", "unsupported")
    elif (
        ceiling.mode == "none"
        and network.mode != "none"
        or ceiling.mode == "limited"
        and (
            network.mode == "unrestricted"
            or (
                network.mode == "limited"
                and not set(network.allowed_hosts) <= set(ceiling.allowed_hosts)
            )
        )
    ):
        raise WorkerSpecRefused("sandbox", "not_subset")


def fleet_root_permissions(
    source: BackendAgent,
    admission: AdmissionGrant,
    grant: AgentFleetGrant,
    *,
    fleet: FleetFacts,
    policy: TenantAccessPolicy,
    now: datetime,
    seal_ids: frozenset[str] = frozenset(),
) -> WorkerPermissions:
    """Compose captured source; caller joins origin/binding and rechecks grant epoch."""
    from daimon.core.memory_resource import memory_mount
    from daimon.core.turn.ceiling import TURN_CEILING_S
    from mux.drivers.anthropic.schemas import agent_tools

    if now.utcoffset() is None:
        raise WorkerSpecRefused("deadline", "unsupported")
    try:
        grant = AgentFleetGrant.model_validate(grant.model_dump())
    except ValueError:
        raise WorkerSpecRefused("budget", "unsupported") from None
    if grant.epoch <= 0 or grant.expires_at <= now:
        raise WorkerSpecRefused("origin", "stale")
    actual, effective, environment = source.source, source.effective_spec, source.environment
    if (
        actual is None
        or effective is None
        or environment is None
        or environment.native is None
        or environment.lifecycle != "active"
        or environment.archived_at is not None
        or environment.ref.kind != "environment"
        or environment.ref.provider != actual.ref.provider
        or environment.ref.account_scope_id != actual.ref.account_scope_id
        or (environment.ref.tenant_id, environment.ref.account_id)
        != (fleet.scope.tenant_id, fleet.scope.account_id)
    ):
        raise WorkerSpecRefused("origin", "unbound")
    tools = effective.tools
    try:
        for extension in effective.extensions or ():
            if extension.namespace == f"{MANAGED_AGENTS.provider}.agent_tools":
                tools = agent_tools(extension).intent()
    except ValueError:
        raise WorkerSpecRefused("tools", "unsupported") from None
    # The complete capture remains the input to the existing obligation classifier.
    spec = effective.model_copy(update={"extensions": None, "tools": tools})
    resources = source.resource_requirements
    read_only = source.memory_read_only is not False
    if source.memory_store_id is not None:
        mount = cast(JsonValue, memory_mount(source.memory_store_id, read_only=read_only))
        resources += (
            ResourceBinding(
                id=source.memory_store_id,
                kind="native",
                resource=actual.ref.model_copy(
                    update={
                        "kind": "memory_store",
                        "id": source.memory_store_id,
                        "account_id": fleet.scope.account_id,
                    }
                ),
                native=ExtensionConfig(
                    namespace=f"{MANAGED_AGENTS.provider}.session_resource_create",
                    version=1,
                    value={"resource": mount},
                ),
            ),
        )
    duration = min(grant.max_run_seconds, grant.max_worker_seconds, TURN_CEILING_S)
    deadline = min(grant.expires_at, now + timedelta(seconds=duration))
    limits = grant.limits.model_copy(update={"deadline": deadline})
    return freeze_root_permissions(
        WorkerPermissions(
            fleet=fleet,
            subject=admission.subject,
            agent=admission.agent,
            spec=spec,
            base_system=spec.system or "",
            backend_grants=grant.backends,
            models=grant.models,
            limits=limits,
            resources=resources,
            environment=environment.spec,
            source_agents=(actual.ref,),
            source_revisions=((actual.ref, actual.revision),),
            seal_ids=seal_ids,
            memory_read_only=read_only,
            agent_id=source.agent_id,
        ),
        source,
        admission=admission,
        policy=policy,
        now=now,
    )


def freeze_root_permissions(
    grants: WorkerPermissions,
    source: BackendAgent,
    *,
    admission: AdmissionGrant,
    policy: TenantAccessPolicy,
    now: datetime,
) -> WorkerPermissions:
    """Pin host-owned finite grants to an admitted, captured root execution.

    The caller joins the verified origin, binding and intent before this call.
    Neither grants nor source may be built from a worker/tool/card request.
    No latest-source read, allocation, credential lookup or financial hold occurs.
    """
    from daimon.core.channel_backend import check_backend

    fleet, actual, effective = grants.fleet, source.source, source.effective_spec
    if (
        admission.tenant_id != source.tenant_id
        or str(admission.tenant_id) != fleet.scope.tenant_id
        or grants.subject != admission.subject
        or grants.agent != admission.agent
        or fleet.place != admission.run_place
        or fleet.root.thread_id != admission.thread_id
        or fleet.root.channel.channel_id != admission.channel_id
        or source.resource_scope != fleet.scope
        or grants.agent_id != source.agent_id
        or actual is None
        or effective is None
        or actual.native is None
        or actual.lifecycle != "active"
        or actual.archived_at is not None
        or actual.ref.kind != "agent"
        or actual.ref.tenant_id != fleet.scope.tenant_id
        or actual.ref.account_id
        not in (fleet.scope.account_id, resource_scope(tenant_id=fleet.scope.tenant_id).account_id)
        or actual.ref.provider != actual.spec.model.provider
        or grants.source_agents != (actual.ref,)
        or grants.source_revisions != ((actual.ref, actual.revision),)
        or source.memory_read_only is None
        or source.memory_required is None
        or source.environment is None
        or dict(grants.spec.metadata or {}) != dict(effective.metadata or {})
    ):
        raise WorkerSpecRefused("origin", "unbound")
    if not grants.backend_grants or not grants.models:
        raise WorkerSpecRefused("backend", "unbound")
    if any(model in grants.models[:index] for index, model in enumerate(grants.models)):
        raise WorkerSpecRefused("backend", "ambiguous")
    try:
        for backend in grants.backend_grants:
            check_backend(
                ConfigRevision.create(fleet.root.channel, 1, backend),
                enabled_backends=policy.enabled_backends,
            )
    except (DaimonError, InvalidConfig):
        raise WorkerSpecRefused("backend", "unsupported") from None
    try:
        caps = WorkerLimits.model_validate(grants.limits.model_dump())
    except ValueError:
        raise WorkerSpecRefused("budget", "unsupported") from None
    if grants.base_system != grants.spec.system:
        raise WorkerSpecRefused("system", "unbound")
    narrowed = resolve_worker_spec(
        grants,
        grants.spec,
        policy=policy,
        now=now,
        resources=grants.resources,
        environment=grants.environment,
        limits=caps,
        captured_source=source,
    ).permissions
    return replace(
        narrowed,
        fleet=fleet,
        subject=admission.subject,
        agent=admission.agent,
        backend_grants=grants.backend_grants,
        models=grants.models,
        source_agents=grants.source_agents,
        source_revisions=grants.source_revisions,
        agent_id=source.agent_id,
    )


def resolve_worker_spec(
    parent: WorkerPermissions,
    spec: AgentSpec | Agent,
    *,
    policy: TenantAccessPolicy,
    now: datetime,
    backend: BackendConfig | None = None,
    overrides: AgentPatch | None = None,
    resources: tuple[ResourceBinding, ...] = (),
    environment: EnvironmentSpec | None = None,
    limits: WorkerLimits | None = None,
    captured_source: BackendAgent | None = None,
) -> ResolvedWorkerSpec:
    """Resolve existing spec shapes against authentic, finite parent authority.

    This does not reserve funds, create resources or authorize native execution.
    captured_source comes only from the scoped loader/resource projector.
    Preparation must repeat against fresh host facts and apply the derived port
    and tool grants; a persisted digest is an idempotency key, never authority.
    """
    from daimon.core.channel_backend import (
        check_backend,
        compatibility_missing,
        spec_compatibility_items,
    )
    from daimon.core.turn.ceiling import TURN_CEILING_S
    from daimon.core.turn.runtime_manifest import (
        classify_source_obligations,
        compose_source_instructions,
    )

    fleet = parent.fleet
    place = fleet.place
    decision = authorize(
        policy,
        subject=parent.subject,
        action=Action.SPAWN_WORKER,
        surface=Surface.FLEET,
        agent=parent.agent,
        place=place,
        fleet=fleet,
    )
    if not decision:
        raise WorkerSpecRefused("origin", decision.reason or "unbound")
    if not parent.base_system.strip():
        raise WorkerSpecRefused("system", "unbound")
    if now.utcoffset() is None:
        raise WorkerSpecRefused("deadline", "unsupported")
    if isinstance(spec, Agent):
        if (
            spec.ref not in parent.source_agents
            or spec.ref.kind != "agent"
            or spec.ref.provider != spec.spec.model.provider
            or spec.lifecycle != "active"
            or spec.archived_at is not None
        ):
            raise WorkerSpecRefused("scope", "unbound")
        if spec.ref.tenant_id != fleet.scope.tenant_id or (
            spec.ref.account_id != fleet.scope.account_id
            and not (
                spec.ref.account_id == resource_scope(tenant_id=fleet.scope.tenant_id).account_id
                and captured_source is not None
                and captured_source.resource_scope == fleet.scope
                and captured_source.source == spec
            )
        ):
            raise WorkerSpecRefused("scope", "not_subset")
        if (spec.ref, spec.revision) not in parent.source_revisions:
            raise WorkerSpecRefused("scope", "stale")
        clone = authorize(
            policy,
            subject=parent.subject,
            action=Action.RUN_AGENT,
            surface=Surface.FLEET,
            agent=build_agent_ref(spec.spec.name, spec.spec.metadata or {}),
            place=place,
        )
        if not clone:
            raise WorkerSpecRefused("scope", clone.reason or "unbound")
        source = spec.spec
    else:
        if overrides is not None:
            raise WorkerSpecRefused("overrides", "unsupported")
        source = spec
    data = source.model_dump(mode="json")
    if overrides is not None:
        if overrides.extensions:
            raise WorkerSpecRefused("extensions", "unsupported")
        data.update(overrides.model_dump(mode="json", exclude_unset=True))
        if "metadata" in overrides.model_fields_set and overrides.metadata is not None:
            metadata = dict(source.metadata or {})
            for key, value in overrides.metadata.items():
                if value is None:
                    metadata.pop(key, None)
                else:
                    metadata[key] = value
            data["metadata"] = metadata
    try:
        resolved = AgentSpec.model_validate(data)
    except ValueError:
        raise WorkerSpecRefused("spec", "unsupported") from None
    if captured_source is not None:
        actual, effective = captured_source.source, captured_source.effective_spec
        if (
            actual is None
            or effective is None
            or actual.native is None
            or actual.lifecycle != "active"
            or actual.archived_at is not None
            or captured_source.resource_scope != fleet.scope
            or str(captured_source.tenant_id) != fleet.scope.tenant_id
            or actual.ref.tenant_id != fleet.scope.tenant_id
            or (
                actual != spec
                if isinstance(spec, Agent)
                else captured_source.agent_id != parent.agent_id
            )
            or captured_source.memory_read_only is None
            or captured_source.memory_required is None
            or captured_source.environment is None
        ):
            raise WorkerSpecRefused("scope", "stale")
        parent = replace(
            parent, memory_read_only=parent.memory_read_only or captured_source.memory_read_only
        )
    drops = resolved.declared_drops
    inherited = (parent.spec, source, captured_source.effective_spec if captured_source else None)
    if any(item is not None and not set(item.declared_drops) <= set(drops) for item in inherited):
        raise WorkerSpecRefused("declared_drops", "not_subset")
    # All root stamps remain immutable; omitted clone keys preserve, null
    # values delete source-only keys, and an explicit map clear cannot erase
    # the root's ownership or guidance metadata.
    if dict(resolved.metadata or {}) != dict(parent.spec.metadata or {}):
        raise WorkerSpecRefused("scope", "not_subset")
    worker_run = authorize(
        policy,
        subject=parent.subject,
        action=Action.RUN_AGENT,
        surface=Surface.FLEET,
        agent=build_agent_ref(resolved.name, resolved.metadata or {}),
        place=place,
    )
    if not worker_run:
        raise WorkerSpecRefused("scope", worker_run.reason or "unbound")
    if resolved.model.options:
        raise WorkerSpecRefused("effort", "unsupported")
    if resolved.extensions:
        raise WorkerSpecRefused("extensions", "unsupported")
    choices = tuple(
        grant
        for grant in parent.backend_grants
        if grant.backend == resolved.model.provider
        and (grant.model is None or grant.model == resolved.model.id)
    )
    if backend is None and len(choices) != 1:
        raise WorkerSpecRefused("backend", "ambiguous")
    try:
        selected = resolve_default(backend) if backend is not None else choices[0]
    except (ValueError, InvalidConfig):
        raise WorkerSpecRefused("backend", "unsupported") from None
    if selected not in parent.backend_grants:
        raise WorkerSpecRefused("backend", "not_subset")
    if (
        selected.thread_mode != "per_caller"
        or resolved.model.provider != selected.backend
        or (selected.model is not None and selected.model != resolved.model.id)
        or resolved.model not in parent.models
    ):
        raise WorkerSpecRefused("backend", "not_subset")
    try:
        check_backend(
            ConfigRevision.create(fleet.root.channel, 1, selected),
            enabled_backends=policy.enabled_backends,
        )
    except DaimonError:
        raise WorkerSpecRefused("backend", "unsupported") from None
    profile = get_profile(selected.profile)
    resolved = resolved.model_copy(
        update={
            "skills": () if "skills" in drops else resolved.skills,
            "mcp_servers": () if "mcp" in drops or "tools" in drops else resolved.mcp_servers,
            "tools": tuple(
                tool
                for tool in resolved.tools or ()
                if "tools" not in drops
                and not ("mcp" in drops and tool.kind == "mcp_toolset")
                and not (
                    "sandbox" in drops
                    and tool.kind == "builtin"
                    and tool.name in profile.execution_tools
                )
            )
            if drops
            else resolved.tools,
        }
    )
    skills, mcps = spec_compatibility_items(resolved, skill_providers=(parent.spec.model.provider,))
    for item in skills:
        if compatibility_missing(item, selected.backend, profile):
            raise WorkerSpecRefused("skills", "unsupported")
    for item in mcps:
        if compatibility_missing(item, selected.backend, profile):
            raise WorkerSpecRefused("mcp", "unsupported")
    if (
        profile.implicit_execution
        and "sandbox" not in drops
        and "tools" not in drops
        and not any(
            tool.kind == "builtin" and tool.name == profile.execution_tool
            for tool in resolved.tools or ()
        )
    ):
        resolved = resolved.model_copy(
            update={
                "tools": tuple(resolved.tools or ())
                + (ToolSpec(name=profile.execution_tool, kind="builtin"),)
            }
        )
    # Native confirmation capability does not imply that the neutral tool
    # compiler maps this per-tool field. Unproven ask controls refuse.
    if any(tool.permission != "auto" for tool in resolved.tools or ()):
        raise WorkerSpecRefused("tools", "unsupported")
    _worker_tools(resolved, parent.spec)
    _worker_mcp(resolved, parent.spec, profile)
    for pin in resolved.skills or ():
        if pin.version is None:
            raise WorkerSpecRefused("skills", "unbound")
    _worker_subset("skills", tuple(resolved.skills or ()), tuple(parent.spec.skills or ()))
    _worker_subset("files", resources, parent.resources)
    for resource in resources:
        ref = resource.resource
        if ref is not None and (ref.tenant_id, ref.account_id) != (
            fleet.scope.tenant_id,
            fleet.scope.account_id,
        ):
            raise WorkerSpecRefused("scope", "not_subset")
        if resource.target_path is not None and any(
            part in ("..", "") for part in PurePosixPath(resource.target_path).parts
        ):
            raise WorkerSpecRefused("files", "unsupported")
    resources = tuple(
        resource
        for resource in resources
        if not ("files" in drops and resource.kind == "artifact")
        and not ("repository" in drops and resource.kind == "repository")
        and not (
            "memory" in drops
            and resource.resource is not None
            and resource.resource.kind == "memory_store"
        )
    )
    from mux.drivers.anthropic.sessions_lifecycle import MemoryMount, SessionResourceConfig

    for resource in resources:
        if resource.native is None:
            continue
        native, ref = resource.native, resource.resource
        try:
            mount = SessionResourceConfig.model_validate(dict(native.value)).resource
        except ValueError:
            raise WorkerSpecRefused("files", "unsupported") from None
        if (
            native.namespace != "anthropic.session_resource_create"
            or native.version != 1
            or resource.kind != "native"
            or resource.target_path is not None
            or resource.credential_ref is not None
            or not isinstance(mount, MemoryMount)
            or ref is None
            or ref.kind != "memory_store"
            or ref.id != mount.memory_store_id
            or ref.provider != selected.backend
            or profile.support.get("memory_stores") != "native"
        ):
            raise WorkerSpecRefused("files", "unsupported")
    environment = environment if environment is not None else parent.environment
    if "sandbox" in drops:
        environment = None
    elif environment is not None and ("files" in drops or "repository" in drops):
        environment = environment.model_copy(
            update={
                "sources": tuple(
                    item
                    for item in environment.sources or ()
                    if not ("files" in drops and item.kind == "file")
                    and not ("repository" in drops and item.kind == "repository")
                ),
            }
        )
    _worker_environment(environment, parent.environment)
    if (environment is None or environment.execution == "none") and any(
        tool.kind == "builtin" and tool.name == profile.execution_tool
        for tool in resolved.tools or ()
    ):
        raise WorkerSpecRefused("sandbox", "unbound")
    cap = limits or parent.limits
    cap.require_native_dispatch(now)
    if cap.budget_usd <= 0 or cap.budget_usd > parent.limits.budget_usd:
        raise WorkerSpecRefused("budget", "not_subset")
    if (
        cap.deadline <= now
        or cap.deadline > parent.limits.deadline
        or (cap.deadline - now).total_seconds() > TURN_CEILING_S
    ):
        raise WorkerSpecRefused("deadline", "not_subset")
    if cap.max_turns > parent.limits.max_turns:
        raise WorkerSpecRefused("max_turns", "not_subset")
    if parent.limits.native_budget_usd is not None and (
        cap.native_budget_usd is None or cap.native_budget_usd > parent.limits.native_budget_usd
    ):
        raise WorkerSpecRefused("native_budget_usd", "not_subset")
    request_fields: tuple[WorkerField, ...] = (
        "max_model_requests",
        "max_input_tokens",
        "max_output_tokens",
    )
    for field in request_fields:
        retained, narrowed = getattr(parent.limits, field), getattr(cap, field)
        if retained is not None and (narrowed is None or narrowed > retained):
            raise WorkerSpecRefused(field, "not_subset")
    if parent.limits.provider_limits_usd is not None:
        lines = cap.provider_limits_usd
        if lines is None or any(
            provider not in parent.limits.provider_limits_usd
            or amount > parent.limits.provider_limits_usd[provider]
            for provider, amount in lines.items()
        ):
            raise WorkerSpecRefused("provider_limits_usd", "not_subset")
    if cap.provider_limits_usd is not None and (
        cap.budget_usd > cap.provider_limits_usd.get(selected.backend, Decimal(0))
    ):
        raise WorkerSpecRefused("provider_limits_usd", "not_subset")
    cap.require_profile(profile)
    system = compose_source_instructions(parent.base_system, resolved.system)
    if captured_source is not None:
        system = compose_source_instructions(
            cast(AgentSpec, captured_source.effective_spec).system, system, deduplicate_prefix=True
        )
    resolved = resolved.model_copy(update={"system": system})
    if drops:
        guidance = "Worker restrictions: " + "; ".join(
            "no persistent memory"
            if item == "memory"
            else "no sandbox execution"
            if item == "sandbox"
            else f"no {item}"
            for item in drops
        )
        if guidance not in system.split("\n\n"):
            resolved = resolved.model_copy(update={"system": system + "\n\n" + guidance})
    if captured_source is not None:
        missing = classify_source_obligations(
            captured_source, resolved, environment=environment, resources=resources
        )
        if missing:
            field = {
                "instructions": "system",
                "environment": "sandbox",
                "repository": "files",
                "credentials": "files",
                "source_definition": "extensions",
            }.get(sorted(missing)[0], sorted(missing)[0])
            raise WorkerSpecRefused(cast(WorkerField, field), "unsupported")
    body = {
        "spec": resolved.model_dump(mode="json"),
        "backend": selected.model_dump(mode="json"),
        "resources": [resource.model_dump(mode="json") for resource in resources],
        "environment": environment.model_dump(mode="json") if environment is not None else None,
        "limits": cap.model_dump(mode="json"),
        "origin": str(fleet.origin_id),
        "run": str(fleet.run_id),
        "place": asdict(fleet.place),
        "source": (
            [spec.ref.model_dump(mode="json"), spec.revision.model_dump(mode="json")]
            if isinstance(spec, Agent)
            else None
        ),
        "scope": fleet.scope.model_dump(mode="json"),
        "seals": sorted(parent.seal_ids),
        "memory_read_only": parent.memory_read_only,
    }
    if captured_source is not None:
        body["capture"] = TypeAdapter(object).dump_python(dict(captured_source), mode="json")
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    child_scope = fleet.scope.model_copy(
        update={"authorization_id": f"worker:{fleet.origin_id}:{digest}"}
    )
    child = replace(
        parent,
        fleet=replace(fleet, scope=child_scope, depth=1),
        subject=replace(
            parent.subject, is_admin=False, via_agent_key=True, administered_channel_ids=frozenset()
        ),
        spec=resolved,
        backend_grants=(selected,),
        models=(resolved.model,),
        resources=resources,
        environment=environment,
        limits=cap,
        source_agents=(),
        source_revisions=(),
    )
    source_agent = (spec.ref, spec.revision) if isinstance(spec, Agent) else None
    return ResolvedWorkerSpec(
        resolved, selected, resources, environment, cap, child, digest, source_agent
    )


async def audit_worker_action(
    session: AsyncSession,
    parent: WorkerPermissions,
    *,
    worker_id: uuid.UUID,
    operation: Literal["spawn", "kill", "resume", "review"],
    outcome: Literal["allowed", "denied", "error"],
    reason: WorkerReason | Literal["authorized", "cancel_requested"],
    worker: ResolvedWorkerSpec | None = None,
) -> security_audit.SecurityAuditRow | None:
    """Append in the caller's fleet transition transaction, with no input data.

    The caller verifies worker membership under its existing run/operation CAS.
    Allowed means the transaction authorized a transition, not native completion.
    """
    fleet = parent.fleet
    if worker is not None and (
        worker.permissions.fleet.root != fleet.root
        or worker.permissions.fleet.origin_id != fleet.origin_id
        or worker.permissions.fleet.run_id != fleet.run_id
        or worker.permissions.fleet.account_id != fleet.account_id
        or worker.permissions.fleet.scope.tenant_id != fleet.scope.tenant_id
        or worker.permissions.fleet.scope.account_id != fleet.scope.account_id
        or worker.permissions.fleet.place != fleet.place
        or worker.permissions.agent_id != parent.agent_id
    ):
        raise WorkerSpecRefused("scope", "not_subset")
    try:
        drops = AgentSpec.model_validate(
            (worker.spec if worker is not None else parent.spec).model_dump(mode="json")
        ).declared_drops
    except ValueError:
        raise WorkerSpecRefused("declared_drops", "unsupported") from None
    reason_text = reason + ("; declared_drops=" + ",".join(drops) if drops else "")
    if (
        fleet.scope.is_platform
        or fleet.scope.is_legacy_host_authorized
        or parent.agent_id is None
        or fleet.scope.account_id != str(fleet.account_id)
        or fleet.scope.tenant_id != fleet.root.channel.tenant_id
    ):
        raise WorkerSpecRefused("origin", "unbound")
    row = await security_audit.append_event(
        session,
        tenant_id=uuid.UUID(fleet.scope.tenant_id),
        account_id=fleet.account_id,
        agent_id=parent.agent_id,
        platform=fleet.root.channel.platform,
        platform_user_id=parent.subject.platform_user_id,
        tool_name="fleet",
        operation=operation,
        outcome=outcome,
        reason=reason_text,
        scope=f"fleet:{fleet.run_id}:worker:{worker_id}",
        turn_ref=f"origin:{fleet.origin_id}",
    )
    # Account erasure redacts the existing audit row; it cannot authorize a new
    # worker transition after its actor has disappeared.
    return row if row is not None and row.account_id == fleet.account_id else None
