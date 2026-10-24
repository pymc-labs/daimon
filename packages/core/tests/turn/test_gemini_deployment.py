"""Offline app-runtime opt-in, explicit tools and durable Gemini proofs."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import httpx
import pytest
from daimon.core import pricing
from daimon.core.channel_backend import check_backend
from daimon.core.fleet import FleetWorker
from daimon.core.neutral_resources import (
    NeutralResourceHost,
    ResolvedWorkerSpec,
    WorkerPermissions,
    resolve_worker_spec,
)
from daimon.core.pricing import ModelRates, ProviderPrice
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini import (
    PROFILE,
    GeminiJournal,
    GeminiUsageRuntime,
)
from daimon.core.turn.gemini_deployment import GeminiPlan, build_gemini_runtime
from daimon.core.turn.gemini_prepare import prepare_gemini_turn
from daimon.core.turn.gemini_state import SQLiteFlashAccounting, SQLiteGeminiStorage
from daimon.core.turn.prepare import ProviderPreparationRequest, bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.runtimes import build_channel_runtime
from daimon.core.turn_hold import TurnHold
from daimon.testing.factories import make_tenant
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.actions import UserMessage
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.events import TextPart
from mux.contracts.ids import ModelRef, ResourceRef, Scope
from mux.contracts.resources import (
    Agent,
    AgentPatch,
    Artifact,
    EnvironmentSpec,
    MCPConnection,
    NetworkPolicy,
    ResourceBinding,
    ToolSpec,
    WorkspaceSource,
)
from mux.drivers.gemini.catalog_transport import AgentCatalogTransport, SDKAgentCatalogTransport
from mux.drivers.gemini.core import compile_agent
from mux.drivers.gemini.flash import FLASH_PRIMARY, FlashAttempt
from mux.drivers.gemini.storage import ArtifactRecord, Storage
from mux.drivers.gemini.transport import NO_TOOLS_TOOL, Transport
from mux.errors import ScopeViolation
from mux.profiles import selectable_models
from mux.profiles.gemini import ARGON_MODEL
from mux.testing.drivers.gemini.fake import MemoryStorage
from mux.testing.isolated_sdk import build_gemini_client
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..test_fleet_permissions import NOW, POLICY, parent
from ..test_native_tariffs import fixed_source_date as fixed_source_date
from .test_agent_native_runtime import claimed_worker_request as claimed_worker_request
from .test_agent_native_runtime import clone_source as clone_source
from .test_agent_native_runtime import explicit_worker_request as explicit_worker_request
from .test_gemini_codec import TrackedTransport, interaction
from .test_native_counterpart import Owned
from .test_native_counterpart import clean_sdk_environment as clean_sdk_environment
from .test_native_counterpart import owned as owned
from .test_openai_deployment import application as application
from .test_openai_deployment import manifest
from .test_openai_worker_plan import claimed_openai_worker as claimed_openai_worker


@pytest.fixture
async def selected(application: ProviderPreparationRequest) -> ProviderPreparationRequest:
    revision = application.admission.backend_revision
    assert revision is not None
    config = ConfigRevision.create(
        revision.channel,
        1,
        resolve_default(BackendConfig(backend="gemini", profile=PROFILE, model=FLASH_PRIMARY)),
    )
    return replace(
        application,
        admission=replace(
            application.admission, backend_revision=config, backend=check_backend(config)
        ),
        deps=replace(
            application.deps, state_store=PostgresStateStore(application.deps.sessionmaker)
        ),
    )


def entry(selected: ProviderPreparationRequest, tmp_path: Path) -> dict[str, JsonValue]:
    config = selected.admission.backend_revision
    assert config is not None
    return {
        "profile": PROFILE,
        "tenant_id": selected.scope.tenant_id,
        "platform": config.channel.platform,
        "channel_id": config.channel.channel_id,
        "account_id": selected.scope.account_id,
        "config_digest": config.digest,
        "project": "actual-operator-project",
        "api_key_env": "N4_OFFLINE_GEMINI_KEY",
        "records_path": str(tmp_path / "records.sqlite"),
        "holds_path": str(tmp_path / "holds.sqlite"),
        "hold_usd": "0.05",
        "cap_usd": "0.25",
        "agent_name": "explicit-gemini",
        "tools": [],
        "environment": {"name": "explicit-inline"},
    }


@pytest.fixture
def worker_target(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> tuple[GeminiPlan, ConfigRevision, ResolvedWorkerSpec]:
    root = parent("gemini")
    variant = getattr(request, "param", None)
    if variant == "argon":

        class InArgonWindow(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
                return datetime(2026, 11, 30, tzinfo=UTC)

        # Argon is tariff-gated; its intro price is current through 2026-11-30.
        monkeypatch.setattr(pricing, "datetime", InArgonWindow)
    if variant in ("cheap", "argon"):
        model = ModelRef(
            provider="gemini",
            id="gemini-3.5-flash-lite" if variant == "cheap" else "gemini-4-argon",
        )
        selection = resolve_default(
            BackendConfig(backend="gemini", profile=PROFILE, model=model.id)
        )
        root = replace(
            root,
            spec=root.spec.model_copy(update={"model": model}),
            models=(*root.models, model),
            backend_grants=(*root.backend_grants, selection),
        )
    worker = resolve_worker_spec(
        root,
        root.spec.model_copy(
            update={
                "declared_drops": (
                    "memory",
                    "sandbox",
                    "skills",
                    "mcp",
                    "tools",
                    "files",
                    "repository",
                )
            }
        ),
        policy=POLICY,
        now=NOW,
        backend=BackendConfig(backend="gemini", profile=PROFILE, model=root.spec.model.id),
    )
    revision = ConfigRevision.create(root.fleet.root.channel, 1, worker.backend)
    plan = GeminiPlan(
        profile=PROFILE,
        tenant_id=root.fleet.scope.tenant_id,
        platform=root.fleet.root.channel.platform,
        channel_id=root.fleet.root.channel.channel_id,
        account_id=root.fleet.scope.account_id,
        config_digest=revision.digest,
        project="project",
        api_key_env="OFFLINE_WORKER_KEY",
        records_path=tmp_path / "records.sqlite",
        holds_path=tmp_path / "holds.sqlite",
        hold_usd=Decimal("0.05"),
        cap_usd=Decimal("0.25"),
        agent_name=worker.spec.name,
        system=worker.spec.system,
        tools=(),
        environment=EnvironmentSpec(name="explicit-no-execution"),
    )
    return plan, revision, worker


@pytest.mark.parametrize("worker_target", ("cheap",), indirect=True)
def test_cheap_worker_model_is_canonical_and_admitted_before_compilation(
    worker_target: tuple[GeminiPlan, ConfigRevision, ResolvedWorkerSpec],
) -> None:
    from mux.profiles.gemini import WORKER_MAX_TOTAL_TOKENS

    plan, revision, worker = worker_target
    check_backend(revision)
    assert worker.backend.model == revision.model == "gemini-3.5-flash-lite"
    assert worker.spec.model == ModelRef(provider="gemini", id=revision.model)
    deployment = plan.worker_deployment(worker, revision)
    assert deployment.agent == worker.spec
    assert deployment.max_total_tokens == WORKER_MAX_TOTAL_TOKENS == 65_536
    assert compile_agent(deployment.agent, max_total_tokens=deployment.max_total_tokens)[
        "agent_config"
    ] == {"type": "antigravity", "model": "gemini-3.5-flash-lite", "max_total_tokens": 65_536}


def test_the_unbounded_worker_token_cap_fits_the_worker_hold() -> None:
    """10 min of work needs more than 4096 tokens; 65,536 still fits the $0.49 hold.

    Every token of an unbounded turn priced at the highest tariff of any model the
    unbounded path runs (Flash and Flash-Lite) is the worst case.
    """
    from daimon.core.turn.runtime_defaults import (
        _GEMINI_WORKER_HOLD,  # pyright: ignore[reportPrivateUsage]
    )
    from mux.drivers.gemini import FLASH_PRIMARY
    from mux.profiles.gemini import INLINE_REUSE, WORKER_MAX_TOTAL_TOKENS, WORKER_MODEL

    prices = pricing.provider_prices(INLINE_REUSE.profile_id)
    for model in (FLASH_PRIMARY, WORKER_MODEL):
        price = prices[(INLINE_REUSE.provider, model)]
        worst = WORKER_MAX_TOTAL_TOKENS * max(price.input, price.output) / Decimal(10**6)
        assert worst <= Decimal(_GEMINI_WORKER_HOLD), (model, worst)


@pytest.mark.parametrize("worker_target", ("argon",), indirect=True)
def test_argon_worker_compiles_only_on_the_bounded_envelope(
    worker_target: tuple[GeminiPlan, ConfigRevision, ResolvedWorkerSpec],
) -> None:
    plan, revision, worker = worker_target
    check_backend(revision, worker=True)
    assert worker.backend.model == revision.model == "gemini-4-argon"
    # Unbounded (the total-token cap path is Flash's), Argon is refused.
    with pytest.raises(AdmissionDenied):
        plan.worker_deployment(worker, revision)
    # A bounded envelope whose worst case at Argon's tariff fits the $0.05 hold:
    # 10k input at $2/M + 2048 output at $10/M = $0.04048.
    bounded = replace(
        worker,
        limits=worker.limits.model_copy(
            update={"max_input_tokens": 10_000, "max_output_tokens": 2048, "max_turns": 1}
        ),
    )
    deployment = plan.worker_deployment(bounded, revision)
    assert deployment.agent.model == ModelRef(provider="gemini", id="gemini-4-argon")
    assert deployment.max_total_tokens is None
    assert (deployment.max_input_tokens, deployment.max_output_tokens) == (10_000, 2048)
    # The markup counts too: a $0.06 budget at 2x markup buys only $0.03 of provider cost.
    priced = replace(
        bounded, limits=bounded.limits.model_copy(update={"budget_usd": Decimal("0.06")})
    )
    assert plan.worker_deployment(priced, revision, markup=Decimal(1)) == deployment
    with pytest.raises(AdmissionDenied):
        plan.worker_deployment(priced, revision, markup=Decimal(2))


@pytest.mark.parametrize(
    ("worker_target", "max_input", "max_output"),
    (
        # Flash: 262144 * $0.75/M + 65536 * $3.75/M = $0.44 against a $0.05 hold.
        ("flash", 262144, 65536),
        # Argon: 30k * $2/M + 2048 * $10/M = $0.08 against a $0.05 hold.
        ("argon", 30_000, 2048),
    ),
    indirect=["worker_target"],
)
def test_explicit_worker_caps_whose_worst_case_exceeds_the_hold_are_refused(
    worker_target: tuple[GeminiPlan, ConfigRevision, ResolvedWorkerSpec],
    max_input: int,
    max_output: int,
) -> None:
    plan, revision, worker = worker_target

    def capped(max_input: int, max_output: int) -> ResolvedWorkerSpec:
        update = {"max_input_tokens": max_input, "max_output_tokens": max_output, "max_turns": 1}
        return replace(worker, limits=worker.limits.model_copy(update=update))

    # Control: the same worker with an envelope that fits is admitted.
    assert plan.worker_deployment(capped(1000, 1024), revision).max_output_tokens == 1024
    with pytest.raises(AdmissionDenied):
        plan.worker_deployment(capped(max_input, max_output), revision)


@pytest.mark.parametrize("missing", ("backend", "model"))
def test_cheap_worker_model_still_needs_explicit_root_grants(missing: str) -> None:
    from daimon.core.neutral_resources import WorkerSpecRefused

    root = parent("gemini")
    model = ModelRef(provider="gemini", id="gemini-3.5-flash-lite")
    selection = resolve_default(BackendConfig(backend="gemini", profile=PROFILE, model=model.id))
    root = replace(
        root,
        models=root.models if missing == "model" else (*root.models, model),
        backend_grants=root.backend_grants
        if missing == "backend"
        else (*root.backend_grants, selection),
    )
    spec = root.spec.model_copy(update={"model": model})
    with pytest.raises(WorkerSpecRefused) as refused:
        resolve_worker_spec(
            root,
            spec,
            backend=BackendConfig(backend="gemini", profile=PROFILE, model=spec.model.id),
            policy=POLICY,
            now=NOW,
        )
    assert (refused.value.field, refused.value.reason) == ("backend", "not_subset")


def test_resolved_drops_compile_remote_without_execution_or_memory(
    worker_target: tuple[GeminiPlan, ConfigRevision, ResolvedWorkerSpec],
) -> None:
    from mux.drivers.gemini.core import compile_environment

    plan, revision, worker = worker_target
    deployment = plan.worker_deployment(worker, revision)
    assert deployment.agent == worker.spec
    assert deployment.config_digest == revision.digest
    assert compile_agent(deployment.agent)["tools"] == [NO_TOOLS_TOOL]
    assert compile_environment(deployment.environment) == {"type": "remote", "sources": []}
    assert worker.resources == ()
    assert worker.spec.system is not None
    assert worker.spec.system.startswith(worker.permissions.base_system + "\n\n")
    assert "no persistent memory" in worker.spec.system
    assert "no sandbox execution" in worker.spec.system


def test_worker_materialization_compiles_the_canonical_spec_under_operator_ceiling(
    worker_target: tuple[GeminiPlan, ConfigRevision, ResolvedWorkerSpec],
) -> None:
    plan, revision, worker = worker_target
    template = plan.model_copy(
        update={"agent_name": "template-only", "system": "Template defaults"}
    )
    assert plan.worker_materialization is False
    assert "worker_materialization" not in plan.model_dump(mode="json")
    with pytest.raises(AdmissionDenied):
        template.worker_deployment(worker, revision)
    enabled = template.model_copy(update={"worker_materialization": True})
    deployment = enabled.worker_deployment(worker, revision)
    assert deployment.agent == worker.spec
    assert deployment.agent.name != enabled.agent_name
    assert deployment.agent.system != enabled.system
    assert deployment.agent.system == worker.permissions.spec.system
    assert compile_agent(deployment.agent)["tools"] == [NO_TOOLS_TOOL]


def test_worker_materialization_cannot_add_an_unreviewed_non_execution_tool(
    worker_target: tuple[GeminiPlan, ConfigRevision, ResolvedWorkerSpec],
) -> None:
    plan, revision, worker = worker_target
    requested = replace(
        worker,
        spec=worker.spec.model_copy(
            update={
                "tools": (ToolSpec(name="google_search", kind="builtin"),),
                "declared_drops": tuple(drop for drop in worker.declared_drops if drop != "tools"),
            }
        ),
    )
    with pytest.raises(AdmissionDenied):
        plan.model_copy(update={"worker_materialization": True}).worker_deployment(
            requested, revision
        )


def test_sealed_worker_readback_stays_available_but_materialization_refuses(
    worker_target: tuple[GeminiPlan, ConfigRevision, ResolvedWorkerSpec],
) -> None:
    plan, revision, worker = worker_target
    sealed = replace(
        worker, permissions=replace(worker.permissions, seal_ids=frozenset({"root-seal"}))
    )
    assert plan.worker_deployment(sealed, revision).agent == worker.spec
    with pytest.raises(AdmissionDenied):
        plan.model_copy(update={"worker_materialization": True}).worker_deployment(sealed, revision)


@pytest.mark.parametrize("flag", (None, 0, 1, "true", "false", []))
def test_worker_materialization_is_an_explicit_strict_operator_capability(
    worker_target: tuple[GeminiPlan, ConfigRevision, ResolvedWorkerSpec], flag: object
) -> None:
    from pydantic import ValidationError

    plan, _, _ = worker_target
    with pytest.raises(ValidationError):
        GeminiPlan.model_validate({**plan.model_dump(), "worker_materialization": flag})


@pytest.mark.parametrize(
    "drift",
    (
        "memory",
        "sandbox",
        "execution",
        "custom",
        "mcp",
        "resources",
        "network",
        "packages",
        "environment-none",
        "files",
        "repository",
        "instructions",
        "scope",
        "model",
    ),
)
def test_worker_target_refuses_undeclared_or_unrepresentable_native_intent(
    worker_target: tuple[GeminiPlan, ConfigRevision, ResolvedWorkerSpec],
    drift: str,
) -> None:
    plan, revision, worker = worker_target
    if drift in ("memory", "sandbox"):
        worker = replace(
            worker,
            spec=worker.spec.model_copy(
                update={
                    "declared_drops": tuple(item for item in worker.declared_drops if item != drift)
                }
            ),
        )
    elif drift in ("execution", "custom"):
        tool = ToolSpec(
            name="code_execution" if drift == "execution" else "run_command",
            kind="builtin" if drift == "execution" else "custom",
        )
        worker = replace(
            worker,
            spec=worker.spec.model_copy(
                update={
                    "tools": (tool,),
                    "declared_drops": tuple(
                        item for item in worker.declared_drops if item != "tools"
                    ),
                }
            ),
        )
        plan = plan.model_copy(update={"tools": (tool,)})
    elif drift == "mcp":
        worker = replace(
            worker,
            spec=worker.spec.model_copy(
                update={
                    "mcp_servers": (MCPConnection(name="execute", url="https://mcp.invalid"),),
                    "declared_drops": tuple(
                        item for item in worker.declared_drops if item not in ("mcp", "tools")
                    ),
                }
            ),
        )
    elif drift == "resources":
        worker = replace(
            worker,
            resources=(ResourceBinding(id="retained", kind="artifact", target_path="input.txt"),),
        )
    elif drift in ("network", "packages", "environment-none", "files", "repository"):
        update = (
            {"network": NetworkPolicy(mode="none")}
            if drift == "network"
            else {"packages": {"pip": ("numpy",)}}
            if drift == "packages"
            else {"execution": "none"}
            if drift == "environment-none"
            else {"sources": (WorkspaceSource(kind="file", target_path="input.txt"),)}
            if drift == "files"
            else {
                "sources": (
                    WorkspaceSource(
                        kind="repository", target_path="repo", repository_url="https://repo.invalid"
                    ),
                )
            }
        )
        plan = plan.model_copy(update={"environment": plan.environment.model_copy(update=update)})
    elif drift == "instructions":
        plan = plan.model_copy(update={"system": "Stripped protected guidance"})
    elif drift == "scope":
        plan = plan.model_copy(update={"account_id": "foreign"})
    elif drift == "model":
        worker = replace(
            worker,
            spec=worker.spec.model_copy(
                update={"model": ModelRef(provider="gemini", id="gemini-3.5-pro")}
            ),
        )
    with pytest.raises(AdmissionDenied) as refused:
        plan.worker_deployment(worker, revision)
    assert refused.value.reason == "backend_unsupported"


@pytest.fixture
def provider(request: pytest.FixtureRequest) -> Literal["openai", "gemini"]:
    return cast(Literal["openai", "gemini"], getattr(request, "param", "gemini"))


@pytest.mark.parametrize(
    "preparation",
    (
        "checked",
        "missing",
        "ordinary",
        "drift",
        "sealed",
        "publish",
        "held",
        "identity",
        "revision",
        "tenant",
        "account",
        "workspace",
        "provider",
    ),
)
async def test_worker_native_readback_checks_stripped_tools_mounts_and_protected_guidance(
    owned: Owned,
    monkeypatch: pytest.MonkeyPatch,
    preparation: str,
) -> None:
    root = parent("gemini")
    config = owned.request.admission.backend_revision
    assert config is not None and isinstance(owned.plan, GeminiPlan)
    root = replace(
        root,
        fleet=replace(
            root.fleet,
            scope=owned.request.scope,
            root=root.fleet.root.model_copy(update={"channel": config.channel}),
            account_id=UUID(owned.request.scope.account_id),
        ),
        spec=owned.retained.agent.spec,
        base_system=owned.retained.agent.spec.system or "",
    )
    worker = resolve_worker_spec(
        root,
        root.spec.model_copy(
            update={
                "declared_drops": (
                    "memory",
                    "sandbox",
                    "skills",
                    "mcp",
                    "tools",
                    "files",
                    "repository",
                )
            }
        ),
        policy=POLICY,
        now=NOW,
    )
    agent = await owned.resources.backend.agents.create(
        owned.request.scope, worker.spec, key="explicit-worker-counterpart"
    )
    retained = await owned.resources.catalog.put_agent(
        owned.request.scope, "owned-counterpart", agent, expected_revision=1
    )
    plan = owned.plan.model_copy(
        update={
            "agent_name": worker.spec.name,
            "system": worker.spec.system,
            "agent_id": agent.ref.id,
            "skill_ids": (),
            "counterpart_catalog_revision": retained.local_revision,
        }
    )
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    owned.wire.requests.clear()
    checked = await plan.read_counterpart(
        replace(owned.request, worker=worker), source=owned.source, resources=owned.resources
    )
    assert checked == retained
    assert owned.wire.agent["tools"] == []
    assert owned.wire.agent["base_environment"] == {"type": "remote", "sources": []}
    assert owned.wire.agent["system_instruction"] == worker.spec.system
    assert [request.method for request in owned.wire.requests] == ["GET"]
    for field, drift in (
        ("tools", [{"type": "code_execution"}]),
        (
            "base_environment",
            {
                "type": "remote",
                "sources": [
                    {
                        "type": "inline",
                        "target": ".agents/skills/hidden/SKILL.md",
                        "content": "hidden",
                    }
                ],
            },
        ),
        ("system_instruction", root.base_system),
    ):
        original = owned.wire.agent[field]
        owned.wire.agent[field] = drift
        with pytest.raises(AdmissionDenied) as refused:
            await plan.read_counterpart(
                replace(owned.request, worker=worker),
                source=owned.source,
                resources=owned.resources,
            )
        assert refused.value.reason == "backend_unsupported"
        owned.wire.agent[field] = original

    # Persist the independently read-back native acknowledgement at the owned storage edge.
    import mux.drivers.gemini as sdk_deployment
    from daimon.core.turn import gemini_deployment

    storage = MemoryStorage()
    async with storage.transaction() as records:
        records.agents[checked.agent.ref.id] = checked.agent
    transport = TrackedTransport()
    async with AsyncExitStack() as stack:
        client = build_gemini_client(
            transport=httpx.MockTransport(owned.wire.handle),
            api_key="offline",
            base_url="https://gemini.invalid",
        )
        stack.callback(client.close)
        stack.push_async_callback(client.aio.aclose)
        catalog = SDKAgentCatalogTransport(client)

        def retained_storage(path: Path, *, namespace: str) -> Storage:
            return storage

        def offline_transport(*, api_key: str) -> Transport:
            if api_key != "offline":
                raise AssertionError("only the explicit offline credential is accepted")
            return transport

        def offline_catalog(*, api_key: str) -> AgentCatalogTransport:
            if api_key != "offline":
                raise AssertionError("only the explicit offline credential is accepted")
            return catalog

        original_get = os.environ.get

        def offline_key(name: str, default: str | None = None) -> str | None:
            return "offline" if name == plan.api_key_env else original_get(name, default)

        monkeypatch.setattr(gemini_deployment, "SQLiteGeminiStorage", retained_storage)
        monkeypatch.setattr(sdk_deployment, "configured_transport", offline_transport)
        monkeypatch.setattr(sdk_deployment, "configured_catalog_transport", offline_catalog)
        monkeypatch.setattr(os.environ, "get", offline_key)
        request = replace(
            owned.request,
            source=owned.source,
            worker=worker,
            counterpart=checked,
            deps=replace(owned.request.deps, agent_backends=True),
            admission=replace(owned.request.admission, memory_read_only=True),
        )
        runtime = build_gemini_runtime(request)
        assert runtime.read_agent_counterpart is not None
        assert await runtime.read_agent_counterpart(request) == checked
        if preparation != "checked":
            if preparation == "missing":
                request = replace(request, counterpart=None)
            elif preparation == "ordinary":
                request = replace(request, worker=None)
            elif preparation == "drift":
                request = replace(
                    request,
                    counterpart=replace(
                        checked,
                        agent=checked.agent.model_copy(
                            update={
                                "spec": checked.agent.spec.model_copy(update={"system": "stripped"})
                            }
                        ),
                    ),
                )
            elif preparation == "sealed":
                request = replace(
                    request,
                    admission=replace(request.admission, origin_seal_ids=frozenset({UUID(int=1)})),
                )
            elif preparation == "held":
                request = replace(
                    request,
                    admission=replace(
                        request.admission,
                        hold=TurnHold(platform="slack", channel_id="C-SYN-1", client=True),
                    ),
                )
            elif preparation == "publish":
                request = replace(
                    request, admission=replace(request.admission, asks_before_publishing=True)
                )
            elif preparation == "revision":
                request = replace(
                    request, counterpart=replace(checked, local_revision=checked.local_revision + 1)
                )
            else:
                field = {
                    "identity": "id",
                    "tenant": "tenant_id",
                    "account": "account_id",
                    "workspace": "account_scope_id",
                    "provider": "provider",
                }[preparation]
                request = replace(
                    request,
                    counterpart=replace(
                        checked,
                        agent=checked.agent.model_copy(
                            update={
                                "ref": checked.agent.ref.model_copy(
                                    update={field: "openai" if field == "provider" else "foreign"}
                                )
                            }
                        ),
                    ),
                )

            def forbidden(config: ConfigRevision, scope: Scope) -> object:
                raise AssertionError("refused preparation allocated a provider transport")

            request = replace(
                request,
                deps=replace(
                    request.deps,
                    turn_runtimes={PROFILE: replace(runtime, transport_factory=forbidden)},
                ),
            )
            with pytest.raises(AdmissionDenied) as refused:
                await prepare_gemini_turn(request)
            assert refused.value.reason == "backend_unsupported"
            assert not transport.requests
            return
        prepared = await prepare_gemini_turn(
            replace(request, deps=replace(request.deps, turn_runtimes={PROFILE: runtime}))
        )
        assert prepared.admission.memory_read_only is True
        assert prepared.backend is not None and prepared.session_ref is not None
        assert prepared.runtime is not None and not transport.requests
        transport.responses.append(interaction("worker-interaction", status="completed"))
        await prepared.backend.events.send(
            request.scope,
            prepared.session_ref,
            (UserMessage(content=(TextPart(text="Do the bounded task"),)),),
            key="worker-input",
        )
        assert transport.requests[0]["agent"] == checked.agent.ref.id
        assert transport.requests[0]["tools"] == [NO_TOOLS_TOOL]
        assert transport.requests[0]["environment"] == {"type": "remote", "sources": []}
        async with storage.transaction() as records:
            assert records.sessions[prepared.session_ref.id].spec.resources == ()


async def test_worker_materialization_does_not_weaken_ordinary_read_only_plan(owned: Owned) -> None:
    assert isinstance(owned.plan, GeminiPlan) and owned.request.worker is None
    plan = owned.plan.model_copy(
        update={"worker_materialization": True, "system": "Different operator instruction"}
    )
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    with pytest.raises(AdmissionDenied):
        await plan.read_counterpart(owned.request, source=owned.source, resources=owned.resources)
    assert all(request.method == "GET" for request in owned.wire.requests)


@pytest.mark.parametrize("origin", ("fresh", "clone"))
@pytest.mark.parametrize("outcome", ("acknowledged", "lost_ack", "drift"))
async def test_worker_catalog_materializes_exact_spec_with_one_durable_creation(
    owned: Owned,
    tmp_path: Path,
    origin: str,
    outcome: str,
) -> None:
    from mux.drivers.gemini import GeminiManagedAgents
    from mux.drivers.gemini.transport import SDKTransport
    from mux.errors import ProviderError

    source = await owned.resources.read(owned.retained.catalog_id)
    config = owned.request.admission.backend_revision
    assert config is not None
    root = parent("gemini")
    root = replace(
        root,
        fleet=replace(
            root.fleet,
            scope=owned.request.scope,
            root=root.fleet.root.model_copy(update={"channel": config.channel}),
            account_id=UUID(owned.request.scope.account_id),
        ),
        spec=source.agent.spec,
        base_system=source.agent.spec.system or "",
        source_agents=(source.agent.ref,),
        source_revisions=((source.agent.ref, source.agent.revision),),
    )
    drops = ("memory", "sandbox", "skills", "mcp", "tools", "files", "repository")
    patch = AgentPatch(name=f"{origin}-worker", declared_drops=drops)
    worker = resolve_worker_spec(
        root,
        source.agent
        if origin == "clone"
        else root.spec.model_copy(update={"name": patch.name, "declared_drops": drops}),
        overrides=patch if origin == "clone" else None,
        policy=POLICY,
        now=NOW,
    )
    assert worker.source_agent == (
        (source.agent.ref, source.agent.revision) if origin == "clone" else None
    )
    assert worker.spec != source.agent.spec and worker.spec.tools == ()
    key = f"fleet:{root.fleet.run_id}:spawn:0:agent"
    catalog_id = f"fleet:{root.fleet.run_id}:0"
    record_path = tmp_path / "materialization.sqlite"
    state_store = owned.request.deps.state_store
    assert state_store is not None
    posts = 0

    def native(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        response = owned.wire.handle(request)
        if request.method == "POST":
            posts += 1
            if outcome == "lost_ack":
                raise httpx.ReadError("offline accepted creation with lost acknowledgement")
        elif outcome == "drift":
            return httpx.Response(
                200, json={**owned.wire.agent, "tools": [{"type": "code_execution"}]}
            )
        return response

    async with AsyncExitStack() as stack:
        sdk = build_gemini_client(
            transport=httpx.MockTransport(native),
            api_key="offline",
            base_url="https://gemini.invalid",
        )
        stack.callback(sdk.close)
        stack.push_async_callback(sdk.aio.aclose)

        def host() -> NeutralResourceHost:
            backend = GeminiManagedAgents(
                SDKTransport(sdk),
                storage=SQLiteGeminiStorage(record_path, namespace="worker-materialization"),
                state_store=state_store,
                account_scope_id=owned.resources.workspace,
                resource_catalog_enabled=True,
                resource_catalog_transport=SDKAgentCatalogTransport(sdk),
            )
            return NeutralResourceHost(
                backend,
                owned.request.deps.sessionmaker,
                scope=worker.permissions.fleet.scope,
                account_scope_id=owned.resources.workspace,
                operation_key=key,
                resource_catalog_enabled=True,
                catalog_adapter=backend.catalog_adapter,
            )

        resources = host()
        owned.wire.requests.clear()
        if outcome != "acknowledged":
            with pytest.raises(ProviderError):
                await resources.publish(catalog_id, worker.spec, expected_revision=0, key=key)
            assert await resources.catalog.get_agent(resources.scope, catalog_id) is None
            # A new owner instance reconciles the durable creation by GET only.
            resources = host()
            if outcome == "drift":
                with pytest.raises(ProviderError):
                    await resources.publish(catalog_id, worker.spec, expected_revision=0, key=key)
                assert posts == 1
                assert await resources.catalog.get_agent(resources.scope, catalog_id) is None
                return
        row = await resources.publish(catalog_id, worker.spec, expected_revision=0, key=key)
        assert posts == 1 and row.local_revision == 1
        assert row.agent.spec == worker.spec
        assert row.agent.ref != source.agent.ref
        assert await resources.read(catalog_id) == row
        assert owned.wire.agent["tools"] == []
        assert owned.wire.agent["base_environment"] == {"type": "remote", "sources": []}
        assert owned.wire.agent["system_instruction"] == worker.spec.system
        assert owned.wire.agent["agent_config"] == {"type": "antigravity", "model": FLASH_PRIMARY}
        assert [request.method for request in owned.wire.requests] == ["POST", "GET", "GET"]
        # Catalog publication does not mutate the retained source row or revision.
        assert await owned.resources.entry(source.catalog_id) == source


@pytest.mark.parametrize(
    "invalid",
    [
        "absent",
        "missing",
        "malformed",
        "oversize",
        "profile",
        "tools-missing",
        "tools-null",
        "tenant",
        "account",
        "platform",
        "channel",
        "digest",
        "duplicate",
        "inline-key",
        "relative",
        "same-db",
        "cap",
    ],
)
async def test_invalid_deployment_refuses_before_key_or_persistence(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid: str,
) -> None:
    row = entry(selected, tmp_path)
    if invalid == "profile":
        row["profile"] = "openai.persistent_workspace"
    elif invalid == "tools-missing":
        del row["tools"]
    elif invalid == "tools-null":
        row["tools"] = None
    elif invalid in ("tenant", "account", "platform", "channel", "digest"):
        row[
            {
                "tenant": "tenant_id",
                "account": "account_id",
                "platform": "platform",
                "channel": "channel_id",
                "digest": "config_digest",
            }[invalid]
        ] = "foreign"
    elif invalid == "inline-key":
        row["api_key"] = "never-show-secret"
    elif invalid == "relative":
        row["records_path"] = "relative.sqlite"
    elif invalid == "same-db":
        row["holds_path"] = row["records_path"]
    elif invalid == "cap":
        row["hold_usd"] = "1"
    manifest(
        monkeypatch, tmp_path / "manifest.json", [row, row] if invalid == "duplicate" else [row]
    )
    path = tmp_path / "manifest.json"
    if invalid == "absent":
        monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE")
    elif invalid == "missing":
        path.unlink()
    elif invalid == "malformed":
        path.write_text("invalid-json")
    elif invalid == "oversize":
        path.write_bytes(b" " * 1_048_577)
    original = os.environ.get

    def get(name: str, default: str | None = None) -> str | None:
        if name == "N4_OFFLINE_GEMINI_KEY":
            pytest.fail("refused deployment accessed key")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", get)
    with pytest.raises(AdmissionDenied) as error:
        build_channel_runtime(selected)
    assert error.value.reason == "backend_unsupported" and "never-show-secret" not in str(
        error.value
    )
    assert not (tmp_path / "records.sqlite").exists() and not (tmp_path / "holds.sqlite").exists()


@pytest.mark.parametrize("tools", [[], [{"name": "google_search", "kind": "builtin"}]])
async def test_only_explicit_tools_compile_and_scope_is_pinned(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tools: list[dict[str, JsonValue]],
) -> None:
    row = entry(selected, tmp_path)
    row["tools"] = cast(JsonValue, tools)
    manifest(monkeypatch, tmp_path / "manifest.json", [row])
    runtime = build_channel_runtime(selected)
    assert isinstance(runtime.journal, GeminiJournal) and runtime.journal.deployment is not None
    native = compile_agent(runtime.journal.deployment.agent)
    assert native["tools"] == ([{"type": "google_search"}] if tools else [NO_TOOLS_TOOL])
    assert runtime.journal.state_store is selected.deps.state_store
    assert isinstance(runtime.usage_store, GeminiUsageRuntime)
    expected = {model: price for (_, model), price in pricing.provider_prices(PROFILE).items()}
    assert runtime.usage_store.prices == expected
    assert all(runtime.usage_store.prices[model] is price for model, price in expected.items())
    with pytest.raises(TypeError):
        runtime.usage_store.prices[FLASH_PRIMARY] = expected[FLASH_PRIMARY]
        # Inline reuse has no sandbox or container: its usage settles at $0 infrastructure.
        assert runtime.usage_store.infrastructure_usd == Decimal(0)
    assert not (tmp_path / "records.sqlite").exists()
    config = selected.admission.backend_revision
    assert config is not None
    with pytest.raises(ScopeViolation):
        runtime.transport_factory(
            config, selected.scope.model_copy(update={"account_id": "foreign"})
        )


async def test_storage_restart_rollback_full_bytes_and_tuple_keys(tmp_path: Path) -> None:
    path = tmp_path / "records.sqlite"

    def storage(namespace: str = "actual-account") -> SQLiteGeminiStorage:
        return SQLiteGeminiStorage(path, namespace=namespace)

    ref = ResourceRef(
        id="artifact",
        kind="artifact",
        provider="gemini",
        account_scope_id="project",
        tenant_id="tenant",
        account_id="account",
    )
    async with storage().transaction() as records:
        records.creations[("tenant", "account", "key")] = ("principal", "digest", "agent")
        records.artifacts["artifact"] = ArtifactRecord(
            Artifact(
                ref=ref,
                filename="binary.bin",
                media_type="application/octet-stream",
                size_bytes=3,
                created_at=datetime.now(UTC),
            ),
            "digest",
            b"\x00\xff\x80",
        )
    with pytest.raises(RuntimeError):
        async with storage().transaction() as records:
            records.creations.clear()
            raise RuntimeError("rollback")
    async with storage().transaction() as records:
        assert records.creations[("tenant", "account", "key")] == ("principal", "digest", "agent")
        assert records.artifacts["artifact"].body == b"\x00\xff\x80"
    async with storage("other-account").transaction() as records:
        assert not records.creations and not records.artifacts


async def test_storage_serializes_distinct_workers_and_recovers_after_cancellation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "records.sqlite"
    first, second = (SQLiteGeminiStorage(path, namespace="account") for _ in range(2))
    started = asyncio.Event()

    async def blocked() -> None:
        started.set()
        async with second.transaction() as records:
            records.creations[("t", "a", "second")] = ("p", "d", "two")

    async with first.transaction() as records:
        records.creations[("t", "a", "first")] = ("p", "d", "one")
        task = asyncio.create_task(blocked())
        await started.wait()
        await asyncio.sleep(0.05)
        assert not task.done()
    await task
    async with first.transaction() as records:
        assert len(records.creations) == 2

    async def cancel() -> None:
        async with second.transaction() as records:
            records.creations.clear()
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancel()
    async with first.transaction() as records:
        assert len(records.creations) == 2


@pytest.mark.parametrize("outcome", ["accepted", "refused", "unknown"])
async def test_holds_survive_restart_and_refusal_never_releases_budget(
    tmp_path: Path, outcome: str
) -> None:
    path = tmp_path / "holds.sqlite"

    def accounting() -> SQLiteFlashAccounting:
        return SQLiteFlashAccounting(
            path, namespace="account", hold_usd=Decimal(".05"), cap_usd=Decimal(".05")
        )

    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    hold = accounting()
    await hold.before_attempt(model=model, attempt=1)
    await hold.after_attempt(
        receipt=FlashAttempt(model, 1, cast("Literal['accepted', 'refused', 'unknown']", outcome))
    )
    with pytest.raises(AdmissionDenied) as error:
        await accounting().before_attempt(model=model, attempt=1)
    assert error.value.reason == "provider_hold_cap_reached"


async def test_argon_attempt_holds_and_near_miss_ids_refuse(tmp_path: Path) -> None:
    hold = SQLiteFlashAccounting(
        tmp_path / "holds.sqlite",
        namespace="account",
        hold_usd=Decimal(".05"),
        cap_usd=Decimal(".10"),
    )
    for near in ("gemini-4-argon-preview", "gemini-4"):
        with pytest.raises(AdmissionDenied):
            await hold.before_attempt(model=ModelRef(provider="gemini", id=near), attempt=1)
    argon = ModelRef(provider="gemini", id=ARGON_MODEL)
    await hold.before_attempt(model=argon, attempt=1)
    await hold.after_attempt(receipt=FlashAttempt(argon, 1, "accepted", interaction_id="argon"))


async def test_unknown_hold_commits_before_native_dispatch_and_survives_crash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "holds.sqlite"
    hold = SQLiteFlashAccounting(
        path, namespace="account", hold_usd=Decimal(".05"), cap_usd=Decimal(".05")
    )
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    await hold.before_attempt(model=model, attempt=1)
    # Simulate process loss before any receipt. A new worker must preserve hold.
    restart = SQLiteFlashAccounting(
        path, namespace="account", hold_usd=Decimal(".05"), cap_usd=Decimal(".05")
    )
    with pytest.raises(AdmissionDenied):
        await restart.before_attempt(model=model, attempt=1)


@pytest.mark.parametrize("selection", ["unset", "anthropic", "legacy", "disabled"])
async def test_unconfigured_gemini_never_reads_manifest_or_any_key(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    original = os.environ.get

    def forbidden(name: str, default: str | None = None) -> str | None:
        if name in (
            "DAIMON_TURN__PROVIDER_RUNTIME_FILE",
            "N4_OFFLINE_GEMINI_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
        ):
            pytest.fail("default path discovered provider configuration")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", forbidden)
    request = selected
    if selection == "unset":
        request = replace(request, admission=replace(request.admission, backend_revision=None))
    elif selection == "anthropic":
        revision = selected.admission.backend_revision
        assert revision is not None
        request = replace(
            request,
            admission=replace(
                request.admission,
                backend_revision=ConfigRevision.create(
                    revision.channel, 1, resolve_default(BackendConfig())
                ),
            ),
        )
    else:
        request = replace(
            request,
            deps=replace(
                request.deps,
                **(
                    {"turn_path": "legacy"}
                    if selection == "legacy"
                    else {"channel_backends": False}
                ),
            ),
        )
    with pytest.raises(AdmissionDenied):
        build_channel_runtime(request)


async def test_policy_refusal_precedes_selected_key_and_transport(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest(monkeypatch, tmp_path / "manifest.json", [entry(selected, tmp_path)])
    original = os.environ.get

    def forbidden(name: str, default: str | None = None) -> str | None:
        if name == "N4_OFFLINE_GEMINI_KEY":
            pytest.fail("policy refusal inspected credential")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", forbidden)
    with pytest.raises(AdmissionDenied):
        await bind_session_impl(
            selected.deps,
            replace(selected.admission, asks_before_publishing=True),
            tenant_id=selected.tenant_id,
            platform=selected.platform,
            external_user_id=selected.external_user_id,
            thread_id=selected.thread_id,
            session_account_id=selected.session_account_id,
            reuse_existing=True,
        )
    assert not (tmp_path / "records.sqlite").exists()


def argon_clock(monkeypatch: pytest.MonkeyPatch, day: datetime) -> None:
    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:  # pyright: ignore[reportIncompatibleMethodOverride]
            assert tz is UTC
            return day

    monkeypatch.setattr(pricing, "datetime", Clock)


def argon_selected(
    selected: ProviderPreparationRequest, monkeypatch: pytest.MonkeyPatch, day: datetime
) -> ProviderPreparationRequest:
    """Select Argon on the channel and move the tariff clock to `day`."""
    argon_clock(monkeypatch, day)
    revision = selected.admission.backend_revision
    assert revision is not None
    config = ConfigRevision.create(
        revision.channel,
        1,
        resolve_default(BackendConfig(backend="gemini", profile=PROFILE, model=ARGON_MODEL)),
    )
    return replace(
        selected,
        admission=replace(
            selected.admission, backend_revision=config, backend=check_backend(config)
        ),
    )


@pytest.mark.parametrize(
    "output_cap,model", ((None, FLASH_PRIMARY), (4096, FLASH_PRIMARY), (None, ARGON_MODEL))
)
async def test_application_constructs_gemini_and_reuses_persisted_native_binding(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_clean: None,
    output_cap: int | None,
    model: str,
) -> None:
    import httpx

    from .test_gemini_codec import interaction

    if model == ARGON_MODEL:
        selected = argon_selected(selected, monkeypatch, datetime(2026, 11, 30, tzinfo=UTC))
    plan = entry(selected, tmp_path)
    if output_cap is not None:
        plan.update(
            max_output_tokens=output_cap, environment={"name": "no-execution", "execution": "none"}
        )
    manifest(monkeypatch, tmp_path / "manifest.json", [plan])
    monkeypatch.setenv("N4_OFFLINE_GEMINI_KEY", "offline-selected-key")
    calls: list[httpx.Request] = []
    posts: list[dict[str, JsonValue]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.headers["x-goog-api-key"] == "offline-selected-key"
        if request.method == "POST":
            body = json.loads(request.content)
            # A capped model request has no built-ins; an agent allows the sentinel.
            assert body["tools"] == ([NO_TOOLS_TOOL] if output_cap is None else [])
            if output_cap is None:
                assert body["agent_config"]["model"] == model
                assert "generation_config" not in body
            else:
                assert body["model"] == model
                assert body["generation_config"] == {"max_output_tokens": 4096}
                assert not {"agent", "agent_config", "environment"} & body.keys()
            assert "spend_control" not in body
            posts.append(body)
            if len(posts) > 1:
                assert body["previous_interaction_id"] == "one"
                if output_cap is None:
                    assert body["environment"] == "workspace"
            response = interaction("one" if len(posts) == 1 else "two")
        else:
            response = interaction("two" if request.url.path.endswith("two") else "one")
        if output_cap is not None:
            response.pop("environment_id")
        return httpx.Response(200, json=response)

    # Replace owned HTTP transports only; exercise the real product SDK and host.
    from .test_gemini_runtime_transport import clients_for

    clients = clients_for(monkeypatch, handle)
    async with selected.deps.sessionmaker() as db, db.begin():
        await make_tenant(db, id=selected.tenant_id)
    deps = replace(selected.deps, state_store=None)

    async def forbidden_anthropic_backfill(*args: object, **kwargs: object) -> None:
        pytest.fail("foreign native sessions must not enter Anthropic billing catch-up")

    monkeypatch.setattr("daimon.core.turn.run.settle_session", forbidden_anthropic_backfill)

    session_id = None
    for index in range(2):
        prepared = await bind_session_impl(
            deps,
            selected.admission,
            tenant_id=selected.tenant_id,
            platform=selected.platform,
            external_user_id=selected.external_user_id,
            thread_id=selected.thread_id,
            session_account_id=selected.session_account_id,
            reuse_existing=True,
        )
        assert prepared.runtime is not None and isinstance(prepared.runtime.journal, GeminiJournal)
        deployment = prepared.runtime.journal.deployment
        assert deployment is not None and deployment.agent.model.id == model
        if session_id is not None:
            assert prepared.ma_session_id == session_id
        session_id = prepared.ma_session_id

        async def reseed() -> str:
            pytest.fail("Gemini turn used Anthropic recovery")

        outcome = await run_prepared_turn(
            deps,
            prepared,
            tenant_id=selected.tenant_id,
            platform=selected.platform,
            thread_id=selected.thread_id,
            external_user_id=selected.external_user_id,
            user_message="question",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            operation_key=f"offline-gemini:{index}",
        )
        assert outcome.state.error is None and not outcome.recovered
        from daimon.core.turn.state import extract_final_response

        assert extract_final_response(outcome.state.content) == "answer"
        assert outcome.state.usage_totals.output_tokens == 11
    assert len(posts) == 2 and calls and len(clients) == 2 * len(calls)
    assert all(client.is_closed for client in clients)
    assert (tmp_path / "records.sqlite").exists() and (tmp_path / "holds.sqlite").exists()
    # Inline reuse has no infrastructure charge, so its measured usage settles at once:
    # nothing pending, and the turn's cost is on the ledger (SMOKE fc0fa16a: every
    # Gemini usage row stayed pending forever, so no Gemini turn was ever debited).
    from sqlalchemy import text

    async with deps.sessionmaker() as db:
        assert (
            await db.scalar(text("SELECT count(*) FROM accounting_outbox WHERE applied_at IS NULL"))
            == 0
        )
        debited = await db.scalar(
            text("SELECT coalesce(sum(delta_usd), 0) FROM tenant_ledger WHERE delta_usd < 0")
        )
        assert debited < 0


type _ClaimedWorker = tuple[
    ProviderPreparationRequest,
    WorkerPermissions,
    FleetWorker,
    Mapping[tuple[str, str], ModelRates | ProviderPrice],
    Owned,
    list[httpx.Request],
]


@pytest.fixture
def materialization_worker_request(
    claimed_worker_request: _ClaimedWorker, monkeypatch: pytest.MonkeyPatch
) -> tuple[_ClaimedWorker, str]:
    from daimon.core.turn import gemini_deployment

    selected, root, worker, prices, owned, calls = claimed_worker_request
    values = json.loads(owned.manifest_path.read_text())[0]
    values.update(
        worker_materialization=True,
        agent_id=None,
        counterpart_catalog_id=None,
        counterpart_catalog_revision=None,
    )
    plan = GeminiPlan.model_validate(values)
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))
    before = owned.wire.native_id
    owned.wire.requests.clear()
    monkeypatch.setattr(gemini_deployment, "SQLiteGeminiStorage", SQLiteGeminiStorage)
    selected = replace(selected, deps=replace(selected.deps, turn_runtimes={}))
    return (selected, root, worker, prices, owned, calls), before


@pytest.mark.parametrize("clone_source", (False, True), indirect=True)
async def test_worker_materialization_runs_the_real_headless_driver(
    materialization_worker_request: tuple[_ClaimedWorker, str],
    clone_source: bool,
) -> None:
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores import fleet, mux_state
    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import drain_outcomes

    (selected, root, worker, prices, owned, calls), before = materialization_worker_request
    assert selected.worker is not None and selected.admission.backend_revision is not None
    assert (selected.worker.source_agent is not None) is clone_source
    result = await run_worker(
        selected,
        parent=root,
        worker=worker,
        trigger_message="Complete the assigned task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert result.id == worker.id and result.status == "succeeded" and result.turns_used == 1
    assert result.result is not None and result.result.excerpt == "verified-worker-result"
    creations = [call for call in owned.wire.requests if call.method == "POST"]
    assert len(creations) == 1
    assert len([call for call in owned.wire.requests if call.method == "GET"]) == 2
    body = json.loads(creations[0].content)
    assert body["id"] == owned.wire.native_id and body["id"] != before
    assert body["agent_config"] == {"type": "antigravity", "model": FLASH_PRIMARY}
    assert body["tools"] == [] and body["base_environment"] == {"type": "remote", "sources": []}
    assert selected.worker is not None
    assert body["system_instruction"] == selected.worker.spec.system
    inputs = [call for call in calls if call.method == "POST"]
    assert len(inputs) == 1 and json.loads(inputs[0].content)["agent"] == body["id"]
    assert json.loads(inputs[0].content)["tools"] == [NO_TOOLS_TOOL]
    assert json.loads(inputs[0].content)["agent_config"] == {
        "type": "antigravity",
        "model": selected.worker.spec.model.id,
        "max_total_tokens": 65_536,
    }
    operation = f"fleet:{worker.run_id}:spawn:{worker.ordinal}"
    store = selected.deps.state_store
    assert store is not None
    record = await store.get_operation(selected.scope, operation + ":agent_create:0")
    assert record is not None and record.operation.status == "processed"
    assert Agent.model_validate(dict(record.result)).ref.id == body["id"]
    async with selected.deps.sessionmaker.begin() as db:
        graph, _ = await fleet.lock_budget(
            db, tenant_id=selected.tenant_id, actor_id=root.fleet.account_id, run_id=worker.run_id
        )
        actual = next(row for _, rows in graph for row in rows if row.id == worker.id)
        assert actual == result
        entries = await mux_state.read_events(
            db, result.result.session_id, after=result.result.sequence - 1, limit=1
        )
    assert len(entries) == 1 and entries[0].id == f"fleet:{worker.id}:result"
    assert entries[0].type == "agent.message"
    await drain_outcomes()
    async with selected.deps.sessionmaker() as db:
        outcomes = await list_for_tenant(db, selected.tenant_id)
    assert any(row.origin == "worker" and row.reason.value == "completed" for row in outcomes)


@pytest.mark.parametrize("fault", ("disabled", "lost_ack", "native_drift"))
async def test_real_worker_materialization_refuses_without_input_or_recreation(
    materialization_worker_request: tuple[_ClaimedWorker, str],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import mux.drivers.gemini as sdk_deployment
    from daimon.core.fleet import FleetConflict
    from daimon.core.headless_runner import run_worker
    from daimon.core.neutral_resources import WorkerSpecRefused
    from daimon.core.stores import fleet

    (selected, root, worker, prices, owned, calls), _ = materialization_worker_request
    if fault == "disabled":
        values = json.loads(owned.manifest_path.read_text())[0]
        values["worker_materialization"] = False
        owned.manifest_path.write_text(json.dumps([values]))

    def native(call: httpx.Request) -> httpx.Response:
        response = owned.wire.handle(call)
        if fault == "lost_ack" and call.method == "POST":
            raise httpx.ReadError("offline accepted native creation with lost acknowledgement")
        if fault == "native_drift" and call.method == "GET":
            return httpx.Response(200, json={**owned.wire.agent, "system_instruction": "stripped"})
        return response

    async with AsyncExitStack() as stack:
        sdk = build_gemini_client(transport=httpx.MockTransport(native), api_key="offline")
        stack.callback(sdk.close)
        stack.push_async_callback(sdk.aio.aclose)
        catalog = SDKAgentCatalogTransport(sdk)

        def transport(*, api_key: str) -> AgentCatalogTransport:
            assert api_key == "offline"
            return catalog

        monkeypatch.setattr(sdk_deployment, "configured_catalog_transport", transport)
        for _attempt in range(2 if fault == "lost_ack" else 1):
            with pytest.raises((AdmissionDenied, WorkerSpecRefused, FleetConflict)):
                await run_worker(
                    selected,
                    parent=root,
                    worker=worker,
                    trigger_message="Complete the assigned task",
                    prices=prices,
                    cancel=asyncio.Event(),
                )
            assert not calls
            posts = [call for call in owned.wire.requests if call.method == "POST"]
            assert len(posts) == (1 if fault in {"lost_ack", "native_drift"} else 0)
            async with selected.deps.sessionmaker.begin() as db:
                graph, _ = await fleet.lock_budget(
                    db,
                    tenant_id=selected.tenant_id,
                    actor_id=root.fleet.account_id,
                    run_id=worker.run_id,
                )
            actual = next(row for _, rows in graph for row in rows if row.id == worker.id)
            assert actual.turns_used == 0 and actual.binding_id is None and actual.result is None
            if fault == "lost_ack":
                assert actual.reserved_usd > 0
                store = selected.deps.state_store
                assert store is not None
                key = f"fleet:{worker.run_id}:spawn:{worker.ordinal}:agent_create:0"
                record = await store.get_operation(selected.scope, key)
                assert record is not None and record.operation.status == "outcome_unknown"
                assert not record.result


@pytest.mark.parametrize("activated", (False, True))
async def test_dynamic_worker_callbacks_require_a_lease_before_key_or_native_access(
    materialization_worker_request: tuple[_ClaimedWorker, str],
    monkeypatch: pytest.MonkeyPatch,
    activated: bool,
) -> None:
    from contextlib import nullcontext

    from daimon.core.turn.persistence import TurnPersistence
    from daimon.core.turn.runtime_manifest import capture_preparation_source
    from mux.contracts.ids import ThreadRef
    from mux.state.lease import Slot

    (selected, _, worker, _, owned, calls), _ = materialization_worker_request
    assert selected.admission.backend_revision is not None
    source = await capture_preparation_source(selected)

    async def forbidden() -> None:
        raise AssertionError("unleased caller invoked its financial guard")

    selected = replace(selected, source=source, before_native=forbidden)
    revision = selected.admission.backend_revision
    assert revision is not None
    runtime = build_gemini_runtime(selected)
    assert runtime.materialize_worker is not None and runtime.read_agent_counterpart is not None
    plan = GeminiPlan.model_validate(json.loads(owned.manifest_path.read_text())[0])
    getter = os.environ.get

    def key(name: str, default: str | None = None) -> str | None:
        if name == plan.api_key_env:
            raise AssertionError("unleased caller read the selected provider key")
        return getter(name, default)

    monkeypatch.setattr(os.environ, "get", key)
    store = selected.deps.state_store
    assert store is not None
    persistence = TurnPersistence(
        store,
        None,
        selected.scope,
        slot=Slot(
            thread=ThreadRef(
                channel=revision.channel,
                thread_id=f"fleet/{worker.run_id}/{worker.ordinal}",
            ),
            account_id=selected.scope.account_id,
        ),
        operation_key=f"fleet:{worker.run_id}:spawn:{worker.ordinal}",
        now=selected.now,
    )
    with persistence.activate() if activated else nullcontext():
        with pytest.raises((AdmissionDenied, ScopeViolation)):
            await runtime.materialize_worker(selected)
        with pytest.raises((AdmissionDenied, ScopeViolation)):
            await runtime.read_agent_counterpart(selected)
    assert not owned.wire.requests and not calls


@pytest.mark.parametrize("fault", ("missing", "different"))
async def test_dynamic_preparation_requires_the_prefix_verified_counterpart(
    materialization_worker_request: tuple[_ClaimedWorker, str],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from daimon.core.headless_runner import run_worker
    from daimon.core.turn import runtime_manifest

    (selected, root, worker, prices, owned, calls), _ = materialization_worker_request
    read = runtime_manifest.read_preparation_counterpart

    async def substitute(request: ProviderPreparationRequest) -> ProviderPreparationRequest:
        verified = await read(request)
        assert verified.counterpart is not None
        marker = (
            None
            if fault == "missing"
            else replace(verified.counterpart, catalog_id="different-catalog")
        )
        return replace(verified, verified_counterpart=marker)

    monkeypatch.setattr(runtime_manifest, "read_preparation_counterpart", substitute)
    with pytest.raises(AdmissionDenied) as refused:
        await run_worker(
            selected,
            parent=root,
            worker=worker,
            trigger_message="Complete the assigned task",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert refused.value.reason == "backend_unsupported"
    assert not any(call.method == "POST" for call in calls)
    assert [call.method for call in owned.wire.requests] == ["POST", "GET", "GET"]


async def _refuse_materialization_before_host(
    claimed_worker_request: _ClaimedWorker,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from contextlib import nullcontext

    from daimon.core.turn.persistence import TurnPersistence
    from daimon.core.turn.runtime_manifest import capture_preparation_source
    from daimon.core.turn.runtimes import build_channel_runtime
    from mux.contracts.ids import ThreadRef
    from mux.state.lease import Slot
    from mux.state.memory import MemoryStateStore

    selected, _, worker, _, owned, calls = claimed_worker_request
    values = json.loads(owned.manifest_path.read_text())[0]
    values.update(
        worker_materialization=True,
        agent_id=None,
        counterpart_catalog_id=None,
        counterpart_catalog_revision=None,
    )
    plan = type(owned.plan).model_validate(values)
    owned.manifest_path.write_text(json.dumps([plan.model_dump(mode="json")]))

    async def guard() -> None:
        pytest.fail("refused authority reached the fresh financial guard")

    selected = replace(
        selected,
        source=await capture_preparation_source(selected),
        before_native=guard,
        deps=replace(selected.deps, turn_runtimes={}),
    )
    runtime = build_channel_runtime(selected)
    assert runtime.materialize_worker is not None
    owned.wire.requests.clear()
    calls.clear()
    changed = selected
    if fault == "scope":
        changed = replace(
            selected, scope=selected.scope.model_copy(update={"authorization_id": "foreign"})
        )
    elif fault == "store":
        changed = replace(selected, deps=replace(selected.deps, state_store=MemoryStateStore()))
    elif fault == "worker":
        changed = replace(selected, worker=None)
    elif fault == "source":
        changed = replace(selected, source=None)
    elif fault == "guard":
        changed = replace(selected, before_native=None)
    assert selected.deps.state_store is not None and selected.admission.backend_revision is not None
    persistence = TurnPersistence(
        selected.deps.state_store,
        None,
        selected.scope,
        slot=Slot(
            thread=ThreadRef(
                channel=selected.admission.backend_revision.channel, thread_id=selected.thread_id
            ),
            account_id=selected.scope.account_id,
        ),
        operation_key=f"fleet:{worker.run_id}:spawn:{worker.ordinal}",
        now=selected.now,
    )
    getter = os.environ.get

    def key(name: str, default: str | None = None) -> str | None:
        if name == plan.api_key_env:
            pytest.fail("refused authority read the provider key")
        return getter(name, default)

    monkeypatch.setattr(os.environ, "get", key)

    async def materialize() -> None:
        assert runtime.materialize_worker is not None
        await runtime.materialize_worker(changed)

    with (
        persistence.activate() if fault != "context" else nullcontext(),
        pytest.raises((AdmissionDenied, ScopeViolation)),
    ):
        if fault in ("context", "lease"):
            await materialize()
        else:
            await persistence.run(materialize)
    assert not owned.wire.requests and not calls


@pytest.mark.parametrize(
    "fault", ("context", "lease", "scope", "store", "worker", "source", "guard")
)
async def test_gemini_shared_materialization_refuses_before_provider_host_construction(
    claimed_worker_request: _ClaimedWorker,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    await _refuse_materialization_before_host(claimed_worker_request, monkeypatch, fault)


@pytest.mark.parametrize("provider", ("openai",), indirect=True)
@pytest.mark.parametrize("claimed_openai_worker", (False,), indirect=True)
@pytest.mark.parametrize(
    "fault", ("context", "lease", "scope", "store", "worker", "source", "guard")
)
async def test_openai_shared_materialization_refuses_before_provider_host_construction(
    claimed_openai_worker: tuple,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    await _refuse_materialization_before_host(claimed_openai_worker[:6], monkeypatch, fault)


async def test_worker_runtime_refuses_a_deadline_beyond_the_cancel_bound_before_key_access(
    materialization_worker_request: tuple[_ClaimedWorker, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    (selected, _, _, _, owned, calls), _ = materialization_worker_request
    selected = replace(selected, deadline=selected.now() + timedelta(seconds=121))
    plan = GeminiPlan.model_validate(json.loads(owned.manifest_path.read_text())[0])
    getter = os.environ.get

    def key(name: str, default: str | None = None) -> str | None:
        if name == plan.api_key_env:
            pytest.fail("unbounded worker read its provider key")
        return getter(name, default)

    monkeypatch.setattr(os.environ, "get", key)
    with pytest.raises(AdmissionDenied) as refused:
        build_gemini_runtime(selected)
    assert refused.value.reason == "backend_unsupported"
    assert not owned.wire.requests and not calls


async def test_argon_after_its_intro_tariff_refuses_before_runtime_discovery(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Admitted inside the window; the tariff expires before the runtime is built.
    selected = argon_selected(selected, monkeypatch, datetime(2026, 11, 30, tzinfo=UTC))
    argon_clock(monkeypatch, datetime(2026, 12, 1, tzinfo=UTC))
    getter = os.environ.get

    def no_manifest(name: str, default: str | None = None) -> str | None:
        if name == "DAIMON_TURN__PROVIDER_RUNTIME_FILE":
            pytest.fail("unpriced Argon discovered its runtime")
        return getter(name, default)

    monkeypatch.setattr(os.environ, "get", no_manifest)
    with pytest.raises(AdmissionDenied) as refused:
        build_gemini_runtime(selected)
    assert refused.value.reason == "backend_unsupported"


async def test_cheap_model_does_not_enable_an_ordinary_channel_runtime(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mux.profiles.gemini import WORKER_MODEL

    revision = selected.admission.backend_revision
    assert revision is not None and selected.worker is None
    changed = revision.model_copy(update={"model": WORKER_MODEL})
    selected = replace(selected, admission=replace(selected.admission, backend_revision=changed))
    getter = os.environ.get

    def no_manifest(name: str, default: str | None = None) -> str | None:
        if name == "DAIMON_TURN__PROVIDER_RUNTIME_FILE":
            pytest.fail("unsupported ordinary model discovered its runtime")
        return getter(name, default)

    monkeypatch.setattr(os.environ, "get", no_manifest)
    with pytest.raises(AdmissionDenied) as refused:
        build_gemini_runtime(selected)
    assert refused.value.reason == "backend_unsupported"


@pytest.mark.parametrize("model", selectable_models("gemini"))
def test_every_agent_selectable_model_composes_an_uncapped_host_turn(model: str) -> None:
    """What the card offers is what the host runs: an agent's turn is uncapped."""
    from daimon.core.mux_backend import TurnRuntime
    from daimon.core.turn.gemini import (  # pyright: ignore[reportPrivateUsage]
        GeminiDeployment,
        GeminiJournal,
        _runtime_model,
    )
    from mux.contracts.ids import ModelRef
    from mux.contracts.resources import AgentSpec, EnvironmentSpec

    deployment = GeminiDeployment(
        "digest",
        1,
        "project",
        AgentSpec(name="agent", model=ModelRef(provider="gemini", id=model)),
        EnvironmentSpec(name="remote"),
    )
    runtime = TurnRuntime(
        transport_factory=lambda *_: None,
        journal=GeminiJournal(storage=None, state_store=None, deployment=deployment),  # pyright: ignore[reportArgumentType]
        usage_store=None,
    )
    assert _runtime_model(runtime) == model


