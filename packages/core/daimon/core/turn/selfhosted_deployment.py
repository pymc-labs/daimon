"""Explicit self-hosted manifests; only the admitted tenant can resolve credentials."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.core.turn.selfhosted_codec import PROFILE
from daimon.core.turn.selfhosted_host import SelfHostedHostRuntime, register_host
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ResourceRef, Scope, SkillRef
from mux.contracts.resources import AgentSpec, SessionSpec
from mux.drivers.selfhosted.transport import DirectTransport, FoundryInvocationsTransport
from mux.errors import ScopeViolation
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)

_ROWS = TypeAdapter(list[dict[str, JsonValue]])
_MAX_BYTES = 1_048_576


class SelfHostedDeployment(BaseModel):
    """A credential-free operator grant for one exact channel configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    profile: Literal["selfhosted.loop"] = PROFILE
    tenant_id: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    config_digest: str = Field(min_length=1)
    agent_id: str = Field(pattern=r"^sh_agent_")
    environment_id: str = Field(pattern=r"^sh_env_")
    infrastructure_usd: Decimal = Field(ge=0, allow_inf_nan=False)
    max_tool_calls: int = Field(default=100, ge=0)
    deadline_s: float = Field(default=300, gt=0, allow_inf_nan=False)

    @property
    def project(self) -> str:
        return self.tenant_id


