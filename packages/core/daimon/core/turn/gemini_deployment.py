"""Explicit Gemini deployment selection; no startup credential discovery."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, cast
from uuid import UUID

from daimon.core import pricing
from daimon.core.channel_backend import load_backend_agent
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.mcp_auth import read_private_credential_bytes, resolve_agent_mcp_credential
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
    SourceCapabilities,
    fixed_plan_origin_allowed,
    materialize_counterpart,
    read_counterpart,
    represented_source,
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
from mux.drivers.gemini.skill_deployment import (
    GCSBundleDeployment,
    RetainedSkillDeployments,
    verify_retained_deployments,
)
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
    JsonValue,
    field_validator,
    model_validator,
)

Nonblank = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]


def _private_storage_credential(
    path: Path, *, storage_until: datetime, current: datetime
) -> tuple[str, datetime]:
    from mux.drivers.gemini.mcp_auth import credential

    value = cast(JsonValue, json.loads(read_private_credential_bytes(path)))
    if not isinstance(value, dict) or set(value) != {"access_token", "expires_at"}:
        raise ValueError("storage credential requires exact token/expiry fields")
    expiry = value["expires_at"]
    if not isinstance(expiry, str):
        raise ValueError("storage credential expiry must be a timestamp")
    expires = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    if expires.tzinfo is None or not storage_until + timedelta(
        seconds=30
    ) < expires <= current + timedelta(hours=1):
        raise ValueError("storage credential expiry does not cover the bounded turn")
    return credential(value["access_token"]), expires


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
    source_capabilities: SourceCapabilities | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    mcp_credential_file: Path | None = Field(default=None, exclude_if=lambda value: value is None)
    gcs_credential_file: Path | None = Field(default=None, exclude_if=lambda value: value is None)
    gcs_mount_bucket: Nonblank | None = Field(default=None, exclude_if=lambda value: value is None)
    gcs_mount_prefix: Nonblank | None = Field(default=None, exclude_if=lambda value: value is None)
    skill_deployments: tuple[GCSBundleDeployment, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @field_validator("skill_deployments", mode="before")
    @classmethod
    def _closed_deployments(cls, value: object) -> object:
        fields = {"tenant_id", "account_id", "account_scope_id", "version", "source", "files"}
        if isinstance(value, (tuple, list)) and any(
            isinstance(item, dict) and set(cast(dict[str, object], item)) != fields
            for item in cast(tuple[object, ...] | list[object], value)
        ):
            raise ValueError("skill deployment receipt fields must be exact")
        return cast(object, value)

    @model_validator(mode="after")
    def _configuration_authorization(self) -> GeminiPlan:
        if self.config_digest is None and self.configuration_grant is None:
            raise ValueError("a deployment requires exact or bounded configuration authorization")
        validate_counterpart_pins(self, worker_materialization=self.worker_materialization)
        if self.source_capabilities is not None:
            if (
                self.worker_materialization
                or self.max_output_tokens is not None
                or self.mcp_credential_file is None
                or not self.mcp_credential_file.is_absolute()
                or self.source_capabilities.model
                != ModelRef(provider=INLINE_REUSE.provider, id=FLASH_PRIMARY)
                or tuple(item.target.id for item in self.source_capabilities.skills)
                != self.skill_ids
                or self.gcs_credential_file is None
                or not self.gcs_credential_file.is_absolute()
                or self.gcs_mount_bucket is None
                or self.gcs_mount_prefix is None
                or not self.skill_deployments
            ):
                raise ValueError(
                    "full source capabilities require scoped deployment and credential inputs"
                )
            RetainedSkillDeployments(self.skill_deployments)
        elif (
            self.mcp_credential_file is not None
            or self.gcs_credential_file is not None
            or self.gcs_mount_bucket is not None
            or self.gcs_mount_prefix is not None
            or self.skill_deployments
        ):
            raise ValueError("capability dependencies require a verified source capability plan")
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
        if self.source_capabilities is not None:
            raise AdmissionDenied(reason="backend_unsupported")
        spec, drops = worker.spec, worker.declared_drops
        worker.limits.require_profile(INLINE_REUSE)
        bounded = worker.limits.max_input_tokens is not None
        if bounded and (revision.model != FLASH_PRIMARY or "sandbox" not in drops):
            raise AdmissionDenied(reason="backend_unsupported")
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
            compile_agent(spec, max_output_tokens=worker.limits.max_output_tokens)
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
            max_total_tokens=None if bounded else WORKER_MAX_TOTAL_TOKENS,
            max_input_tokens=worker.limits.max_input_tokens,
            max_output_tokens=worker.limits.max_output_tokens,
        )

    def deployment(self, request: ProviderPreparationRequest) -> GeminiDeployment:
        """Compile the prepared source target without borrowing worker authority."""
        revision = request.admission.backend_revision
        if revision is None:
            raise AdmissionDenied(reason="backend_unsupported")
        if request.worker is not None:
            return self.worker_deployment(request.worker, revision)
        target, source = request.target_spec, request.source
        from daimon.core.turn.runtime_manifest import source_instructions_preserved

        represented = (
            represented_source(source, target)
            if source is not None and self.source_capabilities is not None
            else None
        )
        full = self.source_capabilities is not None
        if (
            request.admission.backend_revision_origin != "agent"
            or target is None
            or source is None
            or source.effective_spec is None
            or self.worker_materialization
            or self.max_output_tokens is not None
            or revision.profile != self.profile
            or revision.model != FLASH_PRIMARY
            or target.model != ModelRef(provider=INLINE_REUSE.provider, id=FLASH_PRIMARY)
            or "memory" not in target.declared_drops
            or target.name != source.effective_spec.name
            or not source_instructions_preserved(source.effective_spec.system, target.system)
            or (full and represented is None)
            or (full and {"tools", "skills", "mcp"}.intersection(target.declared_drops))
            or (not full and target.tools != ())
            or (not full and target.skills != ())
            or (not full and target.mcp_servers != ())
            or target.extensions
            or (not full and self.tools)
            or (not full and self.skill_ids)
            or (full and tuple(target.tools or ()) != self.tools)
            or (full and tuple(pin.id for pin in target.skills or ()) != self.skill_ids)
            or (full and request.worker is not None)
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        try:
            compile_agent(target, authenticated_mcp=full)
            if compile_environment(self.environment) != {"type": "remote", "sources": []}:
                raise AdmissionDenied(reason="backend_unsupported")
        except UnsupportedCapability as error:
            raise AdmissionDenied(reason="backend_unsupported") from error
        return GeminiDeployment(
            revision.digest, revision.local, self.project, target, self.environment
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
        if request.worker is not None or request.target_spec is not None:
            expected = self.deployment(request)

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
                    expected is None
                    and (
                        checked.agent.spec.system != self.system
                        or checked.agent.spec.tools != self.tools
                    )
                )
            ):
                raise AdmissionDenied(reason="backend_unsupported")
            if self.source_capabilities is not None:
                mounted = await adapter.skill_deployments(request.scope, checked.agent)
                if set(mounted) != set(self.skill_deployments) or len(mounted) != len(
                    self.skill_deployments
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
    if plan.source_capabilities is not None and origin != "agent":
        raise AdmissionDenied(reason="backend_unsupported")
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
    if request.worker is not None or request.target_spec is not None:
        deployment = plan.deployment(request)
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
    counterpart_verified = False

    async def verify_storage() -> str | None:
        if plan.source_capabilities is None:
            return
        if plan.gcs_credential_file is None:
            raise AdmissionDenied(reason="backend_unsupported")
        try:
            source_token, expires_at = _private_storage_credential(
                plan.gcs_credential_file, storage_until=request.deadline, current=request.now()
            )
        except (OSError, ValueError):
            raise ScopeViolation(
                "skill-deployment", "invalid explicit storage credential"
            ) from None
        from mux.drivers.gemini.deployment import downscoped_storage_token

        if plan.gcs_mount_bucket is None or plan.gcs_mount_prefix is None:
            raise AdmissionDenied(reason="backend_unsupported")
        token = await asyncio.to_thread(
            downscoped_storage_token,
            token=source_token,
            expires_at=expires_at,
            retained_until=request.deadline,
            bucket=plan.gcs_mount_bucket,
            prefix=plan.gcs_mount_prefix,
            sources=tuple(item.source for item in plan.skill_deployments),
        )
        await verify_retained_deployments(
            plan.skill_deployments,
            token=token,
            retained_until=request.deadline + timedelta(days=7),
        )
        return token

    async def resolve_storage(authorized: Scope, sources: tuple[str, ...]) -> str:
        if (
            not counterpart_verified
            or authorized != scope
            or sources != tuple(item.source for item in plan.skill_deployments)
        ):
            raise ScopeViolation(
                "skill-deployment", "storage credential has no verified source binding"
            )
        token = await verify_storage()
        if token is None:
            raise ScopeViolation("skill-deployment", "storage credential unavailable")
        return token

    async def resolve_mcp(authorized: Scope, reference: str, destination: str) -> str:
        expected = f"caller-mcp:{scope.tenant_id}:{scope.account_id}:{plan.source_agent_id}"
        public_url = request.deps.mcp.public_url
        if (
            not counterpart_verified
            or authorized != scope
            or reference != expected
            or public_url is None
            or destination.rstrip("/") != str(public_url).rstrip("/")
            or plan.mcp_credential_file is None
            or request.deps.mcp.jwt_secret is None
            or plan.source_agent_id is None
        ):
            raise ScopeViolation("mcp-credential", "MCP credential has no verified source binding")
        try:
            assert plan.source_capabilities is not None
            token = await resolve_agent_mcp_credential(
                request.deps.sessionmaker,
                path=plan.mcp_credential_file,
                secret=request.deps.mcp.jwt_secret.get_secret_value().encode(),
                scope=scope,
                agent_id=plan.source_agent_id,
                destination=destination,
                tools=plan.source_capabilities.mcp_tools,
                chat_execution=True,
                now=request.now(),
            )
            await verify_storage()
            return token
        except (OSError, ValueError):
            raise ScopeViolation("mcp-credential", "invalid scoped MCP credential") from None

    def transport(config: ConfigRevision, authorized: Scope) -> GeminiTransportRuntime:
        if config != revision or authorized != scope:
            raise ScopeViolation(request.thread_id, "Gemini deployment differs from admitted turn")
        secret = os.environ.get(plan.api_key_env)
        if secret is None or not secret.strip():
            raise AdmissionDenied(reason="backend_unsupported")
        from mux.drivers.gemini.deployment import configured_catalog_transport, configured_transport

        return GeminiTransportRuntime(
            configured_transport(api_key=secret, protect_storage_credentials=True)
            if plan.source_capabilities is not None
            else configured_transport(api_key=secret),
            configured_catalog_transport(api_key=secret)
            if request.admission.backend_revision_origin == "agent"
            else None,
            mcp_secrets=resolve_mcp if plan.source_capabilities is not None else None,
            gcs_secrets=resolve_storage if plan.source_capabilities is not None else None,
            skill_bundle_publisher=RetainedSkillDeployments(plan.skill_deployments)
            if plan.source_capabilities is not None
            else None,
        )

    async def read(preparation: ProviderPreparationRequest) -> AgentRevision:
        nonlocal counterpart_verified
        counterpart_verified = False
        validate_counterpart_request(
            plan, preparation, worker_materialization=plan.worker_materialization
        )
        if preparation.worker != request.worker:
            raise AdmissionDenied(reason="backend_unsupported")
        source = preparation.source
        if preparation.worker is not None or preparation.target_spec is not None:
            if source is None or plan.deployment(preparation) != deployment:
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
        checked = await plan.read_counterpart(
            preparation, source=source, resources=resources(preparation)
        )
        await verify_storage()
        counterpart_verified = True
        return checked

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