@pytest.mark.parametrize("model", ["gemini-3.8-pro", "gemini-2.5-flash"])
async def test_an_unselectable_model_does_not_enable_an_ordinary_runtime(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
) -> None:
    # Ordinary turns run the selectable models (Flash, Flash-Lite, Argon); others,
    # Pro included, refuse before any runtime discovery.
    revision = selected.admission.backend_revision
    assert revision is not None and selected.worker is None
    changed = revision.model_copy(update={"model": model})
    selected = replace(selected, admission=replace(selected.admission, backend_revision=changed))
    getter = os.environ.get

    def no_manifest(name: str, default: str | None = None) -> str | None:
        if name == "DAIMON_TURN__PROVIDER_RUNTIME_FILE":
            pytest.fail("unsupported ordinary model discovered its runtime")
        return getter(name, default)

    monkeypatch.setattr(os.environ, "get", no_manifest)
    with pytest.raises(AdmissionDenied) as refused:
        build_gemini_runtime(selected)
    assert refused.value.reason == "backend_unsupported"


@pytest.mark.parametrize(
    "defect",
    ("bool", "zero", "above_max", "string", "sandbox", "sources", "network", "packages", "tools"),
)
async def test_capped_turn_refuses_invalid_controls_and_execution_before_key_access(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    defect: str,
) -> None:
    values = entry(selected, tmp_path)
    values.update(max_output_tokens=4096, environment={"name": "no-execution", "execution": "none"})
    if defect in ("bool", "zero", "above_max", "string"):
        values["max_output_tokens"] = {
            "bool": True,
            "zero": 0,
            "above_max": 65_537,
            "string": "4096",
        }[defect]
    elif defect == "tools":
        values["tools"] = [{"name": "code_execution", "kind": "builtin"}]
    else:
        env = dict(cast(dict[str, JsonValue], values["environment"]))
        if defect == "sandbox":
            env["execution"] = "hosted"
        elif defect == "sources":
            env["sources"] = [
                {
                    "kind": "repository",
                    "repository_url": "https://example.test/repo",
                    "target_path": "/workspace/repo",
                }
            ]
        elif defect == "network":
            env["network"] = {"mode": "disabled"}
        elif defect == "packages":
            env["packages"] = {"pip": ["package==1"]}
        values["environment"] = env
    manifest(monkeypatch, tmp_path / "manifest.json", [values])
    get = os.environ.get

    def key(name: str, default: str | None = None) -> str | None:
        if name == "N4_OFFLINE_GEMINI_KEY":
            pytest.fail("incompatible capped turn discovered its credential")
        return get(name, default)

    monkeypatch.setattr(os.environ, "get", key)
    with pytest.raises(AdmissionDenied) as refused:
        build_channel_runtime(selected)
    assert refused.value.reason == "backend_unsupported"
    assert not (tmp_path / "records.sqlite").exists()
    assert not (tmp_path / "holds.sqlite").exists()