def authorized_deployments(tenant_id: str) -> tuple[SelfHostedDeployment, ...]:
    path = os.environ.get("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    if not path:
        return ()
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return ()
        return tuple(
            SelfHostedDeployment.model_validate(row)
            for row in _ROWS.validate_json(raw)
            if row.get("profile") == PROFILE and row.get("tenant_id") == tenant_id
        )
    except (OSError, ValidationError):
        return ()


def build_selfhosted_runtime(request: ProviderPreparationRequest) -> SelfHostedHostRuntime:
    from daimon.core.channel_backend import BackendUnsupported, check_backend

    revision, scope = request.admission.backend_revision, request.scope
    if revision is None or revision.profile != PROFILE or revision.model is None:
        raise AdmissionDenied(reason="backend_unsupported")
    try:
        check_backend(revision)
    except BackendUnsupported as error:
        raise AdmissionDenied(reason="backend_unsupported") from error
    matches = tuple(
        deployment
        for deployment in authorized_deployments(scope.tenant_id)
        if deployment.platform == request.platform
        and deployment.channel_id == revision.channel.channel_id
        and deployment.account_id == scope.account_id
        and deployment.config_digest == revision.content_digest()
    )
    if len(matches) != 1:
        raise AdmissionDenied(reason="backend_unsupported")
    deployment = matches[0]
    if (
        request.admission.agent.id != deployment.agent_id
        or request.admission.environment.id != deployment.environment_id
    ):
        raise ScopeViolation(
            request.thread_id, "self-hosted manifest differs from admitted resources"
        )
    from daimon.core.config import load_settings
    from daimon.core.selfhosted_usage import (
        admit_selfhosted_usage,
        record_selfhosted_usage,
        require_selfhosted_price,
    )
    from daimon.core.stores.selfhosted_resources import SelfHostedAgents, SelfHostedEnvironments
    from mux.drivers.selfhosted.config import HarnessConfig

    model = revision.model
    settings = load_settings()
    billing = settings.billing.selfhosted
    price = require_selfhosted_price(billing, revision.model)
    agents, environments = (
        SelfHostedAgents(request.deps.sessionmaker),
        SelfHostedEnvironments(request.deps.sessionmaker),
    )

    def ref(kind: str, id_: str) -> ResourceRef:
        return ResourceRef(
            id=id_,
            kind=kind,
            provider="selfhosted",
            account_scope_id=scope.tenant_id,
            tenant_id=scope.tenant_id,
            account_id=None,
        )

    agent_ref, environment_ref = (
        ref("agent", deployment.agent_id),
        ref("environment", deployment.environment_id),
    )
    from daimon.core.turn.selfhosted_tools import MintedMcpAuthorization, TurnTools

    config: HarnessConfig | None = None
    prepared_tools: TurnTools | None = None
    prepared_spec: AgentSpec | None = None
    minted: MintedMcpAuthorization | None = None

    def check(configured: ConfigRevision, authorized: Scope) -> None:
        if configured != revision or authorized != scope:
            raise ScopeViolation(
                request.thread_id, "self-hosted runtime differs from admitted turn"
            )

    async def plan(preparation: ProviderPreparationRequest) -> SessionSpec:
        nonlocal config, prepared_tools, prepared_spec
        if preparation.admission.backend_revision is None:
            raise AdmissionDenied(reason="backend_unsupported")
        check(preparation.admission.backend_revision, preparation.scope)
        agent = await agents.retrieve(scope, agent_ref)
        environment = await environments.retrieve(scope, environment_ref)
        extension = environment.spec.native_config
        if (
            agent.lifecycle != "active"
            or environment.lifecycle != "active"
            or agent.spec.model.provider != "selfhosted"
            or agent.spec.model.id != revision.model
            or extension is None
            or extension.namespace != "selfhosted.environment"
            or extension.version != 1
        ):
            raise AdmissionDenied(reason="backend_unsupported")
        config = HarnessConfig.model_validate(extension.value)
        from daimon.core.channel_skills import platform_skill_refs
        from daimon.core.stores.channel_skills import list_channel_skills
        from daimon.core.turn.selfhosted_tools import (
            build_turn_tools,
            load_turn_skill_bundles,
        )

        public_url, signing_secret = request.deps.mcp.public_url, request.deps.mcp.jwt_secret
        if public_url is None or signing_secret is None:
            raise AdmissionDenied(reason="backend_unsupported")
        platform_authored = platform_skill_refs(
            preparation.admission.agent, request.platform, request.deps.platform_skills
        )
        platform_refs = tuple(
            SkillRef(id=skill.skill_id, source="anthropic" if skill.type != "custom" else None)
            for skill in platform_authored
        )
        async with request.deps.sessionmaker() as session:
            channel_rows = await list_channel_skills(
                session,
                tenant_id=request.tenant_id,
                platform=request.platform,
                channel_id=revision.channel.channel_id,
            )
        names = tuple(
            dict.fromkeys(
                name for name in (agent.spec.name, preparation.admission.config.agent_name) if name
            )
        )
        refs = tuple(agent.spec.skills or ()) + platform_refs
        held = {skill.id for skill in refs}
        refs += tuple(
            SkillRef(id=row.skill_id, version=row.version)
            for row in channel_rows
            if row.skill_id not in held
            and (row.owner_agent_name is None or row.owner_agent_name in names)
        )
        bundles = await load_turn_skill_bundles(
            request.deps.defaults_root / "skills", refs, scope=scope
        )
        prepared_tools = await asyncio.to_thread(
            build_turn_tools,
            daimon_mcp_url=str(public_url),
            tenant_id=request.tenant_id,
            platform=request.platform,
            channel_id=revision.channel.channel_id,
            agent_names=names,
            skill_bundles=bundles,
            mcp_credential_bindings=settings.selfhosted.mcp_credential_bindings,
            # The driver compiles permissions from the original pinned spec;
            # this builder only resolves credential-free connection fields.
            mcp_servers=tuple(
                connection.model_copy(update={"tool_policy": {}})
                for connection in agent.spec.mcp_servers or ()
            ),
            agent_skills=agent.spec.skills or (),
            platform_skills=platform_refs,
            channel_skills=channel_rows,
        )
        prepared_spec = agent.spec
        return SessionSpec(
            agent=agent_ref,
            agent_revision=agent.revision,
            environment=environment_ref,
            config_revision=revision.local,
        )

    class ConfiguredTransport:
        async def request(
            self,
            method: str,
            path: str,
            *,
            session_id: str,
            body: Mapping[str, JsonValue] | None = None,
            mcp_authorization: str | None = None,
            deadline: datetime | None = None,
        ) -> JsonValue:
            nonlocal minted
            if config is None:
                raise AdmissionDenied(reason="backend_unsupported")
            # Check before GET as well as POST. The paid send additionally
            # commits its immutable tariff under the same durable wire turn id.
            require_selfhosted_price(billing, model)
            if method == "POST" and path == "/v0/turns":
                from daimon.core.turn.persistence import current_persistence
                from daimon.core.turn.selfhosted_tools import mint_turn_mcp_authorization
                from mux.contracts.ids import ResourceRef

                active = current_persistence.get()
                if active is None or body is None or not isinstance(body.get("turn_id"), str):
                    raise AdmissionDenied(reason="backend_unsupported")
                ref = ResourceRef(
                    id=session_id,
                    kind="session",
                    provider="selfhosted",
                    account_scope_id=scope.tenant_id,
                    tenant_id=scope.tenant_id,
                    account_id=scope.account_id,
                )
                active.check_session(scope, ref)
                await admit_selfhosted_usage(
                    sessionmaker=request.deps.sessionmaker,
                    tenant_id=request.tenant_id,
                    binding_id=active.binding.id,
                    session_ref=ref,
                    admission_key="selfhosted:turn:" + str(body["turn_id"]),
                    deployment=model,
                    billing=billing,
                )
                if minted is None:
                    signing_secret = request.deps.mcp.jwt_secret
                    if signing_secret is None:
                        raise AdmissionDenied(reason="backend_unsupported")
                    minted = await mint_turn_mcp_authorization(
                        request.deps.sessionmaker,
                        scope=scope,
                        agent=agent_ref,
                        agents=agents,
                        secret=signing_secret.get_secret_value().encode(),
                        now=request.now(),
                        deadline_s=min(
                            deployment.deadline_s,
                            (request.deadline - request.now()).total_seconds(),
                        ),
                        slack_turn_context_id=request.admission.slack_turn_context_id,
                    )

            async def token_provider() -> str:
                if config is None:
                    raise AdmissionDenied(reason="backend_unsupported")
                if config.auth_mode == "bearer_env":
                    token = os.environ.get(config.auth_ref)
                    if not token or not token.strip():
                        raise AdmissionDenied(reason="backend_unsupported")
                    return token
                from azure.identity.aio import DefaultAzureCredential

                try:
                    async with DefaultAzureCredential() as credential:
                        audience = config.auth_ref.rstrip("/")
                        token_scope = (
                            audience if audience.endswith("/.default") else audience + "/.default"
                        )
                        return (await credential.get_token(token_scope)).token
                except Exception:
                    raise AdmissionDenied(reason="backend_unsupported") from None

            async with httpx.AsyncClient(timeout=30) as client:
                if config.transport == "foundry_invocations":
                    if config.project_endpoint is None or config.agent_name is None:
                        raise AdmissionDenied(reason="backend_unsupported")
                    transport = FoundryInvocationsTransport(
                        client,
                        project_endpoint=config.project_endpoint,
                        agent_name=config.agent_name,
                        token_provider=token_provider,
                    )
                else:
                    transport = DirectTransport(
                        client,
                        base_url=config.harness_url,
                        token_provider=token_provider,
                    )
                return await transport.request(
                    method,
                    path,
                    session_id=session_id,
                    body=body,
                    deadline=min(request.deadline, deadline)
                    if deadline is not None
                    else request.deadline,
                    mcp_authorization=minted.header.get_secret_value()
                    if method == "POST" and path == "/v0/turns" and minted is not None
                    else None,
                )

    def transport(configured: ConfigRevision, authorized: Scope) -> ConfiguredTransport:
        check(configured, authorized)
        return ConfiguredTransport()

    def authorize(authorized: Scope, kind: str, id_: str | None) -> bool:
        return authorized == scope and (
            kind == "session"
            or (kind, id_)
            in {
                ("agent", deployment.agent_id),
                ("environment", deployment.environment_id),
            }
        )

    async def tools(authorized: Scope, spec: AgentSpec) -> TurnTools:
        if authorized != scope or prepared_tools is None or spec != prepared_spec:
            raise ScopeViolation(agent_ref.id, "self-hosted tools differ from admitted agent")
        return prepared_tools

    from mux.contracts.usage import UsageObservation

    async def record(observation: UsageObservation, binding_id: str) -> bool:
        return await record_selfhosted_usage(
            sessionmaker=request.deps.sessionmaker,
            binding_id=binding_id,
            observation=observation,
            tenant_id=request.tenant_id,
            platform_user_id=request.external_user_id,
            admission_key="selfhosted:turn:" + observation.turn_id
            if observation.turn_id is not None
            else None,
            markup=request.deps.markup,
            channel_id=revision.channel.channel_id,
            infrastructure_usd=deployment.infrastructure_usd,
        )

    async def finalize() -> None:
        nonlocal minted
        if minted is not None:
            from daimon.core.stores.mcp_tokens import revoke_mcp_token

            async with request.deps.sessionmaker() as session, session.begin():
                await revoke_mcp_token(session, jti=minted.jti, now=request.now())
            minted = None

    def skipped_notice() -> str:
        if prepared_tools is None or not prepared_tools.skipped:
            return ""
        details = ", ".join(
            f"{item.name} ({item.reason.replace('_', ' ')})" for item in prepared_tools.skipped
        )
        return "Daimon: some skills are unavailable in this thread: " + details + "."

    return SelfHostedHostRuntime(
        transport_factory=transport,
        journal=None,
        usage_store=None,
        account_scope_id=scope.tenant_id,
        record_usage=record,
        finalize=finalize,
        skipped_notice=skipped_notice,
        tools_resolver=tools,
        price=price,
        infrastructure_usd=deployment.infrastructure_usd,
        session_plan=plan,
        agents=agents,
        environments=environments,
        authorization=authorize,
        model=model,
        max_tool_calls=deployment.max_tool_calls,
        deadline_s=min(deployment.deadline_s, (request.deadline - request.now()).total_seconds()),
    )


def register_runtime() -> None:
    from daimon.core.config import load_billing_settings
    from daimon.core.selfhosted_usage import selfhosted_prices
    from daimon.core.turn.runtimes import register_channel_runtime

    register_host()
    register_channel_runtime(
        PROFILE,
        build_selfhosted_runtime,
        authorizations=authorized_deployments,
        tariffs=lambda: selfhosted_prices(load_billing_settings().selfhosted),
        fallback=True,
    )
