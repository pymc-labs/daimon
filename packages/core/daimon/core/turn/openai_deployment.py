"""Operator-owned OpenAI deployment plans, loaded only for explicit admission.

The manifest contains scoped provisioned identities and credential environment
names, never credentials. Transport construction follows the preparer's policy
recheck. No SDK client or provider configuration is loaded during app startup.
"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from daimon.core.channel_backend import load_backend_agent
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.neutral_resources import NeutralResourceHost, ResolvedWorkerSpec
from daimon.core.pricing import provider_prices
from daimon.core.sessions import native_session_budget_cents
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_codec import PROFILE
from daimon.core.turn.openai_host import OpenAIHostRuntime, driver, register_host
from daimon.core.turn.openai_state import OpenAIRecoveryJournal, OpenAIUsageRevisions
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.core.turn.runtime_manifest import (
    ApiKeyEnvironment,
    ConfigurationGrant,
    fixed_plan_origin_allowed,
    materialize_counterpart,
    read_counterpart,
    select_runtime_manifest,
    validate_counterpart_pins,
    validate_counterpart_request,
)
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import EnvironmentSpec, SessionSpec
from mux.drivers.openai.mcp_auth import KEY as MCP_METADATA_KEY
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.skill_bindings import reserved as skill_metadata_key
from mux.drivers.openai.transport import Transport
from mux.errors import ScopeViolation
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

Nonblank = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]
_MANIFEST = TypeAdapter(list[dict[str, JsonValue]])
_MAX_MANIFEST_BYTES = 1_048_576


class OpenAIDeployment(BaseModel):
    """An operator authorization for one account's exact channel configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: Literal["openai.persistent_workspace"]
    tenant_id: Nonblank
    platform: Nonblank
    channel_id: Nonblank
    account_id: Nonblank
    config_digest: Nonblank | None = None
    configuration_grant: ConfigurationGrant | None = None
    project: Nonblank
    agent_id: Nonblank | None = None
    environment_id: Nonblank
    api_key_env: ApiKeyEnvironment
    spend_limit_usd_cents: Annotated[int, Field(gt=0, strict=True)] | None = None
    skill_ids: tuple[Nonblank, ...] = ()
    worker_materialization: bool = Field(
        default=False, strict=True, exclude_if=lambda value: not value
    )
    source_agent_id: UUID | None = None
    source_agent_version: Annotated[int, Field(gt=0, strict=True)] | None = None
    counterpart_catalog_id: Nonblank | None = None
    counterpart_catalog_revision: Annotated[int, Field(gt=0, strict=True)] | None = None

    @model_validator(mode="after")
    def _configuration_authorization(self) -> OpenAIDeployment:
        if self.config_digest is None and self.configuration_grant is None:
            raise ValueError("a deployment requires exact or bounded configuration authorization")
        validate_counterpart_pins(self, worker_materialization=self.worker_materialization)
        if not self.worker_materialization and self.agent_id is None:
            raise ValueError("a fixed deployment requires a native agent identity")
        return self

    async def read_counterpart(
        self,
        request: ProviderPreparationRequest,
        *,
        source: BackendAgent,
        resources: NeutralResourceHost,
    ) -> AgentRevision:
        """Read the operator-pinned catalog component; complete source proof remains separate."""

        worker = request.worker
        expected = self.worker_environment(request, worker) if worker is not None else None

        async def verify_native(checked: AgentRevision) -> None:
            if worker is not None:
                if checked.agent.spec != worker.spec:
                    raise AdmissionDenied(reason="backend_unsupported")
                # The catalog verifies compiled tools, pins and instructions;
                # independently retain every authored metadata stamp as well.
                native = await resources.backend.agents.retrieve(request.scope, checked.agent.ref)
                metadata = {
                    key: value
                    for key, value in (native.spec.metadata or {}).items()
                    if key != "mux_tenant"
                    and key != MCP_METADATA_KEY
                    and not skill_metadata_key(key)
                }
                if metadata != dict(worker.spec.metadata or {}):
                    raise AdmissionDenied(reason="backend_unsupported")
            await self.verify_environment(request, resources=resources, expected=expected)

        return await read_counterpart(
            self,
            request,
            source=source,
            resources=resources,
            verify_native=verify_native,
            worker_materialization=self.worker_materialization,
        )

    async def verify_environment(
        self,
        request: ProviderPreparationRequest,
        *,
        resources: NeutralResourceHost,
        expected: EnvironmentSpec | None,
    ) -> None:
        scope = request.scope
        environment = await resources.backend.environments.retrieve(
            scope,
            ResourceRef(
                id=self.environment_id,
                kind="environment",
                provider="openai",
                account_scope_id=self.project,
                tenant_id=scope.tenant_id,
                account_id=scope.account_id,
            ),
        )
        if expected is not None:
            expected = expected.model_copy(
                update={
                    "description": None if expected.description == "" else expected.description,
                    "metadata": None if expected.metadata == {} else expected.metadata,
                }
            )
        if (
            environment.lifecycle != "active"
            or environment.spec.execution != "hosted"
            or (expected is not None and environment.spec != expected)
        ):
            raise AdmissionDenied(reason="backend_unsupported")

    def worker_environment(
        self, request: ProviderPreparationRequest, worker: ResolvedWorkerSpec
    ) -> EnvironmentSpec:
        """Refuse unrepresentable plans before reading or allocating native resources."""
        revision = request.admission.backend_revision
        drops, spec, environment = worker.declared_drops, worker.spec, worker.environment
        if (
            revision is None
            or worker.backend.content_digest() != revision.content_digest()
            or revision.profile != self.profile
            or worker.spec.model.provider != "openai"
            or worker.spec.model.id != revision.model
            or worker.permissions.fleet.scope != request.scope
            or "memory" not in drops
            or "sandbox" in drops
            or "tools" in drops
            or worker.resources
            or environment is None
            or environment.execution != "hosted"
            or environment.description not in (None, "")
            or environment.scope is not None
            or environment.metadata
            or environment.native_config is not None
            or environment.network is None
            or environment.sources != ()
            or environment.packages is None
            or set(environment.packages) - {"python", "npm", "system"}
            or spec.extensions
            or spec.model.options
            or spec.description is not None
            or any(
                key == "mux_tenant" or key == MCP_METADATA_KEY or skill_metadata_key(key)
                for key in spec.metadata or {}
            )
            or not any(tool.kind == "builtin" and tool.name == "bash" for tool in spec.tools or ())
            or any(tool.permission != "auto" for tool in spec.tools or ())
            or any(tool.kind == "custom" for tool in spec.tools or ())
            or ("skills" in drops and spec.skills)
            or (
                "mcp" in drops
                and (
                    spec.mcp_servers or any(tool.kind == "mcp_toolset" for tool in spec.tools or ())
                )
            )
            or any(connection.credential_ref is not None for connection in spec.mcp_servers or ())
        ):
            # Conversation-only creation needs a consumed initial-input edge.
            # Never substitute a hosted container for a no-execution declaration.
            raise AdmissionDenied(reason="backend_unsupported")
        return environment


