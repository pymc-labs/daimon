"""The production turn port: each item runs as a real turn in the eval tenant.

An item gets its own fork of the arm's agent, its own sandbox pod serving the worker's eval
environment, a channel naming both, the case's people as principals, its recorded memory and
context, and then turns through the same admit, bind and run path as a chat. The fork's MCP
tools are custom tools the worker runs (`runner.tools`); tokens minted in its turns carry its
id, so the world answers their calls.

Anthropic environment keys come only from its console, so the worker has one Anthropic eval
environment. Its executor takes whichever session the environment hands out, so items on it
run one at a time: each holds the environment from its first turn to its close. A Gemini item
makes an environment of its own (`runner.gemini`), so those run side by side."""

from __future__ import annotations

import asyncio
import hashlib
import shlex
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from typing import Any, Final, Literal

import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.authz import Subject
from daimon.core.channel_backend import channel_ref, set_channel_backend
from daimon.core.context_provider import (
    ContextProviders,
    ContextRequest,
    ContextResponse,
    ProviderConfig,
    estimate_tokens,
)
from daimon.core.defaults.ma_index import agent_id_in_tenant
from daimon.core.defaults.metadata import build_metadata
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.evals.chat.cards import EvalCardNotice, press_eval_card
from daimon.core.evals.chat.surface import EvalSurfaceLifecycle
from daimon.core.evals.eval_tenant import eval_tenant
from daimon.core.evals.format import Case, Usage
from daimon.core.evals.runner.gemini import GeminiArms
from daimon.core.evals.runner.item import TurnRequest
from daimon.core.evals.runner.tools import (
    Caller,
    JsonObject,
    McpService,
    custom_tools,
    daimon_catalogue,
    item_tools,
)
from daimon.core.evals.sandbox.ca import EvalCA
from daimon.core.evals.sandbox.runtime import (
    PodFile,
    PodHandle,
    PodLimits,
    PodSpec,
    SandboxRuntime,
    read_files,
    wait_ready,
)
from daimon.core.evals.world.dispatch import FakeWorld
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_auth import current_eval_item
from daimon.core.memory_resource import ensure_memory_store_and_mount
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import (
    archive_agent,
    create_agent,
    create_memory,
    create_owned_agent,
    create_owned_environment,
    retrieve_agent,
    update_environment,
)
from daimon.core.output_delivery import DeliverableFile, sweep_outputs
from daimon.core.output_runtime import SessionOutput
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores import access_policy, context_providers, identity, scoped_config_write
from daimon.core.stores.domain import Role
from daimon.core.stores.tenants import set_funding_mode
from daimon.core.tool_safety import DAIMON_SERVER_NAME, session_tools_for_policy
from daimon.core.turn.admission import admit, backend_facts
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.gemini import PROFILE as GEMINI_PROFILE
from daimon.core.turn.host_tools import host_tools
from daimon.core.turn.outcomes import observe_turn
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtimes import RuntimeFactory
from daimon.core.turn_hold import RequesterOrg
from daimon.core.turn_origin import render_turn_origin, turn_origin
from mux.contracts.config import BackendConfig
from mux.contracts.ids import ModelRef, Provider
from mux.contracts.resources import AgentSpec, EnvironmentSpec, ToolSpec
from mux.drivers.gemini import self_hosted_id
from mux.profiles import CONTROL_PLANE_HOSTS, get_profile

log = structlog.get_logger()

OUTPUTS_DIR: Final = "/mnt/session/outputs"
"""Where the agent saves files for its reply (the platform prompt names it)."""
_FORK_FIELDS: Final = ("description", "system", "tools", "skills")
_EVAL_CONTEXT: Final = "eval://case-context"
_ENVIRONMENT: Final = "eval-environment"
"""The Daimon name the worker gives its eval environment in the eval tenant."""
_SHARED: Final = "anthropic"
"""The backend whose items share the worker's console-made environment (`PodSettings`)."""
_GEMINI: Final = get_profile(GEMINI_PROFILE).provider
_BACKENDS: Final = frozenset({_SHARED, _GEMINI})
"""Backends whose live arms run here: their executors serve self-hosted sessions."""


@dataclass(frozen=True)
class PodSettings:
    """How an item's pod starts: D's runtime, image and CA, and where the world listens."""

    runtime: SandboxRuntime
    image: str
    ca: EvalCA
    world_url: str
    #: The worker's self-hosted eval environment; its key is in `executor_config`.
    environment_id: str
    executor_config: Mapping[str, str] = field(default_factory=dict[str, str])
    passthrough: frozenset[str] = frozenset()
    registries: Literal["passthrough", "deny"] = "deny"
    limits: PodLimits = PodLimits()


