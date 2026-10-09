"""Session lifecycle; mount administration remains in the resource driver."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from typing import Annotated, Literal, Protocol, cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.session_create_params import SessionCreateParams
from anthropic.types.beta.session_list_params import SessionListParams
from pydantic import BaseModel, Field, JsonValue

from mux.contracts.actions import UserMessage
from mux.contracts.config import ConfigRevision
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, Page, PageRequest, ResourceRef, Revision, Scope, ThreadRef
from mux.contracts.receipts import DeletionReceipt, Operation, RestoreReceipt, UpdateReceipt
from mux.contracts.resources import (
    Continuity,
    ExportRequirements,
    ProviderBinding,
    Session,
    SessionExport,
    SessionFilter,
    SessionSpec,
    UpdatePlan,
)
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_record,
    check_ref,
    visible,
    visible_grant,
)
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.drivers.anthropic.schemas import AgentTool, NativeConfig
from mux.errors import MigrationUnsupported, ScopeViolation, UnsupportedCapability


def _native_json(value: object) -> JsonValue:
    """Keep response field presence, including partial SDK-compatible replies."""
    if isinstance(value, BaseModel):
        return cast(JsonValue, value.model_dump(mode="json", exclude_unset=True))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _native_json(item)
            for key, item in cast(Mapping[object, object], value).items()
        }
    if isinstance(value, (list, tuple)):
        return [_native_json(item) for item in cast(list[object] | tuple[object, ...], value)]
    return _native_json(vars(value))


class MCPServer(NativeConfig):
    name: str
    type: Literal["url"]
    url: str


class CustomSkill(NativeConfig):
    type: Literal["custom"]
    skill_id: str
    version: str | None = None


class CatalogSkill(NativeConfig):
    type: Literal["anthropic"]
    skill_id: str
    version: str | None = None


class AgentOverrides(NativeConfig):
    type: Literal["agent_with_overrides"]
    id: str
    mcp_servers: list[MCPServer] | None = None
    tools: list[AgentTool] | None = None
    skills: list[Annotated[CustomSkill | CatalogSkill, Field(discriminator="type")]] | None = None
    version: int | None = None


class AgentVersion(NativeConfig):
    type: Literal["agent"]
    id: str
    version: int


class SessionCreateConfig(NativeConfig):
    """anthropic.session_create@1: only the native creation differences."""

    agent: Annotated[AgentOverrides | AgentVersion, Field(discriminator="type")] | None = None
    vault_ids: list[str] | None = None


class FileMount(NativeConfig):
    type: Literal["file"]
    file_id: str
    mount_path: str | None = None


class BranchCheckout(NativeConfig):
    type: Literal["branch"]
    name: str


class CommitCheckout(NativeConfig):
    type: Literal["commit"]
    sha: str


class RepositoryMount(NativeConfig):
    type: Literal["github_repository"]
    url: str
    authorization_token_ref: str
    checkout: Annotated[BranchCheckout | CommitCheckout, Field(discriminator="type")] | None = None
    mount_path: str | None = None


class MemoryMount(NativeConfig):
    type: Literal["memory_store"]
    memory_store_id: str
    access: Literal["read_write", "read_only"] | None = None
    instructions: str | None = None


type NativeMount = Annotated[FileMount | RepositoryMount | MemoryMount, Field(discriminator="type")]


class SessionResourceConfig(NativeConfig):
    """anthropic.session_resource_create@1; repo credentials are references."""

    resource: NativeMount


class SessionCreateRequest(NativeConfig):
    """Internal wire envelope; native subobjects are closed-schema checked first.

    Keep their original key order so SDK request bytes stay identical.
    """

    agent: str | dict[str, JsonValue]
    environment_id: str
    metadata: dict[str, str] | None = None
    vault_ids: list[str] | None = None
    resources: list[dict[str, JsonValue]] | None = None


class SessionArchive(Protocol):
    async def archive(self, scope: Scope, session: ResourceRef, *, key: str) -> Operation: ...


class SessionWalk(Protocol):
    def walk(self, scope: Scope) -> AsyncIterator[Session]: ...


def _config[T: NativeConfig](extension: ExtensionConfig, namespace: str, model: type[T]) -> T:
    if extension.namespace != namespace or extension.version != 1:
        raise ValueError(f"expected {namespace}@1")
    return model.model_validate(dict(extension.value))


class AnthropicSessions:
    """No cache, journal, initial send or extra provider lookup on creation."""

    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None = None,
        *,
        archive: SessionArchive | None = None,
        secrets: Callable[[Scope, str], str] | None = None,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id
        self._authorization = authorization
        self._archive = archive
        self._secrets = secrets

    def _ref(self, scope: Scope, kind: str, native_id: str) -> ResourceRef:
        return ResourceRef(
            id=native_id,
            kind=kind,
            provider="anthropic",
            account_scope_id=self._account_scope_id,
            tenant_id=scope.tenant_id,
            account_id=scope.account_id,
        )

    def _check(self, scope: Scope, ref: ResourceRef, kind: str) -> None:
        authorize(self._authorization, scope, kind, ref.id)
        check_ref(scope, ref, self._account_scope_id, kind)

    def _session(self, scope: Scope, native: BetaManagedAgentsSession) -> Session:
        metadata = cast(dict[str, str], getattr(native, "metadata", None) or {})
        check_record(scope, native.id, metadata)
        version = getattr(getattr(native, "agent", None), "version", None)
        revision = Revision(
            local=version or 0, native=str(version) if version is not None else None
        )
        # The host's durable thread binding remains authoritative. This record
        # adopts the native session identity without writing/rebinding any slot.
        binding = ProviderBinding(
            id=native.id,
            thread=ThreadRef(
                channel=ChannelRef(
                    tenant_id=scope.tenant_id,
                    platform="anthropic",
                    channel_id=metadata.get("daimon_channel", native.id),
                ),
                thread_id=metadata.get("daimon_thread", native.id),
            ),
            provider="anthropic",
            profile="anthropic.managed_agents",
            native_refs={"session": native.id},
            generation=0,
            config_revision=0,
            legacy_account_id=scope.account_id,
        )
        status = getattr(native, "status", None) or "provisioning"
        state = "provisioning" if status == "rescheduling" else status
        return Session(
            ref=self._ref(scope, "session", native.id),
            binding=binding,
            continuity=Continuity(
                conversation="native_session",
                workspace="native_reuse",
                processes="unknown",
            ),
            requested_revision=revision,
            effective_revision=revision,
            state=state,
            native=_native_json(native),
        )

    def _create_request(self, scope: Scope, spec: SessionSpec) -> SessionCreateRequest:
        self._check(scope, spec.agent, "agent")
        if spec.environment is None:
            raise ValueError("Anthropic sessions require an environment")
        self._check(scope, spec.environment, "environment")
        check_record(scope, spec.agent.id, spec.metadata)
        config = SessionCreateConfig()
        for namespace, extension in spec.extensions.items():
            if namespace != "anthropic.session_create":
                raise ValueError(f"unsupported session extension {namespace!r}")
            config = _config(extension, namespace, SessionCreateConfig)
        if config.agent is not None and config.agent.id != spec.agent.id:
            raise ScopeViolation(config.agent.id, "override agent differs from authorized agent")
        agent: str | dict[str, JsonValue] = spec.agent.id
        if config.agent is not None:
            agent = dict(
                cast(
                    dict[str, JsonValue], spec.extensions["anthropic.session_create"].value["agent"]
                )
            )
        elif spec.agent_revision.local > 0:
            agent = {"type": "agent", "id": spec.agent.id, "version": spec.agent_revision.local}
        values: dict[str, object] = {"agent": agent, "environment_id": spec.environment.id}
        if "metadata" in spec.model_fields_set:
            values["metadata"] = dict(spec.metadata)
        if config.vault_ids is not None:
            for vault_id in config.vault_ids:
                authorize(self._authorization, scope, "vault", vault_id)
            values["vault_ids"] = config.vault_ids
        if "resources" in spec.model_fields_set:
            mounts: list[dict[str, JsonValue]] = []
            for resource in spec.resources:
                if resource.resource is not None:
                    self._check(scope, resource.resource, resource.resource.kind)
                if resource.native is None:
                    if resource.kind != "artifact" or resource.resource is None:
                        raise UnsupportedCapability(
                            ("session_resource",), "anthropic.managed_agents"
                        )
                    mount = FileMount(type="file", file_id=resource.resource.id)
                    if "target_path" in resource.model_fields_set:
                        mount = mount.model_copy(update={"mount_path": resource.target_path})
                else:
                    mount = _config(
                        resource.native, "anthropic.session_resource_create", SessionResourceConfig
                    ).resource
                    if isinstance(mount, FileMount):
                        self._check(scope, self._ref(scope, "file", mount.file_id), "file")
                    elif isinstance(mount, MemoryMount):
                        self._check(
                            scope,
                            self._ref(scope, "memory_store", mount.memory_store_id),
                            "memory_store",
                        )
                mounts.append(
                    dict(cast(dict[str, JsonValue], resource.native.value["resource"]))
                    if resource.native is not None
                    else cast(
                        dict[str, JsonValue], mount.model_dump(mode="json", exclude_unset=True)
                    )
                )
            values["resources"] = mounts
        return SessionCreateRequest.model_validate(values)

    async def create(
        self, scope: Scope, spec: SessionSpec, *, key: str, initial: UserMessage | None = None
    ) -> Session:
        if initial is not None:
            raise UnsupportedCapability(
                ("session_create_initial_message",), "anthropic.managed_agents"
            )
        request = self._create_request(scope, spec)
        payload = request.model_dump(mode="python", exclude_unset=True)
        if any(resource["type"] == "github_repository" for resource in request.resources or ()):
            # N6 owns the shared credential resolver/redaction boundary.
            from mux.drivers.anthropic.resources._secrets import (
                credential_request,
                unavailable_secret,
            )

            async def send(kwargs: dict[str, object]) -> BetaManagedAgentsSession:
                return await self._client.beta.sessions.create(**cast(SessionCreateParams, kwargs))

            native = await credential_request(
                scope, request, self._secrets or unavailable_secret, send
            )
        else:
            native = await provider_call(
                self._client.beta.sessions.create(**cast(SessionCreateParams, payload))
            )
        return self._session(scope, native)

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Session:
        self._check(scope, ref, "session")
        return self._session(
            scope, await provider_call(self._client.beta.sessions.retrieve(ref.id))
        )

    async def list(
        self, scope: Scope, *, filters: SessionFilter, page: PageRequest
    ) -> Page[Session]:
        authorize(self._authorization, scope, "session")
        kwargs: SessionListParams = {}
        if filters.agent is not None:
            self._check(scope, filters.agent, "agent")
            kwargs["agent_id"] = filters.agent.id
        if "include_archived" in filters.model_fields_set:
            kwargs["include_archived"] = filters.include_archived
        if filters.created_after is not None:
            kwargs["created_at_gt"] = filters.created_after
        if page.cursor is not None:
            kwargs["page"] = page.cursor
        if page.limit is not None:
            kwargs["limit"] = page.limit
        if page.order is not None:
            kwargs["order"] = page.order
        result = await provider_call(self._client.beta.sessions.list(**kwargs))
        return Page(
            data=tuple(
                self._session(scope, item)
                for item in result.data
                if visible(scope, item.metadata)
                and visible_grant(self._authorization, scope, "session", item.id)
            ),
            has_more=result.has_next_page(),
            next_cursor=result.next_page if result.has_next_page() else None,
        )

    async def walk(self, scope: Scope) -> AsyncIterator[Session]:
        """The existing full workspace SDK paginator, with no request kwargs."""
        authorize(self._authorization, scope, "session")
        async for item in provider_iter(self._client.beta.sessions.list()):
            if visible(scope, getattr(item, "metadata", None)) and visible_grant(
                self._authorization, scope, "session", item.id
            ):
                yield self._session(scope, item)

    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        self._check(scope, ref, "session")
        if self._archive is None:
            raise UnsupportedCapability(("session_archive",), "anthropic.managed_agents")
        return await self._archive.archive(scope, ref, key=key)

    async def plan_update(self, scope: Scope, ref: ResourceRef, desired: SessionSpec) -> UpdatePlan:
        raise UnsupportedCapability(("session_update",), "anthropic.managed_agents")

    async def apply_update(
        self, scope: Scope, plan: UpdatePlan, *, expected: Revision, key: str
    ) -> UpdateReceipt:
        raise UnsupportedCapability(("session_update",), "anthropic.managed_agents")

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        raise UnsupportedCapability(("session_delete",), "anthropic.managed_agents")

    async def export(
        self, scope: Scope, ref: ResourceRef, *, requested: ExportRequirements, key: str
    ) -> SessionExport:
        raise UnsupportedCapability(("session_export",), "anthropic.managed_agents")

    async def restore(
        self,
        scope: Scope,
        export: SessionExport,
        target: SessionSpec,
        *,
        accept_losses: frozenset[str],
        key: str,
    ) -> RestoreReceipt:
        raise UnsupportedCapability(("session_restore",), "anthropic.managed_agents")

    async def migrate(
        self, scope: Scope, ref: ResourceRef, target: ConfigRevision, *, expected: int, key: str
    ) -> Session:
        raise MigrationUnsupported(ref.id)
