"""Explicit Gemini deployment selection; no startup credential discovery."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal
from uuid import UUID

from daimon.core import pricing
from daimon.core.channel_backend import load_backend_agent
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.mux_backend import TurnRuntime, register_turn_backend
from daimon.core.neutral_resources import NeutralResourceHost, ResolvedWorkerSpec
from daimon.core.stores.domain import BackendAgent
from daimon.core.stores.neutral_catalog import AgentRevision
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini import (
    PROFILE,
    GeminiDeployment,
    GeminiJournal,
    GeminiTransportRuntime,
    GeminiUsageRuntime,
    compose_gemini_backend,
    compose_gemini_codec,
    gemini_backend,
)
from daimon.core.turn.gemini_prepare import prepare_gemini_turn
from daimon.core.turn.gemini_state import SQLiteFlashAccounting, SQLiteGeminiStorage
from daimon.core.turn.io import register_turn_codec
from daimon.core.turn.persistence import TurnPersistence, current_persistence
from daimon.core.turn.prepare import ProviderPreparationRequest, register_turn_preparation
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
from mux.contracts.ids import ModelRef, Scope
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ToolSpec
from mux.drivers.gemini.catalog import GeminiCatalogAdapter
from mux.drivers.gemini.core import compile_agent, compile_environment
from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.errors import ScopeViolation, UnsupportedCapability
from mux.profiles.gemini import (
    INLINE_REUSE,
    WORKER_MAX_RUNTIME_S,
    WORKER_MAX_TOTAL_TOKENS,
    WORKER_MODEL,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

Nonblank = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]


class GeminiPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: Literal["gemini.inline_reuse"]
    tenant_id: Nonblank
    platform: Nonblank
    channel_id: Nonblank
    account_id: Nonblank
    config_digest: Nonblank | None = None
    configuration_grant: ConfigurationGrant | None = None
    project: Nonblank
    api_key_env: ApiKeyEnvironment
    records_path: Path
    holds_path: Path
    hold_usd: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    cap_usd: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    agent_name: Nonblank
    system: str | None = None
    # REQUIRED: [] means no tools. Never use the provider's implicit paid tools.
    tools: tuple[ToolSpec, ...]
    environment: EnvironmentSpec
    agent_id: Nonblank | None = None
    skill_ids: tuple[Nonblank, ...] = ()
    source_agent_id: UUID | None = None
    source_agent_version: Annotated[int, Field(gt=0, strict=True)] | None = None
    counterpart_catalog_id: Nonblank | None = None
    counterpart_catalog_revision: Annotated[int, Field(gt=0, strict=True)] | None = None
    worker_materialization: bool = Field(
        default=False, strict=True, exclude_if=lambda value: not value
    )
    max_output_tokens: Annotated[int, Field(gt=0, le=65_536, strict=True)] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _configuration_authorization(self) -> GeminiPlan:
        if self.config_digest is None and self.configuration_grant is None:
            raise ValueError("a deployment requires exact or bounded configuration authorization")
        validate_counterpart_pins(self, worker_materialization=self.worker_materialization)
        if (
            not self.records_path.is_absolute()
            or not self.holds_path.is_absolute()
            or self.records_path.resolve() == self.holds_path.resolve()
            or self.hold_usd > self.cap_usd
        ):
            raise ValueError(
                "a deployment requires distinct absolute accounting paths and a bounded hold"
            )
        return self

    def worker_deployment(
        self, worker: ResolvedWorkerSpec, revision: ConfigRevision
    ) -> GeminiDeployment:
        """Compile the resolved worker without adding resources or implicit execution."""
        spec, drops = worker.spec, worker.declared_drops
        scope = worker.permissions.fleet.scope
        if (
            "memory" not in drops
            or self.max_output_tokens is not None
            or worker.resources
            or (self.worker_materialization and worker.permissions.seal_ids)
            or revision.profile != self.profile
            or revision.model not in (FLASH_PRIMARY, WORKER_MODEL)
            or spec.model != ModelRef(provider="gemini", id=revision.model or "")
            or worker.backend.profile != self.profile
            or worker.backend.model != revision.model
            or worker.backend.backend != revision.backend
            or worker.backend.thread_mode != revision.thread_mode
            or worker.backend.requires != revision.requires
            or scope.is_platform
            or scope.is_legacy_host_authorized
            or (scope.tenant_id, scope.account_id) != (self.tenant_id, self.account_id)
            or worker.permissions.fleet.root.channel != revision.channel
            or (
                not self.worker_materialization
                and (
                    spec.name != self.agent_name
                    or spec.system != self.system
                    or tuple(spec.tools or ()) != self.tools
                    or tuple(pin.id for pin in spec.skills or ()) != self.skill_ids
                )
            )
            or (
                self.worker_materialization
                and (
                    any(tool not in self.tools for tool in spec.tools or ())
                    or any(pin.id not in self.skill_ids for pin in spec.skills or ())
                )
            )
            or ("skills" in drops and spec.skills)
            or ("mcp" in drops and spec.mcp_servers)
            or ("tools" in drops and (spec.tools or spec.mcp_servers))
            or any(tool.kind == "custom" for tool in spec.tools or ())
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        if "sandbox" in drops:
            if (
                worker.environment is not None
                or any(
                    tool.kind != "builtin" or tool.name in INLINE_REUSE.execution_tools
                    for tool in spec.tools or ()
                )
                or spec.mcp_servers
            ):
                raise AdmissionDenied(reason="backend_unsupported")
            environment = self.environment
        else:
            if worker.environment is None or worker.environment != self.environment:
                raise AdmissionDenied(reason="backend_unsupported")
            environment = worker.environment
        if environment.sources:
            raise AdmissionDenied(reason="backend_unsupported")
        try:
            compile_agent(spec)
            if compile_environment(environment) != {"type": "remote", "sources": []}:
                raise AdmissionDenied(reason="backend_unsupported")
        except UnsupportedCapability as error:
            raise AdmissionDenied(reason="backend_unsupported") from error
        return GeminiDeployment(
            revision.digest,
            revision.local,
            self.project,
            spec,
            environment,
            max_total_tokens=WORKER_MAX_TOTAL_TOKENS,
        )

    async def read_counterpart(
        self,
        request: ProviderPreparationRequest,
        *,
        source: BackendAgent,
        resources: NeutralResourceHost,
    ) -> AgentRevision:
        """Read native named-agent intent; no full source or execution proof is implied."""

        expected = None
        if request.worker is not None:
            revision = request.admission.backend_revision
            if revision is None:
                raise AdmissionDenied(reason="backend_unsupported")
            expected = self.worker_deployment(request.worker, revision)

        async def verify_native(checked: AgentRevision) -> None:
            adapter = resources.backend.agents
            if expected is not None and checked.agent.spec != expected.agent:
                raise AdmissionDenied(reason="backend_unsupported")
            if (
                not isinstance(adapter, GeminiCatalogAdapter)
                or not adapter.enabled
                or adapter.account_scope_id != self.project
                or compile_environment(self.environment) != {"type": "remote", "sources": []}
                or (
                    (not self.worker_materialization or expected is None)
                    and (
                        checked.agent.spec.system != self.system
                        or checked.agent.spec.tools != self.tools
                    )
                )
            ):
                raise AdmissionDenied(reason="backend_unsupported")

        return await read_counterpart(
            self,
            request,
            source=source,
            resources=resources,
            verify_native=verify_native,
            worker_materialization=self.worker_materialization,
        )


def load_gemini_deployment(request: ProviderPreparationRequest) -> GeminiPlan:
    revision = request.admission.backend_revision
    origin = request.admission.backend_revision_origin
    if not fixed_plan_origin_allowed(origin) and (
        origin != "agent" or not request.deps.agent_backends
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    if revision is None or revision.profile != PROFILE:
        raise AdmissionDenied(reason="backend_unsupported")
    plan = select_runtime_manifest(request, GeminiPlan)
    if origin == "agent":
        validate_counterpart_request(
            plan, request, worker_materialization=plan.worker_materialization
        )
    return plan


def build_gemini_runtime(request: ProviderPreparationRequest) -> TurnRuntime:
    revision, scope = request.admission.backend_revision, request.scope
    if (
        revision is None
        or revision.model
        not in ((FLASH_PRIMARY, WORKER_MODEL) if request.worker is not None else (FLASH_PRIMARY,))
        or request.deps.state_store is None
        or (
            request.worker is not None
            and request.deadline > request.now() + timedelta(seconds=WORKER_MAX_RUNTIME_S)
        )
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    plan = load_gemini_deployment(request)
    if plan.max_output_tokens is not None and (
        request.worker is not None
        or request.admission.backend_revision_origin == "agent"
        or plan.agent_id is not None
        or plan.skill_ids
        or plan.worker_materialization
    ):
        raise AdmissionDenied(reason="backend_unsupported")
    agent = AgentSpec(
        name=plan.agent_name,
        model=ModelRef(provider="gemini", id=FLASH_PRIMARY),
        system=plan.system,
        tools=plan.tools,
    )
    # Validate native policy before reading the selected credential or opening
    # persistence. Unsupported MCP, confirmations and workspace sources refuse.
    if request.worker is not None:
        deployment = plan.worker_deployment(request.worker, revision)
    else:
        try:
            compile_agent(agent, max_output_tokens=plan.max_output_tokens)
            compile_environment(plan.environment, model_only=plan.max_output_tokens is not None)
        except UnsupportedCapability as error:
            if plan.max_output_tokens is None:
                raise
            raise AdmissionDenied(reason="backend_unsupported") from error
        deployment = GeminiDeployment(
            revision.digest,
            revision.local,
            plan.project,
            agent,
            plan.environment,
            max_output_tokens=plan.max_output_tokens,
        )
    namespace = json.dumps([scope.tenant_id, scope.account_id, plan.project], separators=(",", ":"))

    def transport(config: ConfigRevision, authorized: Scope) -> GeminiTransportRuntime:
        if config != revision or authorized != scope:
            raise ScopeViolation(request.thread_id, "Gemini deployment differs from admitted turn")
        secret = os.environ.get(plan.api_key_env)
        if secret is None or not secret.strip():
            raise AdmissionDenied(reason="backend_unsupported")
        from mux.drivers.gemini.deployment import configured_catalog_transport, configured_transport

        return GeminiTransportRuntime(
            configured_transport(api_key=secret),
            configured_catalog_transport(api_key=secret)
            if request.admission.backend_revision_origin == "agent"
            else None,
        )

    async def read(preparation: ProviderPreparationRequest) -> AgentRevision:
        validate_counterpart_request(
            plan, preparation, worker_materialization=plan.worker_materialization
        )
        if preparation.worker != request.worker:
            raise AdmissionDenied(reason="backend_unsupported")
        source = preparation.source
        if preparation.worker is not None:
            if source is None or plan.worker_deployment(preparation.worker, revision) != deployment:
                raise AdmissionDenied(reason="backend_unsupported")
        elif source is None:
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
        return await plan.read_counterpart(
            preparation, source=source, resources=resources(preparation)
        )

    def resources(preparation: ProviderPreparationRequest) -> NeutralResourceHost:
        if plan.worker_materialization:
            persistence = current_persistence.get()
            if persistence is None:
                raise AdmissionDenied(reason="backend_unsupported")
            persistence.check_session(preparation.scope, None)
        backend = gemini_backend(revision, preparation.scope, runtime, plan.project)
        return NeutralResourceHost(
            backend,
            preparation.deps.sessionmaker,
            scope=preparation.scope,
            account_scope_id=plan.project,
            operation_key="counterpart-read",
            resource_catalog_enabled=True,
            catalog_adapter=backend.catalog_adapter,
        )

    async def materialize(preparation: ProviderPreparationRequest) -> AgentRevision:
        async def host(_: TurnPersistence, worker: ResolvedWorkerSpec) -> NeutralResourceHost:
            if plan.worker_deployment(worker, revision) != deployment:
                raise AdmissionDenied(reason="backend_unsupported")
            return resources(preparation)

        return await materialize_counterpart(plan, preparation, request, resources=host)

    runtime = TurnRuntime(
        transport_factory=transport,
        read_agent_counterpart=read,
        materialize_worker=materialize if plan.worker_materialization else None,
        journal=GeminiJournal(
            SQLiteGeminiStorage(plan.records_path, namespace=namespace),
            request.deps.state_store,
            deployment,
        ),
        usage_store=GeminiUsageRuntime(
            SQLiteFlashAccounting(
                plan.holds_path, namespace=namespace, hold_usd=plan.hold_usd, cap_usd=plan.cap_usd
            ),
            MappingProxyType(
                {model: price for (_, model), price in pricing.provider_prices(PROFILE).items()}
            ),
        ),
    )

    return runtime


def register_runtime() -> None:
    """Install the provider's host callbacks without transports or credential access."""
    from daimon.core.turn.runtimes import register_channel_runtime

    register_turn_backend(PROFILE, compose_gemini_backend, fallback=True)
    register_turn_codec(PROFILE, compose_gemini_codec, fallback=True)
    register_turn_preparation(PROFILE, prepare_gemini_turn, fallback=True)
    register_channel_runtime(
        PROFILE,
        build_gemini_runtime,
        tariffs=partial(pricing.provider_prices, PROFILE),
        fallback=True,
    )