@dataclass
class _Item:
    tenant_id: uuid.UUID
    case: Case
    surface: EvalSurfaceLifecycle
    name: str
    agent_id: str
    world_token: str
    #: Whether it holds the worker's shared environment until it closes.
    shared: bool
    tools: list[JsonObject] = field(default_factory=list[JsonObject])
    pod: PodHandle | None = None
    #: Clean-up after the pod stops: the fork, a per-item environment.
    cleanup: list[Callable[[], Awaitable[None]]] = field(
        default_factory=list[Callable[[], Awaitable[None]]]
    )
    #: The item's own turn runtime (`runtimes.RuntimeFactory`); None: the deployment's.
    runtime: RuntimeFactory | None = None


class PodOutputs:
    """The pod's outputs folder as an `OutputRuntime`, for `sweep_outputs`."""

    settles = False

    def __init__(self, runtime: SandboxRuntime, pod: PodHandle) -> None:
        self.session_id, self._runtime, self._pod = pod.id, runtime, pod
        self._files: dict[str, DeliverableFile] = {}

    async def list_outputs(self) -> tuple[SessionOutput, ...]:
        files = await read_files(self._runtime, self._pod, OUTPUTS_DIR)
        self._files = {f.file_id: f for f in files}
        return tuple(SessionOutput(f.file_id, f.filename, f.mime_type, f.size_bytes) for f in files)

    async def read_output(self, output_id: str) -> bytes:
        return self._files[output_id].content

    async def delete_output(self, output_id: str) -> None:
        path = f"{OUTPUTS_DIR}/{output_id}"
        await self._runtime.run(self._pod, f"rm -f -- {shlex.quote(path)}", timeout_s=60)


class CaseContext:
    """The context provider of eval channels: each answers with its case's recorded context."""

    def __init__(self) -> None:
        self.by_channel: dict[str, str] = {}

    async def __call__(self, config: ProviderConfig, request: ContextRequest) -> ContextResponse:
        text = self.by_channel.get(request.channel_id or "")
        if not text:
            return ContextResponse()
        return ContextResponse(turn_fragment=text, estimated_tokens=estimate_tokens(text),
                               status="ok")  # fmt: skip


def _role(case: Case, person: str) -> Role:
    people = {p.id: p for p in case.surface.participants}
    found = people.get(person)
    return Role.ADMIN if found and found.role in ("owner", "admin") else Role.USER


