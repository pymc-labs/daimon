"""Explicit self-hosted manifests; only the admitted tenant can resolve credentials."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.prepare import ProviderPreparationRequest
from daimon.core.turn.selfhosted_codec import PROFILE
from daimon.core.turn.selfhosted_host import SelfHostedHostRuntime, register_host
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ResourceRef, Scope, SkillRef
from mux.contracts.resources import AgentSpec, SessionSpec
from mux.drivers.selfhosted.transport import DirectTransport, FoundryInvocationsTransport
from mux.errors import ProviderError, ScopeViolation
from mux.profiles.anthropic import MANAGED_AGENTS
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
)

if TYPE_CHECKING:
    from daimon.core.config import SelfHostedSettings
    from daimon.core.tool_safety import ToolSafetyPolicy
    from mux.drivers.selfhosted.config import HarnessConfig

_ROWS = TypeAdapter(list[dict[str, JsonValue]])
_MAX_BYTES = 1_048_576


class EntraTokenProvider:
    """One credential and expiring token for an admitted deployment transport."""

    def __init__(self, audience: str) -> None:
        from azure.core.credentials import AccessToken
        from azure.identity.aio import DefaultAzureCredential

        self._credential = DefaultAzureCredential()
        audience = audience.rstrip("/")
        self._scope = audience if audience.endswith("/.default") else audience + "/.default"
        self._token: AccessToken | None = None
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            try:
                if self._token is None or self._token.expires_on <= time.time() + 60:
                    self._token = await self._credential.get_token(self._scope)
                return self._token.token
            except Exception:
                raise ProviderError(
                    "transient_network", retryable=True, native_code="entra_token"
                ) from None

    async def close(self) -> None:
        await self._credential.close()


def validate_harness_auth(config: HarnessConfig, settings: SelfHostedSettings, tenant: str) -> None:
    """Validate origin, audience and tenant before resolving any credential."""
    bindings = {
        (binding.auth_mode, binding.name): binding for binding in settings.harness_auth_bindings
    }
    binding = bindings.get((config.auth_mode, config.auth_ref))
    parsed = urlsplit(config.project_endpoint or config.harness_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if (
        (
            config.auth_mode == "bearer_env"
            and not config.auth_ref.startswith("DAIMON_SELFHOSTED__HARNESS_TOKEN_")
        )
        or binding is None
        or origin != binding.origin
        or not (
            tenant in tuple(map(str, binding.tenants))
            or (settings.single_tenant and binding.tenants == ("*",))
        )
    ):
        raise AdmissionDenied(reason="backend_unsupported")


def host_tool_policy(
    safety: ToolSafetyPolicy,
    servers: tuple[str, ...],
    *,
    asks_before_publishing: bool,
    rule_tools: frozenset[str],
) -> dict[str, Literal["auto", "ask", "deny"]]:
    """Project host gates so every guarded invocation reaches host approval."""
    from daimon.core.tool_safety import CONFIRMED_DAIMON_TOOLS, PUBLISH_TOOLS

    policy: dict[str, Literal["auto", "ask", "deny"]] = {}
    if safety.enabled:
        policy.update({server + "/*": "ask" for server in servers if server != "daimon-mcp"})
        for key in safety.denied:
            policy[key if "/" in key else key + "/*"] = "deny"
    asked = set(CONFIRMED_DAIMON_TOOLS) | set(rule_tools)
    if asks_before_publishing:
        asked.update(PUBLISH_TOOLS)
    # A proxy is gated only when host policy needs to inspect the underlying tool.
    # With default settings, neutral proxy calls retain automatic execution.
    if safety.enabled or rule_tools or asks_before_publishing:
        asked.add("call_tool")
    for name in asked:
        policy.setdefault("daimon-mcp/" + name, "ask")
    return policy


def merge_tool_policy(
    original: JsonValue,
    host: Mapping[str, Literal["auto", "ask", "deny"]],
) -> dict[str, JsonValue]:
    """Combine effective permissions, including wildcard defaults, without widening."""
    if not isinstance(original, dict):
        raise ValueError("invalid driver tool policy")
    rank = {"auto": 0, "ask": 1, "deny": 2}

    def permission(policy: Mapping[str, JsonValue], key: str) -> str:
        value = policy.get(key, policy.get(key.split("/")[0] + "/*", "auto"))
        if not isinstance(value, str) or value not in rank:
            raise ValueError("invalid driver permission")
        return value

    return {
        key: max((permission(original, key), permission(host, key)), key=rank.__getitem__)
        for key in original.keys() | host.keys()
    }


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
    client: httpx.AsyncClient | None = None
    from mux.contracts.ids import Revision

    credential: EntraTokenProvider | None = None
    transport_impl: DirectTransport | None = None
    environment_pin: Revision | None = None
    active_turn: tuple[str, str] | None = None
    session_bindings: dict[str, str] = {}
    receipt_cursors: dict[str, int] = {}
    transport_high_water: dict[str, int] = {}
    history_floors: dict[tuple[str, str | None], int] = {}
    ended_turns: set[str] = set()
    agent_policy: dict[str, JsonValue] = {}
    host_policy: dict[str, Literal["auto", "ask", "deny"]] = {}
    token_deadline_s = min(
        deployment.deadline_s, (request.deadline - request.now()).total_seconds()
    )
    if token_deadline_s <= 0:
        raise ValueError("self-hosted MCP token requires a positive remaining turn deadline")

    def check(configured: ConfigRevision, authorized: Scope) -> None:
        if configured != revision or authorized != scope:
            raise ScopeViolation(
                request.thread_id, "self-hosted runtime differs from admitted turn"
            )

    async def plan(preparation: ProviderPreparationRequest) -> SessionSpec:
        nonlocal config, prepared_tools, prepared_spec, environment_pin, host_policy
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
        environment_pin = environment.revision
        config = HarnessConfig.model_validate(extension.value)
        validate_harness_auth(config, settings.selfhosted, scope.tenant_id)
        from mux.drivers.selfhosted.events import agent_tool_policy

        agent_policy.update(agent_tool_policy(agent.spec))
        host_policy = host_tool_policy(
            request.deps.tool_safety,
            tuple(connection.name for connection in agent.spec.mcp_servers or ()),
            asks_before_publishing=preparation.admission.asks_before_publishing,
            rule_tools=preparation.admission.daimon_rule_tools,
        )
        from daimon.core.channel_skills import platform_skill_refs
        from daimon.core.stores.channel_skills import list_channel_skills
        from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog
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
            SkillRef(
                id=skill.skill_id,
                source=MANAGED_AGENTS.provider if skill.type != "custom" else None,
            )
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
            request.deps.defaults_root / "skills",
            refs,
            scope=scope,
            catalog=PostgresNeutralCatalog(
                request.deps.sessionmaker, provider="selfhosted", account_scope_id=scope.tenant_id
            ),
            platform=request.platform,
            channel_id=revision.channel.channel_id,
            agent_names=names,
            channel_skills=channel_rows,
            agent=agent,
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
            single_tenant=settings.selfhosted.single_tenant,
            mcp_allowed_http_hosts=settings.selfhosted.mcp_allowed_http_hosts,
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
            after_seq: int,
            body: Mapping[str, JsonValue] | None = None,
            mcp_authorization: str | None = None,
            deadline: datetime | None = None,
        ) -> JsonValue:
            nonlocal minted, client, credential, transport_impl, active_turn
            if config is None:
                raise AdmissionDenied(reason="backend_unsupported")
            environment = await environments.retrieve(scope, environment_ref)
            if environment.lifecycle != "active" or environment.revision != environment_pin:
                raise ScopeViolation(
                    environment_ref.id, "self-hosted environment changed before transport"
                )
            validate_harness_auth(config, settings.selfhosted, scope.tenant_id)
            # Check before GET as well as POST. The paid send additionally
            # commits its immutable tariff under the same durable wire turn id.
            require_selfhosted_price(billing, model)
            if method == "POST" and urlsplit(path).path == "/v0/turns":
                if body is not None:
                    body = dict(body)
                    original_policy = body.get("tool_policy", {})
                    if not isinstance(original_policy, dict):
                        raise AdmissionDenied(reason="backend_unsupported")
                    agent_policy.update(original_policy)
                    body["tool_policy"] = merge_tool_policy(
                        body.get("tool_policy", {}), host_policy
                    )
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
                active_turn = session_id, str(body["turn_id"])
                session_bindings[session_id] = active.binding.id
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
                        deadline_s=token_deadline_s,
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
                nonlocal credential
                if credential is None:
                    credential = EntraTokenProvider(config.auth_ref)
                return await credential()

            if transport_impl is None:
                client = httpx.AsyncClient(timeout=30)
                if config.transport == "foundry_invocations":
                    if config.project_endpoint is None or config.agent_name is None:
                        raise AdmissionDenied(reason="backend_unsupported")
                    transport_impl = FoundryInvocationsTransport(
                        client,
                        project_endpoint=config.project_endpoint,
                        agent_name=config.agent_name,
                        token_provider=token_provider,
                    )
                else:
                    transport_impl = DirectTransport(
                        client,
                        base_url=config.harness_url,
                        token_provider=token_provider,
                        session_pool=config.session_pool,
                    )
            # Cleanup and reconciliation need a small independent budget after the host deadline.
            limit = (
                request.deadline
                if (method == "POST" and urlsplit(path).path == "/v0/turns")
                or request.deadline > datetime.now(UTC)
                else datetime.now(UTC) + timedelta(seconds=8)
            )
            after_seq = max(
                after_seq, await receipt_cursor(session_id), transport_high_water.get(session_id, 0)
            )
            transport_high_water[session_id] = after_seq
            parsed = urlsplit(path)
            if method == "GET" and parsed.path.endswith("/events"):
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                floor = await history_floor(session_id)
                original_cursor = int(query["after_seq"])
                replay_cursor = max(int(query.get("replay_after_seq", original_cursor)), floor)
                fence = max(original_cursor, after_seq, floor)
                query["after_seq"] = str(fence)
                if "replay_after_seq" in query or replay_cursor < fence:
                    query["replay_after_seq"] = str(replay_cursor)
                path = parsed.path + "?" + urlencode(query)
            result = await transport_impl.request(
                method,
                path,
                session_id=session_id,
                after_seq=after_seq,
                body=body,
                deadline=min(limit, deadline) if deadline is not None else limit,
                mcp_authorization=minted.header.get_secret_value()
                if method == "POST" and urlsplit(path).path == "/v0/turns" and minted is not None
                else None,
            )
            if method == "GET" and "/events?" in path:
                await reconcile_receipts(session_id, result)
            return result

    async def replay_binding(session_id: str) -> str | None:
        from daimon.core.turn.persistence import current_persistence

        active = current_persistence.get()
        binding_id = session_bindings.get(session_id)
        if binding_id is None and active is not None:
            binding_id = active.binding.id
        if binding_id is None:
            # Preparation may reconcile before entering the turn persistence shell.
            from daimon.core.stores.mux_state import PostgresStateStore
            from mux.contracts.ids import ThreadRef
            from mux.state.lease import Slot

            store = request.deps.state_store or PostgresStateStore(request.deps.sessionmaker)
            binding = await store.get_binding(
                Slot(
                    thread=ThreadRef(channel=revision.channel, thread_id=request.thread_id),
                    account_id=scope.account_id,
                )
            )
            if binding is None or binding.native_refs.get("session") != session_id:
                return None
            binding_id = binding.id
        session_bindings[session_id] = binding_id
        return binding_id

    async def receipt_cursor(session_id: str) -> int:
        from daimon.core.stores.selfhosted_usage import replay_cursor

        if session_id not in receipt_cursors:
            binding_id = await replay_binding(session_id)
            if binding_id is None:
                return 0
            async with request.deps.sessionmaker() as db:
                receipt_cursors[session_id] = await replay_cursor(
                    db, tenant_id=request.tenant_id, binding_id=binding_id
                )
        return receipt_cursors[session_id]

    async def history_floor(session_id: str) -> int:
        from daimon.core.turn.persistence import current_persistence

        active = current_persistence.get()
        operation = active.operation_key if active is not None else None
        key = (session_id, operation)
        if key not in history_floors:
            # Receipts can commit before the answer projection. Only the
            # saved projection or invocation baseline proves a replay floor.
            from daimon.core.stores.mux_state import PostgresStateStore

            store = request.deps.state_store or PostgresStateStore(request.deps.sessionmaker)
            projection = await store.projection(session_id)
            cursor = projection.cursor if projection is not None else ""
            floor = int(cursor) if cursor.isdecimal() else 0
            if active is not None:
                baseline = await active.store.get_operation(
                    active.scope, active.operation_key + ":baseline:0"
                )
                if baseline is not None:
                    saved = baseline.result.get("after")
                    if isinstance(saved, int):
                        floor = saved
            history_floors[key] = floor
        return history_floors[key]

    async def reconcile_receipts(session_id: str, raw: JsonValue) -> None:
        from daimon.core.stores.selfhosted_usage import advance_replay_cursor
        from mux.drivers.selfhosted import wire
        from mux.drivers.selfhosted.normalize import usage

        binding_id = await replay_binding(session_id)
        if binding_id is None:
            return
        after = await receipt_cursor(session_id)
        native = ResourceRef(
            id=session_id,
            kind="session",
            provider="selfhosted",
            account_scope_id=scope.tenant_id,
            tenant_id=scope.tenant_id,
            account_id=scope.account_id,
        )
        receipts: dict[str, UsageObservation] = {}
        events = wire.EventsResponse.model_validate(raw).events
        for event in events:
            if event.session_id != session_id:
                raise ScopeViolation(session_id, "foreign usage replay")
            if event.seq <= after:
                continue
            if event.type == "turn_ended":
                ended_turns.add(event.turn_id)
            if event.type in ("usage", "model_request_started") or (
                event.type == "error"
                and event.data.get("kind") in ("usage_missing", "usage_unavailable")
            ):
                value = usage(event, native)
                prior = receipts.get(value.id)
                if prior is None or prior.revision < value.revision:
                    receipts[value.id] = value
        # Include earlier turns: a deadline can end the host reader before the
        # harness durably reports usage. The original admission survives restart.
        for value in receipts.values():
            await record(value, binding_id)
        cursor = max((event.seq for event in events), default=after)
        if cursor > after:
            async with request.deps.sessionmaker() as db, db.begin():
                await advance_replay_cursor(
                    db, tenant_id=request.tenant_id, binding_id=binding_id, seq=cursor
                )
            receipt_cursors[session_id] = cursor

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
        nonlocal minted, client, credential, transport_impl
        try:
            if active_turn is not None:
                session_id, turn_id = active_turn
                from mux.drivers.selfhosted.events import segment

                cleanup = ConfiguredTransport()
                async with asyncio.timeout(8):
                    if turn_id not in ended_turns:
                        try:
                            await cleanup.request(
                                "POST",
                                f"/v0/turns/{segment(turn_id)}/cancel",
                                session_id=session_id,
                                after_seq=max(
                                    await receipt_cursor(session_id),
                                    transport_high_water.get(session_id, 0),
                                ),
                            )
                        except Exception:
                            logging.getLogger(__name__).warning(
                                "self-hosted cancellation unconfirmed"
                            )
                    await cleanup.request(
                        "GET",
                        f"/v0/sessions/{segment(session_id)}/events"
                        f"?after_seq={await receipt_cursor(session_id)}",
                        session_id=session_id,
                        after_seq=max(
                            await receipt_cursor(session_id),
                            transport_high_water.get(session_id, 0),
                        ),
                    )
        except Exception:
            logging.getLogger(__name__).warning("self-hosted cleanup pending reconciliation")
        try:
            if minted is not None:
                from daimon.core.stores.mcp_tokens import revoke_mcp_token

                async with request.deps.sessionmaker() as session, session.begin():
                    await revoke_mcp_token(session, jti=minted.jti, now=request.now())
                minted = None
        except Exception:
            logging.getLogger(__name__).warning("self-hosted MCP token revocation failed")
        for resource in (client, credential):
            if resource is not None:
                try:
                    if isinstance(resource, httpx.AsyncClient):
                        await resource.aclose()
                    else:
                        await resource.close()
                except Exception:
                    logging.getLogger(__name__).warning("self-hosted transport cleanup failed")
        client = credential = transport_impl = None

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
        history_cursor=lambda ref: history_floor(ref.id),
        explicit_ask=lambda name: (
            agent_policy.get(name, agent_policy.get(name.split("/")[0] + "/*", "auto")) == "ask"
        ),
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