def load_deployment(request: ProviderPreparationRequest) -> OpenAIDeployment:
    origin = request.admission.backend_revision_origin
    if fixed_plan_origin_allowed(origin):
        return _manifest_deployment(request)
    if origin != "agent" or not request.deps.agent_backends:
        raise AdmissionDenied(reason="backend_unsupported")
    deployment = _manifest_deployment(request)
    validate_counterpart_request(
        deployment, request, worker_materialization=deployment.worker_materialization
    )
    return deployment


def _manifest_deployment(request: ProviderPreparationRequest) -> OpenAIDeployment:
    revision = request.admission.backend_revision
    if revision is None or revision.profile != "openai.persistent_workspace":
        raise AdmissionDenied(reason="backend_unsupported")
    return select_runtime_manifest(request, OpenAIDeployment)


def authorized_deployments(tenant_id: str) -> tuple[OpenAIDeployment, ...]:
    """Every explicit OpenAI deployment the operator's manifest grants one tenant.

    For callers outside a turn (the CLI, MCP tools) that route a tenant's or
    channel's resources natively only where the operator authorized it. The
    same manifest and limits as `load_deployment`. No manifest, no row for the
    tenant, or a file that cannot be read or validated means none: nothing is
    authorized, and callers keep their existing path.
    """
    manifest_path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if not manifest_path:
        return ()
    try:
        with Path(manifest_path).open("rb") as manifest:
            raw = manifest.read(_MAX_MANIFEST_BYTES + 1)
        if len(raw) > _MAX_MANIFEST_BYTES:
            return ()
        return tuple(
            OpenAIDeployment.model_validate(row)
            for row in _MANIFEST.validate_json(raw)
            if row.get("profile") == "openai.persistent_workspace"
            and row.get("tenant_id") == tenant_id
        )
    except (OSError, ValidationError):
        return ()


