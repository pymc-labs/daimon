"""Authorized host catalog edits over immutable native resource ports.

Local catalog revisions never become provider revision pins. Uploaded bundles
are retained before attachment; a new native agent is published with catalog
CAS. Existing sessions keep their old native agent and workspace binding.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import structlog
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
from daimon.core.skills.ingest import SkillBundle, SkillIngestError, bundle_from_upload
from daimon.core.skills.service import AddedSkill, StoredSkillUpload, UploadedSkill, add_skill
from daimon.core.stores.neutral_catalog import (
    AgentRevision,
    CatalogConflict,
    PostgresNeutralCatalog,
    SkillVersion,
)
from mux.contracts.ids import Scope, SkillRef
from mux.contracts.ports import Agents, ManagedAgents
from mux.contracts.profile import SUPPORTED
from mux.contracts.resources import (
    Agent,
    AgentSpec,
    MCPConnection,
    SkillUpload,
    SkillUploadFile,
    ToolSpec,
)
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.profiles import native_agent_tools, neutral_catalog_profile_for
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:
    pass

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
        profile = neutral_catalog_profile_for(
            capabilities.provider, profile_id=capabilities.profile_id
        )
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
    ) -> AgentRevision:
        row = await self.entry(catalog_id)
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
    ) -> AgentRevision:
        """Publish authored intent with native creation and a catalog revision check."""
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