async def test_output_cap_never_activates_a_named_option_a_worker(
    materialization_worker_request: tuple[_ClaimedWorker, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (selected, _, _, _, owned, calls), _ = materialization_worker_request
    values = json.loads(owned.manifest_path.read_text())[0]
    values["max_output_tokens"] = 4096
    owned.manifest_path.write_text(json.dumps([values]))
    plan = GeminiPlan.model_validate(values)
    get = os.environ.get

    def key(name: str, default: str | None = None) -> str | None:
        if name == plan.api_key_env:
            pytest.fail("capped worker discovered its credential")
        return get(name, default)

    monkeypatch.setattr(os.environ, "get", key)
    with pytest.raises(AdmissionDenied) as refused:
        build_gemini_runtime(selected)
    assert refused.value.reason == "backend_unsupported"
    assert owned.wire.requests == [] and calls == []


def test_output_cap_omission_preserves_existing_plan_serialization(
    selected: ProviderPreparationRequest,
    tmp_path: Path,
) -> None:
    values = entry(selected, tmp_path)
    default = GeminiPlan.model_validate(values)
    omitted = GeminiPlan.model_validate({**values, "max_output_tokens": None})
    assert default.max_output_tokens is None
    assert default.model_dump_json() == omitted.model_dump_json()
    assert "max_output_tokens" not in json.loads(default.model_dump_json())


@pytest.mark.parametrize("host", ["discord", "mcp"])
async def test_normal_fleet_factories_load_real_installed_native_tariffs(
    db_committing_engine, db_schema, monkeypatch, host
):
    from daimon.adapters.discord import runtime as discord_runtime
    from daimon.adapters.mcp.server import create_mcp_app
    from daimon.core.config import Settings
    from daimon.core.turn import runtimes
    from daimon.testing.asgi import asgi_lifespan
    from daimon.testing.db import build_test_engine
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    from google.genai import Client
    from mux.testing.drivers.anthropic.ma import build_stub_anthropic
    from openai import AsyncOpenAI

    settings = Settings.model_validate(
        {
            "database": {
                "url": db_committing_engine.url.render_as_string(hide_password=False),
                "pool_size": 1,
                "max_overflow": 3,
            },
            "anthropic": {"api_key": "offline"},
            "crypto": {"allow_plaintext": True},
            "defaults_root": Path("defaults"),
            "turn": {"path": "mux", "channel_backends": True, "agent_backends": True},
            "fleet": {"enabled": True},
            "mcp": {"public_url": "https://offline.invalid/mcp"},
        }
    )
    client = build_stub_anthropic()

    def forbidden(*args, **kwargs):
        raise AssertionError("cold native registration constructed a provider client")

    monkeypatch.setattr(Client, "__init__", forbidden)
    monkeypatch.setattr(AsyncOpenAI, "__init__", forbidden)
    monkeypatch.setattr(runtimes, "_RUNTIME_FACTORIES", {})
    expected = dict(pricing.provider_prices(PROFILE)) | dict(
        pricing.provider_prices("openai.persistent_workspace")
    )
    from daimon.core.fleet_orchestration import HostFleetPorts

    owners = []
    construct = HostFleetPorts.__init__

    def observe(owner, *args, **kwargs):
        construct(owner, *args, **kwargs)
        owners.append(owner)

    monkeypatch.setattr(HostFleetPorts, "__init__", observe)

    def verify(owner):
        for key in (("gemini", "gemini-3.5-flash-lite"), ("openai", "gpt-6-luna")):
            if owner.prices.get(key) is not expected[key]:
                raise RuntimeError("normal deployed fleet lost the authoritative tariff")
        for key in (("gemini", "gemini-flash-latest"), ("openai", "unknown-model")):
            if key in owner.prices:
                raise RuntimeError("normal deployed fleet fabricated a model tariff")
        if dict(runtimes.channel_runtime_prices()) != expected:
            raise RuntimeError("installed runtime callbacks did not supply authoritative prices")

    if host == "discord":
        monkeypatch.setattr(discord_runtime, "AsyncAnthropic", lambda **kwargs: client)
        monkeypatch.setattr(
            discord_runtime,
            "build_engine",
            lambda url, **kwargs: build_test_engine(url, db_schema, **kwargs),
        )
        async with discord_runtime.build_runtime(settings) as runtime:
            verify(runtime.fleet_card_factory._service._owner())
    else:
        async with client:
            app = create_mcp_app(
                settings=settings,
                sessionmaker=async_sessionmaker(db_committing_engine, expire_on_commit=False),
                auth=StaticTokenVerifier(tokens={}),
                anthropic=client,
            )
            async with asgi_lifespan(app):
                services = await app.state.mcp.local_provider.list_tools()
                assert "fleet_spawn" in {tool.name for tool in services}
                assert len(owners) == 1
                verify(owners[0])


@pytest.mark.parametrize(
    "provider,model", [("gemini", "gemini-3.5-flash-lite"), ("openai", "gpt-6-luna")]
)
@pytest.mark.parametrize("priced", [False, True])
async def test_real_financial_admission_requires_the_owner_native_model_row(
    db_committing_engine, db_clean, provider, model, priced
):
    from daimon.core.stores import fleet
    from mux.profiles import backend_selection
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from ..test_fleet_budget import add, reserve, root, setup

    sm = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    original = root(provider)
    choice = backend_selection(provider).model_copy(update={"model": model})
    ref = ModelRef(provider=provider, id=model)
    admitted = replace(
        original,
        spec=original.spec.model_copy(update={"model": ref}),
        models=(ref,),
        backend_grants=(choice,),
    )
    admitted, run = await setup(sm, parent=admitted)
    worker = await add(sm, admitted)
    rates = pricing.provider_prices(choice.profile) if priced else {}
    decision = await reserve(sm, admitted, worker, prices=rates)
    if decision.admitted is not priced:
        raise RuntimeError(
            f"native tariff presence did not control financial admission: {decision.reason}"
        )
    current, (stored,) = await fleet.run_status(sm, tenant_id=run.tenant_id, run_id=run.id)
    if not priced and (current.reserved_usd != 0 or stored.reserved_usd != 0):
        raise RuntimeError("missing tariff changed a reservation")


async def test_openai_selected_tariff_matches_real_default_direct_session_request(
    application, monkeypatch, tmp_path
):
    from daimon.core.turn.openai_host import driver

    from .test_openai_deployment import entry as openai_entry
    from .test_openai_host import Wire
    from .test_openai_runtime_transport import clients_for

    manifest(monkeypatch, tmp_path / "runtime.json", [openai_entry()])
    monkeypatch.setenv("N4_OFFLINE_OPENAI_KEY", "offline")
    wire = Wire()
    # The OpenAI profile has no native session cap: the row's limit isn't sent.
    wire.expected_spend_limit = None
    clients = clients_for(monkeypatch, wire.handle)
    runtime = build_channel_runtime(application)
    config = application.admission.backend_revision
    assert config is not None
    assert runtime.price is pricing.provider_prices(config.profile)[("openai", config.model)]
    backend = driver(runtime, config, application.scope)
    await backend.sessions.create(
        application.scope,
        await runtime.session_plan(application),
        key="openai:prepare:standard-session",
    )
    posts = [request for request in wire.requests if request.method == "POST"]
    assert len(posts) == 1
    request = posts[0]
    assert request.url.scheme == "https" and request.url.host == "api.openai.com"
    assert request.url.path == "/v1/agents/sessions" and not request.url.query
    body = json.loads(request.content)
    assert body["agent"]["model"] == config.model
    assert "service_tier" not in body and "service_tier" not in body["agent"]
    assert all(client.is_closed() for client in clients)


@pytest.mark.parametrize("storage_backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("journal_owner", ["injected", "implicit"])
@pytest.mark.parametrize("explicit_worker_request", [True], indirect=True)
async def test_capped_worker_materialization_runs_through_headless_preparation_and_sdk(
    materialization_worker_request,
    storage_backend,
    journal_owner,
    monkeypatch,
):
    from daimon.core.headless_runner import run_worker

    (selected, root, worker, prices, owned, calls), _ = materialization_worker_request
    if storage_backend == "postgres":
        values = json.loads(owned.manifest_path.read_text())[0]
        values.pop("records_path")
        values.pop("holds_path")
        values["storage_backend"] = "postgres"
        monkeypatch.setenv("K_SERVICE", "configured-mcp-host")
        owned.manifest_path.write_text(json.dumps([values]))
    if journal_owner == "implicit":
        selected = replace(selected, deps=replace(selected.deps, state_store=None))
    assert selected.worker.limits.max_input_tokens == 16384
    result = await run_worker(
        selected,
        parent=root,
        worker=worker,
        trigger_message="Complete the assigned task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded" and result.turns_used == 1
    counts = [r for r in calls if r.url.path.endswith(":countTokens")]
    inputs = [r for r in calls if r.url.path.endswith("/interactions") and r.method == "POST"]
    assert len(counts) == len(inputs) == 1
    sent = json.loads(inputs[0].content)
    assert sent["model"] == "gemini-3.8-flash"
    assert sent["system_instruction"] == selected.worker.spec.system
    assert sent["generation_config"] == {"max_output_tokens": 8192}
    assert not {"agent", "agent_config", "environment", "previous_interaction_id"} & sent.keys()
    assert len([r for r in owned.wire.requests if r.method == "POST"]) == 1

    from daimon.core.fleet import FleetConflict

    with pytest.raises(FleetConflict):
        await run_worker(
            selected,
            parent=root,
            worker=result,
            trigger_message="Continue",
            prices=prices,
            cancel=asyncio.Event(),
        )
    assert (
        len([r for r in calls if r.url.path.endswith("/interactions") and r.method == "POST"]) == 1
    )


@pytest.mark.parametrize("explicit_worker_request", [False, True], indirect=True)
async def test_worker_cancelled_before_binding_records_its_cancel_without_native_io(
    materialization_worker_request,
    monkeypatch,
):
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores import fleet

    (selected, root, worker, prices, owned, calls), _ = materialization_worker_request
    # Missing runtime cannot be the binding error if cancellation wins first.
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", raising=False)
    cancelled = asyncio.Event()
    cancelled.set()
    before = len(calls), len(owned.wire.requests)
    with pytest.raises(asyncio.CancelledError):
        await run_worker(
            selected,
            parent=root,
            worker=worker,
            prices=prices,
            trigger_message="Complete the assigned task",
            cancel=cancelled,
        )
    _, rows = await fleet.run_status(
        selected.deps.sessionmaker, tenant_id=worker.tenant_id, run_id=worker.run_id
    )
    ended = next(row for row in rows if row.id == worker.id)
    # A cancel from outside is never a refusal (#1413 review B3).
    assert ended.status == "failed" and ended.error_code == "watch_ended"
    assert ended.binding_id is None and ended.turns_used == 0 and ended.attempts == 0
    assert (len(calls), len(owned.wire.requests)) == before


async def test_postgres_plan_is_explicit_and_refuses_ambiguous_storage(selected, tmp_path):
    from pydantic import ValidationError

    values = entry(selected, tmp_path)
    sqlite = GeminiPlan.model_validate(values)
    assert sqlite.storage_backend == "sqlite"
    assert "storage_backend" not in sqlite.model_dump(mode="json")
    postgres = dict(values, storage_backend="postgres")
    with pytest.raises(ValidationError, match="silently replace"):
        GeminiPlan.model_validate(postgres)
    postgres.pop("records_path")
    postgres.pop("holds_path")
    plan = GeminiPlan.model_validate(postgres)
    assert plan.records_path is None and plan.holds_path is None
    assert plan.model_dump(mode="json")["storage_backend"] == "postgres"
    for changes in ({"storage_backend": "unknown"}, {"storage_backend": "sqlite"}):
        with pytest.raises(ValidationError):
            GeminiPlan.model_validate(postgres | changes)


@pytest.mark.parametrize("marker", ["K_SERVICE", "CLOUD_RUN_JOB"])
async def test_cloud_run_sqlite_refuses_before_key_discovery(
    selected, tmp_path, monkeypatch, marker
):
    values = entry(selected, tmp_path)
    manifest(monkeypatch, tmp_path / "runtime.json", [values])
    monkeypatch.setenv(marker, "configured-host")
    original = os.environ.get

    def protected(name, default=None):
        if name == values["api_key_env"]:
            pytest.fail("ephemeral SQLite deployment discovered provider credential")
        return original(name, default)

    monkeypatch.setattr(os.environ, "get", protected)
    with pytest.raises(AdmissionDenied):
        build_gemini_runtime(selected)


@pytest.mark.parametrize("journal_owner", ["memory", "foreign_postgres"])
@pytest.mark.parametrize("explicit_worker_request", [True], indirect=True)
async def test_hosted_gemini_worker_refuses_a_foreign_journal_before_provider_io(
    materialization_worker_request, journal_owner, monkeypatch
):
    from daimon.core.fleet import FleetConflict
    from daimon.core.headless_runner import run_worker
    from daimon.core.stores.mux_state import PostgresStateStore
    from mux.state.memory import MemoryStateStore
    from sqlalchemy.ext.asyncio import async_sessionmaker

    (selected, root, worker, prices, owned, calls), _ = materialization_worker_request
    values = json.loads(owned.manifest_path.read_text())[0]
    values.pop("records_path")
    values.pop("holds_path")
    values["storage_backend"] = "postgres"
    monkeypatch.setenv("K_SERVICE", "configured-mcp-host")
    owned.manifest_path.write_text(json.dumps([values]))
    foreign = (
        MemoryStateStore()
        if journal_owner == "memory"
        else PostgresStateStore(async_sessionmaker())
    )
    selected = replace(selected, deps=replace(selected.deps, state_store=foreign))
    before = len(calls), len(owned.wire.requests)
    with pytest.raises(FleetConflict) as exc:
        await run_worker(
            selected,
            parent=root,
            worker=worker,
            prices=prices,
            trigger_message="Complete the assigned task",
            cancel=asyncio.Event(),
        )
    assert exc.value.reason == "journal_owner_mismatch"
    assert (len(calls), len(owned.wire.requests)) == before


@pytest.mark.parametrize("platform", ["teams", "discord", "slack"])
async def test_a_new_gemini_thread_sends_its_platforms_system_prompt_with_fragments_intact(
    platform: str,
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_clean: None,
) -> None:
    """The inline agent is created with this platform's variant of the plan's system, and
    the tenant's context-fragment block after it is sent byte for byte."""
    import re

    import yaml
    from daimon.core.agent_guidance import apply_credential_guidance
    from daimon.core.context_prompt import FRAGMENT_MARKER, ContextFragment, encode_fragments

    from .test_gemini_codec import interaction
    from .test_gemini_runtime_transport import clients_for

    config = selected.admission.backend_revision
    assert config is not None
    moved = ConfigRevision.create(
        config.channel.model_copy(update={"platform": platform}),
        1,
        resolve_default(BackendConfig(backend="gemini", profile=PROFILE, model=FLASH_PRIMARY)),
    )
    selected = replace(
        selected,
        platform=platform,
        admission=replace(selected.admission, backend_revision=moved, backend=check_backend(moved)),
    )
    defaults = Path(__file__).resolve().parents[4] / "defaults" / "agents" / "daimon.yaml"
    shipped = apply_credential_guidance(yaml.safe_load(defaults.read_text())["system"])
    other = "Slack" if platform != "slack" else "Discord"
    system = encode_fragments(
        shipped,
        {"routine": ContextFragment(text=f"Post the summary to {other} first.", mode="extend")},
    )
    plan = entry(selected, tmp_path)
    plan["system"] = system
    manifest(monkeypatch, tmp_path / "manifest.json", [plan])
    monkeypatch.setenv("N4_OFFLINE_GEMINI_KEY", "offline-selected-key")
    posts: list[dict[str, JsonValue]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content))
        return httpx.Response(200, json=interaction("one"))

    clients_for(monkeypatch, handle)
    async with selected.deps.sessionmaker() as db, db.begin():
        await make_tenant(db, id=selected.tenant_id)
    deps = replace(selected.deps, state_store=None)
    prepared = await bind_session_impl(
        deps,
        selected.admission,
        tenant_id=selected.tenant_id,
        platform=platform,
        external_user_id=selected.external_user_id,
        thread_id=selected.thread_id,
        session_account_id=selected.session_account_id,
        reuse_existing=True,
    )

    async def reseed() -> str:
        pytest.fail("Gemini turn used Anthropic recovery")

    await run_prepared_turn(
        deps,
        prepared,
        tenant_id=selected.tenant_id,
        platform=platform,
        thread_id=selected.thread_id,
        external_user_id=selected.external_user_id,
        user_message="question",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        operation_key="offline-gemini:platform-system",
    )

    sent = cast(str, posts[0]["system_instruction"])
    head, marker, fragments = sent.partition(FRAGMENT_MARKER)
    assert marker == FRAGMENT_MARKER
    assert fragments == system.partition(FRAGMENT_MARKER)[2], "the fragment block is intact"
    assert f"Post the summary to {other} first." in fragments
    foreign = "|".join(p for p in ("discord", "slack", "teams") if p != platform)
    assert re.search(foreign, head, re.IGNORECASE) is None
    assert head != shipped


async def test_fleet_r7_plain_language_gemini_worker_default_deadline_is_admitted(
    materialization_worker_request: tuple[_ClaimedWorker, str],
) -> None:
    """r7: a run's default deadline past Gemini's cap refused every plain Gemini worker."""
    from datetime import timedelta
    from decimal import Decimal

    from daimon.core.neutral_resources import (
        WorkerLimits,
        default_worker_limits,
        worker_runtime_ceiling,
    )
    from daimon.core.turn.runtime_manifest import capture_preparation_source

    (selected, root, _, _, _, _), _ = materialization_worker_request
    now = selected.now()
    run_ceiling = WorkerLimits(
        budget_usd=Decimal("1"), deadline=now + timedelta(minutes=20), max_turns=1
    )
    with pytest.raises(AdmissionDenied):
        build_gemini_runtime(replace(selected, deadline=run_ceiling.deadline))
    assert worker_runtime_ceiling(replace(root, max_worker_seconds=None), "gemini") == 600
    limits = default_worker_limits(
        run_ceiling,
        None,
        (),
        provider="gemini",
        budget_usd=Decimal("0.49"),
        now=now,
        max_runtime_s=worker_runtime_ceiling(replace(root, max_worker_seconds=None), "gemini"),
    )
    assert limits.deadline == now + timedelta(minutes=10)
    source = await capture_preparation_source(selected)

    async def forbidden() -> None:
        raise AssertionError("building the runtime invoked its financial guard")

    runtime = build_gemini_runtime(
        replace(selected, source=source, before_native=forbidden, deadline=limits.deadline)
    )
    assert runtime.materialize_worker is not None


@pytest.mark.parametrize("explicit_worker_request", ["derived"], indirect=True)
async def test_fleet_r9_plain_gemini_worker_with_the_real_root_prompt_takes_the_bounded_path(
    materialization_worker_request,
):
    """r9: a plain-language Gemini worker inherited a ~24k-char root prompt and got the
    unbounded 4096-token agent, which Google refused (HTTP 400). Derived limits keep it
    on the bounded path, inside its budget and the deployment's $0.49 per-turn hold."""
    from decimal import Decimal

    from daimon.core.headless_runner import run_worker
    from daimon.core.neutral_resources import gemini_worker_cost_usd

    (selected, root, worker, prices, owned, calls), _ = materialization_worker_request
    limits, system = selected.worker.limits, selected.worker.spec.system
    assert len(system) >= 24_000
    assert limits.max_input_tokens == len(system.encode()) + 4096
    worst = gemini_worker_cost_usd(
        selected.worker.spec.model.id, limits.max_input_tokens, limits.max_output_tokens
    )
    assert worst <= limits.budget_usd and worst <= Decimal("0.49")
    result = await run_worker(
        selected,
        parent=root,
        worker=worker,
        trigger_message="Complete the assigned task",
        prices=prices,
        cancel=asyncio.Event(),
    )
    assert result.status == "succeeded"
    inputs = [r for r in calls if r.url.path.endswith("/interactions") and r.method == "POST"]
    assert len(inputs) == 1
    sent = json.loads(inputs[0].content)
    assert sent["system_instruction"] == system
    assert sent["generation_config"] == {"max_output_tokens": limits.max_output_tokens}
    # No managed agent, so no 4096-token total cap for the prompt to overflow.
    assert not {"agent", "agent_config", "environment"} & sent.keys()


async def test_a_used_up_daily_model_cap_refuses_before_google_is_called(
    selected: ProviderPreparationRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    db_clean: None,
) -> None:
    """Gemini usage reaches no ledger debit; the daily model cap stops the turn first."""
    import httpx

    from .test_gemini_runtime_transport import clients_for

    manifest(monkeypatch, tmp_path / "manifest.json", [entry(selected, tmp_path)])
    monkeypatch.setenv("N4_OFFLINE_GEMINI_KEY", "offline-selected-key")
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)

    clients_for(monkeypatch, handle)
    async with selected.deps.sessionmaker() as db, db.begin():
        await make_tenant(db, id=selected.tenant_id)
    deps = replace(selected.deps, state_store=None, gemini_daily_model_cap_usd=Decimal(0))

    with pytest.raises(AdmissionDenied) as refused:
        await bind_session_impl(
            deps,
            selected.admission,
            tenant_id=selected.tenant_id,
            platform=selected.platform,
            external_user_id=selected.external_user_id,
            thread_id=selected.thread_id,
            session_account_id=selected.session_account_id,
            reuse_existing=True,
        )
    model = selected.admission.backend_revision.model
    assert (refused.value.reason, refused.value.provider) == ("provider_daily_cap_reached", model)
    assert not [call for call in calls if call.method == "POST"], "nothing was created"