def build_openai_runtime(request: ProviderPreparationRequest) -> OpenAIHostRuntime:
    revision = request.admission.backend_revision
    if revision is None or revision.model != "gpt-6-luna":
        raise AdmissionDenied(reason="backend_unsupported")
    deployment = load_deployment(request)
    if deployment.worker_materialization and request.worker is None:
        raise AdmissionDenied(reason="backend_unsupported")
    spend_limit = deployment.spend_limit_usd_cents
    native_budget_usd = request.native_budget_usd
    if request.worker is not None and native_budget_usd is not None:
        raise AdmissionDenied(reason="backend_unsupported")
    budget = (
        request.worker.limits.native_budget_usd if request.worker is not None else native_budget_usd
    )
    if budget is not None:
        if request.worker is not None:
            request.worker.limits.require_native_dispatch(request.now())
        try:
            requested = native_session_budget_cents(budget)
        except ValueError:
            raise AdmissionDenied(reason="backend_unsupported") from None
        if spend_limit is not None and spend_limit != requested:
            raise AdmissionDenied(reason="backend_unsupported")
        spend_limit = requested
    scope = request.scope
    worker_agent: ResourceRef | None = None
    allocating = False

    def check(config: ConfigRevision, authorized: Scope) -> None:
        if config != revision or authorized != scope:
            raise ScopeViolation(request.thread_id, "deployment differs from admitted turn")

    async def plan(preparation: ProviderPreparationRequest) -> SessionSpec:
        if preparation.admission.backend_revision != revision or preparation.scope != scope:
            raise ScopeViolation(request.thread_id, "native plan differs from admitted turn")

        def ref(id_: str, kind: str) -> ResourceRef:
            return ResourceRef(
                id=id_,
                kind=kind,
                provider="openai",
                account_scope_id=deployment.project,
                tenant_id=scope.tenant_id,
                account_id=scope.account_id,
            )

        agent = worker_agent
        if not deployment.worker_materialization:
            if deployment.agent_id is None:
                raise AdmissionDenied(reason="backend_unsupported")
            agent = ref(deployment.agent_id, "agent")
        if agent is None:
            raise AdmissionDenied(reason="backend_unsupported")
        if preparation.admission.backend_revision_origin == "agent":
            checked = preparation.counterpart
            if not preparation.deps.agent_backends or checked is None:
                raise AdmissionDenied(reason="backend_unsupported")
            validate_counterpart_request(
                deployment, preparation, worker_materialization=deployment.worker_materialization
            )
            worker = preparation.worker
            if deployment.worker_materialization and worker is not None:
                if worker_agent is None or checked.agent.ref != worker_agent:
                    raise AdmissionDenied(reason="backend_unsupported")
            elif (
                checked.catalog_id != deployment.counterpart_catalog_id
                or checked.local_revision != deployment.counterpart_catalog_revision
                or checked.agent.ref != agent
            ):
                raise AdmissionDenied(reason="backend_unsupported")
            if worker is not None:
                deployment.worker_environment(preparation, worker)
                if checked.agent.spec != worker.spec:
                    raise AdmissionDenied(reason="backend_unsupported")
            agent = checked.agent.ref
        return SessionSpec(
            agent=agent,
            agent_revision=Revision(local=0),
            environment=ref(deployment.environment_id, "environment"),
            config_revision=revision.local,
        )

    def authorize(authorized: Scope, kind: str, id_: str | None) -> bool:
        if authorized != scope:
            return False
        if kind == "session":
            # The host driver obtains existing sessions from its persisted scoped
            # binding; creation publishes the acknowledged native identity there.
            return True
        if kind == "agent" and deployment.worker_materialization:
            if allocating and id_ is None:
                return True
            if worker_agent is not None and id_ == worker_agent.id:
                return True
        return id_ is not None and (kind, id_) in {
            ("agent", deployment.agent_id),
            ("environment", deployment.environment_id),
            *(("skill", skill) for skill in deployment.skill_ids),
        }

    def transport(config: ConfigRevision, authorized: Scope) -> Transport:
        check(config, authorized)
        secret = os.environ.get(deployment.api_key_env)
        if secret is None or not secret.strip():
            raise AdmissionDenied(reason="backend_unsupported")
        # Import/construction is selected-only. The private driver owns SDK
        # request/stream lifetimes and disables retry of uncertain mutations.
        from mux.drivers.openai.deployment import configured_transport

        return configured_transport(api_key=secret, project=deployment.project)

    async def read(preparation: ProviderPreparationRequest) -> AgentRevision:
        validate_counterpart_request(
            deployment, preparation, worker_materialization=deployment.worker_materialization
        )
        worker = preparation.worker
        if worker is not None:
            deployment.worker_environment(preparation, worker)
        source = preparation.source
        if source is None:
            if worker is not None and deployment.worker_materialization:
                raise AdmissionDenied(reason="backend_unsupported")
            source = await load_backend_agent(
                preparation.deps.anthropic,
                preparation.deps.sessionmaker,
                tenant_id=preparation.tenant_id,
                agent_name=(preparation.admission.agent.metadata or {}).get(
                    MA_METADATA_KEY_NAME, preparation.admission.config.agent_name
                )
                or "",
                actual_agent=preparation.admission.agent,
            )
        resources = NeutralResourceHost(
            driver(runtime, revision, scope),
            preparation.deps.sessionmaker,
            scope=scope,
            account_scope_id=deployment.project,
            operation_key="counterpart-read",
        )
        return await deployment.read_counterpart(preparation, source=source, resources=resources)

    async def materialize(preparation: ProviderPreparationRequest) -> AgentRevision:
        nonlocal allocating, worker_agent

        async def host(active: TurnPersistence, worker: ResolvedWorkerSpec) -> NeutralResourceHost:
            nonlocal allocating
            expected = deployment.worker_environment(preparation, worker)
            resources = NeutralResourceHost(
                driver(runtime, revision, scope),
                preparation.deps.sessionmaker,
                scope=scope,
                account_scope_id=deployment.project,
                operation_key=active.operation_key,
            )
            await deployment.verify_environment(preparation, resources=resources, expected=expected)
            allocating = True
            return resources

        try:
            row = await materialize_counterpart(deployment, preparation, request, resources=host)
        finally:
            allocating = False
        worker_agent = row.agent.ref
        return row

    runtime = OpenAIHostRuntime(
        transport_factory=transport,
        read_agent_counterpart=read,
        materialize_worker=materialize if deployment.worker_materialization else None,
        journal=OpenAIRecoveryJournal(),
        usage_store=OpenAIUsageRevisions(),
        account_scope_id=deployment.project,
        price=provider_prices(PROFILE).get((revision.backend, revision.model)),
        session_plan=plan,
        authorization=authorize,
        controls=SessionControls(
            model="gpt-6-luna",
            multi_agent_enabled=False,
            container_size="small",
            spend_limit_usd_cents=spend_limit,
            require_spend_control_echo=budget is not None,
        ),
    )

    return runtime


def register_runtime() -> None:
    """Install the provider's host callbacks without transports or credential access."""
    from daimon.core.turn.runtimes import register_channel_runtime

    register_host()
    register_channel_runtime(
        PROFILE,
        build_openai_runtime,
        authorizations=authorized_deployments,
        tariffs=partial(provider_prices, PROFILE),
        fallback=True,
    )