class EvalTurns:
    """`TurnPort` on the production turn path. `deps` is the worker's own."""

    def __init__(self, deps: TurnDeps, world: FakeWorld, pods: PodSettings,
                 mcp: McpService | None = None,
                 gemini: GeminiArms | None = None) -> None:  # fmt: skip
        self.context = CaseContext()
        self.deps = replace(deps, context_providers=ContextProviders(provider=self.context))
        self.gemini = gemini or GeminiArms(deps.sessionmaker)
        self.world, self.pods = world, pods
        url, secret = deps.mcp.public_url, deps.mcp.jwt_secret
        self.mcp = mcp or (McpService(str(url), secret.get_secret_value().encode())
                           if url is not None and secret is not None else None)  # fmt: skip
        self._items: dict[uuid.UUID, _Item] = {}
        self._environment = asyncio.Lock()
        self._tagged = False

    async def raise_card(self, notice: EvalCardNotice) -> None:
        """A card a tool posted in an item's conversation: the script presses it."""
        item = self._items.get(notice.item_id)
        if item is not None:
            admin = _role(item.case, item.surface.script.requester) is Role.ADMIN
            await press_eval_card(self.deps, item.tenant_id, item.surface, notice, is_admin=admin)

    async def turn(self, request: TurnRequest) -> Usage:
        item = self._items.get(request.item_id) or await self._open(request)
        reset = current_eval_item.set(request.item_id)
        try:
            return await self._turn(item, request)
        finally:
            current_eval_item.reset(reset)

    async def close(self, item_id: uuid.UUID) -> None:
        item = self._items.pop(item_id, None)
        if item is None:
            return
        self.context.by_channel.pop(item.name, None)
        pod = item.pod
        steps = ([lambda: self.pods.runtime.stop(pod)] if pod is not None else []) + item.cleanup
        try:
            for step in steps:
                try:
                    await step()
                # Clean-up is best effort: `reap` removes pods left behind, archives can wait.
                except Exception:
                    log.warning("eval_item.cleanup_failed", item_id=str(item_id), exc_info=True)
        finally:
            if item.shared:
                self._environment.release()

    async def _open(self, request: TurnRequest) -> _Item:
        arm = request.arm
        if arm.live is None or arm.live.backend not in _BACKENDS:
            raise NotImplementedError(
                f"the eval runner runs live arms on {', '.join(sorted(_BACKENDS))}"
            )
        shared = arm.live.backend == _SHARED
        if shared:
            await self._environment.acquire()
        try:
            return await self._opened(request, shared)
        except BaseException:
            if request.item_id in self._items:
                await self.close(request.item_id)
            elif shared:
                self._environment.release()
            raise

    async def _opened(self, request: TurnRequest, shared: bool) -> _Item:
        deps, case, arm = self.deps, request.case, request.arm
        assert arm.live is not None
        tenant_id = await eval_tenant(deps.sessionmaker)
        scope = resource_scope(tenant_id=str(tenant_id))
        name = f"eval-{request.item_id.hex[:20]}"
        account = derive_guild_account_uuid(tenant_id)
        metadata = build_metadata(tenant_id=tenant_id, name=name, account_id=account)
        async with deps.sessionmaker.begin() as session:
            # The operator pays for eval turns; each run's cap bounds them.
            await set_funding_mode(session, tenant_id=tenant_id, funding_mode="operator_funded")
            accounts = {
                person.id: (await identity.get_or_create_platform_principal(
                    session, tenant_id=tenant_id, platform=case.surface.platform,
                    external_id=person.id,
                )).account_id
                for person in case.surface.participants
            }  # fmt: skip
        if shared and not self._tagged:
            # Console-made, so untagged: the channel names it like any environment.
            tag = build_metadata(tenant_id=tenant_id, name=_ENVIRONMENT, account_id=account)
            await update_environment(deps.anthropic, self.pods.environment_id,
                                     {"metadata": tag}, scope=scope,
                                     records=deps.sessionmaker)  # fmt: skip
            self._tagged = True
        source = await self._source(request)
        params: dict[str, Any] = source.model_dump(mode="json", include=set(_FORK_FIELDS))
        source_tools: list[dict[str, Any]] = params.get("tools") or []
        requester = accounts.get(request.surface.script.requester)
        daimon: tuple[JsonObject, ...] = ()
        if (
            self.mcp is not None
            and requester is not None
            and any(t.get("mcp_server_name") == DAIMON_SERVER_NAME for t in source_tools)
        ):
            daimon = await daimon_catalogue(deps.sessionmaker, self.mcp,
                                            Caller(tenant_id, requester, name))  # fmt: skip
        trusted = frozenset({DAIMON_SERVER_NAME})
        gated = session_tools_for_policy(deps.tool_safety, source_tools, trusted_servers=trusted)
        tools = custom_tools(gated or source_tools, daimon, case)
        if not shared:
            assert requester is not None, "the case's requester is a participant"
            item = await self._own(request, name, metadata, params.get("system"), tools,
                                   requester)  # fmt: skip
        else:
            agent = await create_agent(deps.anthropic, {
                **params, "name": name, "metadata": metadata, "purpose": "eval",
                "model": arm.live.model, "mcp_servers": [],
                # Provider-dialed MCP servers reach past the enclosure: the worker runs them.
                "tools": [t for t in source_tools if t.get("type") != "mcp_toolset"] + [
                    {k: v for k, v in t.items() if not k.startswith("_")} for t in tools],
            }, scope=scope, records=deps.sessionmaker)  # fmt: skip
            item = _Item(tenant_id, case, request.surface, name, agent.id, request.world_token,
                         True, tools)  # fmt: skip
            item.cleanup.append(partial(archive_agent, deps.anthropic, agent.id, scope=scope,
                                        records=deps.sessionmaker))  # fmt: skip
            self._items[request.item_id] = item
            executor = {**self.pods.executor_config,
                        "ANTHROPIC_ENVIRONMENT_ID": self.pods.environment_id}  # fmt: skip
            item.pod = await self._pod(request, _SHARED, executor)
        async with deps.sessionmaker.begin() as session:
            await scoped_config_write.set_fields(
                session, scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=name),
                tenant_id=tenant_id, agent_name=name,
                environment_name=_ENVIRONMENT if shared else name,
            )  # fmt: skip
            context = "\n\n".join(c.text for c in (case.agent_context and
                                                    case.agent_context.context) or [])  # fmt: skip
            if context:
                self.context.by_channel[name] = context
                await context_providers.set_provider(
                    session, tenant_id=tenant_id, channel_id=name, set_by="evals",
                    config=ProviderConfig(endpoint=_EVAL_CONTEXT, enabled=True),
                    actor=Subject(is_admin=True),
                )  # fmt: skip
        if shared:
            await self._seed_memory(item)
        return item

    async def _own(self, request: TurnRequest, name: str, metadata: Mapping[str, str],
                   system: str | None, tools: list[JsonObject],
                   requester: uuid.UUID) -> _Item:  # fmt: skip
        """A hosted arm's item: Daimon-owned fork and environment records (the provider
        holds neither), the channel on the arm's backend, and a self-hosted environment of
        the item's own for its pod."""
        deps, live = self.deps, request.arm.live
        assert live is not None
        tenant_id = await eval_tenant(deps.sessionmaker)
        # Owned records belong to an account: the requester's, whose turns run them.
        scope = resource_scope(tenant_id=str(tenant_id), account_id=str(requester))
        fork = await create_owned_agent(deps.anthropic, AgentSpec(
            name=name, model=ModelRef(provider=_GEMINI, id=live.model), system=system,
            metadata=metadata, purpose="eval",
        ), scope=scope, records=deps.sessionmaker)  # fmt: skip
        # The channel's name for the item's environment; the provider's is made below.
        await create_owned_environment(deps.anthropic, EnvironmentSpec(
            name=name, metadata=metadata), scope=scope, records=deps.sessionmaker)  # fmt: skip
        item = _Item(tenant_id, request.case, request.surface, name, fork.id,
                     request.world_token, False, tools)  # fmt: skip
        item.cleanup.append(partial(archive_agent, deps.anthropic, fork.id, scope=scope,
                                    records=deps.sessionmaker))  # fmt: skip
        self._items[request.item_id] = item
        channel = channel_ref(tenant_id, request.case.surface.platform, name)
        async with deps.sessionmaker.begin() as session:
            policy = await access_policy.load_access_policy(session, tenant_id=tenant_id)
            if _GEMINI not in policy.enabled_backends:
                # The eval workspace runs whichever backends its arms name.
                await access_policy.set_access_policy(session, tenant_id=tenant_id,
                    policy=policy.model_copy(update={"enabled_backends": (
                        *policy.enabled_backends, _GEMINI)}))  # fmt: skip
            revision = await set_channel_backend(session, channel, BackendConfig(
                backend=_GEMINI, profile=GEMINI_PROFILE, model=live.model,
            ))  # fmt: skip
        environment, key = await self.gemini.environment(scope, name)
        item.cleanup.append(partial(self.gemini.delete, environment))
        specs = [ToolSpec(kind="custom", name=t["name"], description=t["description"],
                          input_schema=t["input_schema"]) for t in tools]  # fmt: skip
        item.runtime = self.gemini.runtime(revision, name, system, specs, environment)
        item.pod = await self._pod(request, _GEMINI, {
            "GEMINI_ENVIRONMENT_ID": self_hosted_id(environment), "GEMINI_ENVIRONMENT_KEY": key,
        })  # fmt: skip
        return item

    async def _source(self, request: TurnRequest) -> BetaManagedAgentsAgent:
        """The arm's agent in the run's workspace; an arm naming none runs the default one."""
        ref = (request.arm.live and request.arm.live.agent) or "default"
        found = await agent_id_in_tenant(self.deps.anthropic, tenant_id=request.tenant_id,
                                         ref=ref, records=self.deps.sessionmaker)  # fmt: skip
        if found is None:
            raise LookupError(f"the arm's agent {ref!r} is not in the run's workspace")
        scope = resource_scope(tenant_id=str(request.tenant_id))
        return await retrieve_agent(self.deps.anthropic, found, scope=scope,
                                    records=self.deps.sessionmaker)  # fmt: skip

    async def _pod(self, request: TurnRequest, executor: Provider,
                   config: Mapping[str, str]) -> PodHandle:  # fmt: skip
        pods, case = self.pods, request.case
        world_item = await self.world.item(request.item_id)
        assert world_item is not None, "the item is running"
        files = [
            PodFile(f.path, await self.world.blob(world_item, f.blob.sha256),
                    int(f.mode, 8) if f.mode else 0o644)
            for f in case.world.files or [] if f.path.startswith("/workspace/")
        ]  # fmt: skip
        spec = PodSpec(
            item=world_item, image=pods.image, executor=executor, world_url=pods.world_url,
            world_token=request.world_token, ca=pods.ca, executor_config=config, files=files,
            env=(case.world.env and case.world.env.vars) or {},
            control_plane=CONTROL_PLANE_HOSTS[executor], passthrough=pods.passthrough,
            registries=pods.registries,
            clock=case.clock.now, limits=pods.limits,
        )  # fmt: skip
        pod = await pods.runtime.start(spec)
        await wait_ready(pods.runtime, pod)
        return pod

    async def _seed_memory(self, item: _Item) -> None:
        memory = (item.case.agent_context and item.case.agent_context.memory) or []
        if not memory:
            return
        deps = self.deps
        mount = await ensure_memory_store_and_mount(
            deps.anthropic, deps.sessionmaker, tenant_id=item.tenant_id, agent_name=item.name,
            agent_id=derive_agent_uuid(tenant_id=item.tenant_id, ma_agent_id=item.agent_id),
        )  # fmt: skip
        scope = resource_scope(tenant_id=str(item.tenant_id))
        for entry in memory:
            await create_memory(deps.anthropic, mount["memory_store_id"], entry.path,
                                entry.content, scope=scope)  # fmt: skip

    async def _turn(self, item: _Item, request: TurnRequest) -> Usage:
        deps, case = self.deps, item.case
        if item.runtime is not None:
            # The channel names the arm's backend; the item's runtime serves its fork.
            deps = replace(deps, channel_backends=True, turn_runtime_factory=item.runtime)
        platform = case.surface.platform
        requester = request.surface.script.requester
        role = _role(case, requester)
        with observe_turn(deps.sessionmaker, tenant_id=item.tenant_id, platform=platform,
                          channel_id=item.name, thread_id=item.name) as observation:  # fmt: skip
            admission = await admit(
                deps, tenant_id=item.tenant_id, platform=platform, external_user_id=requester,
                channel_id=item.name, thread_id=item.name, role=role, now=datetime.now(UTC),
                origin="chat", requester_org=RequesterOrg.HOST,
            )  # fmt: skip
            prepared = await bind_session(
                deps, admission, tenant_id=item.tenant_id, platform=platform,
                external_user_id=requester, thread_id=item.name,
                session_account_id=admission.account_id, reuse_existing=True,
            )  # fmt: skip
            async with turn_origin(
                deps.sessionmaker, tenant_id=item.tenant_id, account_id=admission.account_id,
                platform=platform, parent_channel_id=item.name, thread_id=item.name,
                responder_ma_agent_id=str(admission.agent.id),
                responder_name=admission.config.agent_name or admission.agent.name, role=role,
                sender_foreign=False, shared_channel=admission.shared_channel,
                message_text=request.text, platform_channel_id=item.name,
            ) as origin:  # fmt: skip
                controls = render_turn_origin(origin, **backend_facts(admission),
                                              effective_is_admin=admission.is_admin)  # fmt: skip
                message = f"{controls}\n\n{request.text}"

                async def reseed() -> str:
                    return message

                caller = Caller(item.tenant_id, admission.account_id, str(admission.agent.id))
                tools = item_tools(
                    deps.sessionmaker, self.mcp, caller, item_id=request.item_id,
                    world_token=item.world_token, tools=item.tools,
                ) if self.mcp and item.tools else None  # fmt: skip
                reset = host_tools.set(tools)
                try:
                    await run_prepared_turn(
                        deps, prepared, tenant_id=item.tenant_id, platform=platform,
                        thread_id=item.name, external_user_id=requester, user_message=message,
                        request_text=request.text, lifecycle=request.surface, cancel=request.cancel,
                        reseed_user_message=reseed, recovery_lifecycle=lambda _: request.surface,
                        confirm_write=request.surface.confirm,
                    )  # fmt: skip
                finally:
                    host_tools.reset(reset)
        if item.pod is not None:
            await sweep_outputs(PodOutputs(self.pods.runtime, item.pod),
                                post=lambda f: self._keep(item, f))  # fmt: skip
        state = request.surface.state
        cost = observation.billed_cost(Decimal(1))
        return Usage(
            input_tokens=state.usage_totals.input_tokens or None,
            output_tokens=state.usage_totals.output_tokens or None,
            cache_read_tokens=state.usage_totals.cache_read_input_tokens or None,
            cache_write_tokens=state.usage_totals.cache_creation_input_tokens or None,
            cost_usd=float(cost) if cost is not None else None,
        )

    async def _keep(self, item: _Item, file: DeliverableFile) -> None:
        """A delivered file: kept as a blob of the run's workspace and shown in the chat."""
        sha = hashlib.sha256(file.content).hexdigest()
        await self.world.keep_blob(item.tenant_id, sha, file.mime_type, file.content)
        item.surface.file(file.filename, file.mime_type, sha, len(file.content), OUTPUTS_DIR)
