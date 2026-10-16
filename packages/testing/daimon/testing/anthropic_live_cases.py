"""Actual scoped A7 host turns and F2 default-capability ports; no scripted native replies."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from contextlib import nullcontext
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import cast

import yaml
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.driver import run_turn
from daimon.core.turn.persistence import TurnPersistence
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import extract_final_response
from daimon.core.turn.termination import TerminationReason
from daimon.testing.anthropic_live import MODEL, LiveContext, LiveRefused
from daimon.testing.conformance.default_capability import (
    DEFAULT_SKILLS,
    DEFAULT_TOOLS,
    DefaultCapabilityAdapter,
    DefaultManifest,
    NamedSkill,
)
from daimon.testing.conformance.recording import Recorder
from daimon.testing.conformance.runner import run_default_capability
from daimon.testing.conformance.usage_settlement import AnthropicSessionSpend
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.f2_mcp_binding import AgentMCPBinding
from daimon.testing.f2_native_bounds import install_f2_bounds, verify_tools
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.actions import UserMessage
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import (
    Agent,
    AgentSpec,
    CredentialBinding,
    MCPConnection,
    ProviderBinding,
    Session,
    SessionSpec,
    SkillUpload,
    SkillUploadFile,
    ToolSpec,
)
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.drivers.anthropic.resources.vaults import Vaults
from mux.drivers.anthropic.sessions_lifecycle import AnthropicSessions, SessionReads
from mux.drivers.anthropic.usage import UsageWalk
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

A7_PROMPT = "Reply with exactly a7-paired-probe-ok. Do not use tools or delegate."


async def collect_usage(context: LiveContext, ref: ResourceRef) -> None:
    """Check accepted ID and served model before the shared normalizer sees spans."""
    if ref.id not in context.transport.session_ids:
        raise LiveRefused("usage fetch is outside the accepted session inventory")
    scope = context.admission.binding.scope
    reads = context.driver.extension(SessionReads, namespace="anthropic.session_reads", version=1)
    native = await reads.read_native(scope, ref)
    agent = native.get("agent")
    model = agent.get("model") if isinstance(agent, dict) else None
    if isinstance(model, dict):
        model = model.get("id")
    if native.get("id") != ref.id or model != MODEL:
        raise LiveRefused("served session identity/model is absent or foreign")
    context.transport.monitor.verified_models[ref.id] = MODEL
    walk = context.driver.extension(UsageWalk, namespace="anthropic.usage_walk", version=1)
    observations = tuple([obs async for obs in walk.model_requests(scope, ref, model_id=MODEL)])
    # SDK wall time/stats and a final idle are NOT a complete billed-interval walk.
    # Preserve request lower bounds; N8 reconciles missing runtime/TTL/finality.
    context.evidence.append(
        AnthropicSessionSpend(ref, observations, runtime=None, usage_complete=False)
    )


async def a7_pair(context: LiveContext) -> Mapping[str, object]:
    binding = context.admission.binding
    if binding.agent is None or binding.database_url is None:
        raise LiveRefused("A7 disposable bindings are missing")
    engine = create_async_engine(binding.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    require_owned_sessionmaker(sessions)
    store = PostgresStateStore(sessions)
    outcomes = context.completed_legs
    try:
        agent = await context.driver.agents.retrieve(binding.scope, binding.agent)
        if (
            agent.spec.model.id != MODEL
            or agent.spec.mcp_servers
            or agent.spec.skills
            or any(
                ext.namespace == "anthropic.agent_tools" and ext.value.get("tools")
                for ext in agent.spec.extensions or ()
            )
            or any(ext.namespace == "anthropic.multiagent" for ext in agent.spec.extensions or ())
        ):
            raise LiveRefused("A7 agent must be Haiku5.5 with no skills/MCP/delegation")
        for path in ("legacy", "mux"):
            session = await context.driver.sessions.create(
                binding.scope,
                SessionSpec(
                    agent=agent.ref,
                    agent_revision=agent.revision,
                    environment=binding.environment,
                    config_revision=0,
                ),
                key=f"a7-{path}-{context.reservation.receipt.run_id}",
            )
            ref = session.ref
            persisted = ProviderBinding(
                id=f"a7:{context.reservation.receipt.run_id}:{path}",
                thread=ThreadRef(
                    channel=ChannelRef(
                        tenant_id=binding.scope.tenant_id,
                        platform="slack",
                        channel_id="a7-disposable",
                    ),
                    thread_id=ref.id,
                ),
                provider="anthropic",
                profile="anthropic.managed_agents",
                generation=1,
                config_revision=0,
                native_refs={"session": ref.id},
                legacy_account_id=binding.scope.account_id,
            )
            await store.put_binding(persisted, expected_generation=0)
            persistence = TurnPersistence(
                store, persisted, binding.scope, operation_key=f"a7:{ref.id}"
            )
            lifecycle = RecordingLifecycle()
            started = time.perf_counter()
            with persistence.activate() if path == "mux" else nullcontext():
                state = await run_turn(
                    anthropic=context.client,
                    session_id=ref.id,
                    session_ref=ref,
                    scope=binding.scope,
                    user_message=A7_PROMPT,
                    lifecycle=lifecycle,
                    cancel=asyncio.Event(),
                    billing=BillingExempt(reason="headless-unrecorded"),
                    path=path,
                    render_interval_s=3600,
                )
            context.retain_completed_leg(
                {
                    "path": path,
                    "session_id": ref.id,
                    "elapsed_ms": (time.perf_counter() - started) * 1000,
                    "termination": state.termination.value if state.termination else None,
                    "final_text": extract_final_response(state.content),
                    "journal_records": len(await store.read_events(ref.id)),
                }
            )
            operation = await store.get_operation(binding.scope, f"a7:{ref.id}:send:0")
            if path == "mux" and (
                operation is None or operation.operation.status not in {"accepted", "processed"}
            ):
                raise LiveRefused("mux native send did not retain its accepted claim")
            await collect_usage(context, ref)
            # Finish the first leg before starting the second; no fresh retry on any failure.
            await context.driver.session_admin.archive(binding.scope, ref, key=f"a7-{path}-archive")
            if (
                state.termination != TerminationReason.COMPLETED
                or extract_final_response(state.content).strip() != "a7-paired-probe-ok"
            ):
                return {"status": "fail", "legs": outcomes}
        return {
            "status": "pass",
            "legs": outcomes,
            "quiet_host_latency_certificate": False,
            "full_33_recipe_certificate": False,
        }
    finally:
        await engine.dispose()


def default_manifest(root: Path, mcp_url: str) -> DefaultManifest:
    authored = yaml.safe_load((root / "defaults/agents/daimon.yaml").read_text())
    skills: list[NamedSkill] = []
    if tuple(ref["skill_id"] for ref in authored["skills"]) != DEFAULT_SKILLS:
        raise LiveRefused("authored default skill inventory changed")
    for name in DEFAULT_SKILLS:
        directory = root / "defaults/skills" / name
        files = tuple(
            SkillUploadFile(
                path=f"{name}/{path.relative_to(directory).as_posix()}",
                content=path.read_bytes(),
                media_type="application/octet-stream",
            )
            for path in sorted(directory.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        )
        skills.append(NamedSkill(name, SkillUpload(files=files)))
    return DefaultManifest(
        name=authored["name"],
        system=authored["system"],
        skills=tuple(skills),
        builtin_tools=DEFAULT_TOOLS,
        mcp=MCPConnection(name="daimon-mcp", url=mcp_url),
    )


def agent_spec(raw: Mapping[str, object]) -> AgentSpec:
    """Independent upstream tool mapping, not the submitted neutral spec."""
    verify_tools(raw)
    tools: list[ToolSpec] = []
    for tool in cast(list[dict[str, object]], raw["tools"]):
        if tool["type"] == "agent_toolset_20260401":
            for config in cast(list[dict[str, object]], tool.get("configs", [])):
                if config.get("enabled", True):
                    tools.append(ToolSpec(kind="builtin", name=str(config["name"])))
        elif tool["type"] == "mcp_toolset":
            tools.append(ToolSpec(kind="mcp_toolset", name=str(tool["mcp_server_name"])))
        else:
            raise LiveRefused("unexpected paid/custom native tool")
    model = raw["model"]
    model_id = cast(dict[str, object], model).get("id") if isinstance(model, dict) else model
    return AgentSpec.model_validate(
        {
            "name": raw["name"],
            "system": raw["system"],
            "model": {"provider": "anthropic", "id": model_id},
            "tools": tools,
            "mcp_servers": [
                {"name": item["name"], "url": item["url"]}
                for item in cast(list[dict[str, object]], raw["mcp_servers"])
            ],
            "skills": [
                {"id": item["skill_id"], "version": item["version"], "source": item["type"]}
                for item in cast(list[dict[str, object]], raw["skills"])
            ],
        }
    )


class NativeCapabilityEvidence:
    def __init__(self, context: LiveContext) -> None:
        self.context = context

    @property
    def skill_uploads(self) -> tuple[SkillUpload, ...]:
        uploads: list[SkillUpload] = []
        for content_type, body in self.context.transport.skill_bodies:
            message = BytesParser(policy=default).parsebytes(
                f"Content-Type: {content_type}\r\n\r\n".encode() + body
            )
            files = tuple(
                SkillUploadFile(
                    path=str(part.get_filename()),
                    content=cast(bytes, part.get_payload(decode=True)),
                    media_type=part.get_content_type(),
                )
                for part in message.iter_parts()
                if part.get_filename() is not None
            )
            uploads.append(SkillUpload(files=files))
        return tuple(uploads)

    @property
    def deployed_agent(self) -> AgentSpec:
        body = self.context.transport.agent_body
        if body is None:
            raise LiveRefused("actual native agent deployment was not observed")
        return agent_spec(body)

    def agent_spec(self, agent: Agent) -> AgentSpec:
        if not isinstance(agent.native, dict):
            raise LiveRefused("independent native agent readback is missing")
        return agent_spec(agent.native)

    def assert_consumed(self) -> None:
        if len(self.context.transport.session_ids) != 1 or self.context.transport.user_sends != 1:
            raise LiveRefused("F2 must use one session and one root turn")


class VaultSessions(AnthropicSessions):
    def __init__(
        self, context: LiveContext, vault: ResourceRef, credential: AgentMCPBinding, vaults: Vaults
    ) -> None:
        self.context = context
        self.credential = credential
        self.vaults = vaults
        self.vault = vault
        super().__init__(
            context.client,
            context.admission.binding.account_scope_id,
            context.transport.authorization,
            archive=context.driver.session_admin,
        )

    async def create(
        self, scope: Scope, spec: SessionSpec, *, key: str, initial: UserMessage | None = None
    ) -> Session:
        # run_default_capability has acknowledged and independently read back
        # this exact agent before it asks the session owner to create anything.
        await self.credential.bind(spec.agent)
        await self.vaults.put(
            scope,
            self.vault,
            CredentialBinding(
                name="daimon-mcp",
                kind="static_bearer",
                credential_ref="host:f2-owned-mcp",
                mcp_server_url=self.context.admission.binding.mcp_url,
            ),
            expected=None,
            key="f2-mcp",
        )
        ext = ExtensionConfig(
            namespace="anthropic.session_create", version=1, value={"vault_ids": [self.vault.id]}
        )
        mounted = spec.model_copy(update={"extensions": {ext.namespace: ext}})
        return await super().create(scope, mounted, key=key, initial=initial)


async def f2_default(context: LiveContext) -> Mapping[str, object]:
    binding = context.admission.binding
    if (
        binding.mcp_url is None
        or binding.database_url is None
        or binding.mcp_signing_secret_file is None
    ):
        raise LiveRefused("after-ACK MCP binding authority is missing")
    credential = AgentMCPBinding(context)
    install_f2_bounds(context.client)
    root = Path(__file__).resolve().parents[4]
    manifest = default_manifest(root, binding.mcp_url)
    secret_ref = "host:f2-owned-mcp"

    def resolve(scope: Scope, reference: str) -> str:
        if scope != binding.scope or reference != secret_ref:
            raise LiveRefused("MCP credential reference is outside the admitted binding")
        if credential.token is None:
            raise LiveRefused("F2 MCP secret requested before its agent ACK binding")
        recorder.secrets = context.redaction_secrets
        return credential.token

    native = AnthropicManagedAgents(
        context.client,
        account_scope_id=binding.account_scope_id,
        authorization=context.transport.authorization,
        secrets=resolve,
    )
    vaults = native.extension(Vaults, namespace="anthropic.vaults", version=1)
    vault = await vaults.create(
        binding.scope, f"f2-{context.reservation.receipt.run_id}", key="f2-vault"
    )

    async def retire() -> None:
        try:
            for kind, id_ in sorted(context.transport.authorization.created):
                if kind == "agent":
                    await native.agents.archive(
                        binding.scope, context.ref(id_, kind), key="f2-agent-cleanup"
                    )
                elif kind == "skill":
                    await native.skills.delete(binding.scope, id_, key="f2-skill-cleanup")
            await vaults.archive(binding.scope, vault.ref, key="f2-vault-cleanup")
        finally:
            await credential.close()

    context.retire = retire
    native = AnthropicManagedAgents(
        context.client,
        account_scope_id=binding.account_scope_id,
        authorization=context.transport.authorization,
        secrets=resolve,
        sessions=VaultSessions(context, vault.ref, credential, vaults),
    )
    context.driver = native
    # Include the dynamically minted bearer before any recorded turn; the same
    # Recorder is passed through provisioning and session creation.
    recorder = Recorder(secrets=context.redaction_secrets)

    result = await run_default_capability(
        manifest,
        DefaultCapabilityAdapter(
            native,
            binding.scope,
            ModelRef(provider="anthropic", id=MODEL),
            binding.environment,
            NativeCapabilityEvidence(context),
            atomic_revision_pin=True,
        ),
        recorder=recorder,
    )
    if context.transport.session_id is not None:
        await collect_usage(context, context.ref(context.transport.session_id, "session"))
    recorder.save(
        context.admission.output / "f2-normalized.json",
        fixture_id="F1",
        provider="anthropic",
        model=MODEL,
        complete=result.status == "pass",
    )
    return {"status": result.status, "evidence": result.evidence}
