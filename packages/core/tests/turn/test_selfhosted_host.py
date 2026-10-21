"""Real self-hosted host turns, with a C3 HTTP fake and fenced persistence."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import httpx
import pytest
from daimon.core.pricing import ProviderPrice
from daimon.core.turn import selfhosted_host
from daimon.core.turn.prepare import ProviderPreparationRequest, bind_session_impl
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn.selfhosted_host import SelfHostedHostRuntime, prepare_selfhosted
from daimon.testing.turn_fakes import RecordingLifecycle, deps_for
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ModelRef, ResourceRef, Revision
from mux.contracts.ports import Agents, Environments
from mux.contracts.resources import Agent, AgentSpec, Environment, EnvironmentSpec, SessionSpec
from mux.drivers.selfhosted.transport import DirectTransport
from mux.state.memory import MemoryStateStore

from .conformance_support import ACCOUNT, TENANT, admission
from .test_provider_dispatch import SCOPE

PROFILE = "selfhosted.loop"
BASE = admission().backend_revision
REVISION = ConfigRevision.create(
    BASE.channel,
    1,
    resolve_default(BackendConfig(backend="selfhosted", profile=PROFILE, model="gpt-6-luna")),
)


class Wire:
    def __init__(self):
        self.events = []
        self.turns = {}
        self.posts = 0
        self.unmatched_start = False

    def handle(self, request):
        assert request.headers["authorization"] == "Bearer offline"
        if request.method == "GET":
            after = int(
                request.url.params.get("replay_after_seq", request.url.params.get("after_seq", "0"))
            )
            return httpx.Response(
                200, json={"events": [e for e in self.events if e["seq"] > after], "complete": True}
            )
        body = json.loads(request.content)
        if body["idempotency_key"] not in self.turns:
            self.turns[body["idempotency_key"]] = body
            self.posts += 1
            events = [
                ("status_running", {}),
                (
                    "usage",
                    {
                        "request_id": f"request-{self.posts}",
                        "model": "gpt-6-luna",
                        "input_tokens": 10,
                        "cached_input_tokens": 0,
                        "output_tokens": 5,
                        "reasoning_tokens": 0,
                    },
                ),
                (
                    "agent_message",
                    {
                        "message_id": f"message-{self.posts}",
                        "parts": [{"type": "text", "text": f"answer {self.posts}"}],
                    },
                ),
                ("turn_ended", {"outcome": "completed"}),
            ]
            if self.unmatched_start:
                events[1] = (
                    "model_request_started",
                    {"request_id": f"request-{self.posts}", "model": "gpt-6-luna"},
                )
                events[-1] = ("turn_ended", {"outcome": "interrupted"})
            for kind, data in events:
                self.events.append(
                    dict(
                        seq=len(self.events) + 1,
                        session_id=body["session_id"],
                        turn_id=body["turn_id"],
                        at=datetime.now(UTC).isoformat(),
                        type=kind,
                        data=data,
                    )
                )
        return httpx.Response(202, json={"accepted": True, "turn_id": body["turn_id"]})


def append_message_deltas(wire, count):
    template = wire.events[0]
    for _ in range(count):
        wire.events.append(
            {
                **template,
                "seq": len(wire.events) + 1,
                "type": "agent_message_delta",
                "data": {"message_id": "long-message", "text": "x"},
            }
        )


class Resources:
    def __init__(self, value):
        self.value = value

    async def retrieve(self, scope, ref):
        assert scope == SCOPE and ref == self.value.ref
        return self.value


@pytest.fixture(params=["memory", "postgres"])
async def composed(request, db_committing_engine, db_clean, monkeypatch):
    from daimon.core.stores.mux_state import PostgresStateStore
    from daimon.testing.factories import make_tenant
    from sqlalchemy.ext.asyncio import async_sessionmaker

    db_session_factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")

    async with db_session_factory() as db, db.begin():
        await make_tenant(db, id=TENANT)
    store = (
        MemoryStateStore() if request.param == "memory" else PostgresStateStore(db_session_factory)
    )
    wire = Wire()
    observations = []

    async def record(**kwargs):
        observations.append(kwargs)
        return False

    monkeypatch.setattr(selfhosted_host, "record_provider_usage", record)

    def ref(kind):
        return ResourceRef(
            id="native-" + kind,
            kind=kind,
            provider="selfhosted",
            account_scope_id=str(TENANT),
            tenant_id=SCOPE.tenant_id,
            account_id=None,
        )

    agents = cast(
        Agents,
        Resources(
            Agent(
                ref=ref("agent"),
                revision=Revision(local=1),
                created_at=datetime.now(UTC),
                spec=AgentSpec(name="test", model=ModelRef(provider="selfhosted", id="gpt-6-luna")),
            )
        ),
    )
    environments = cast(
        Environments,
        Resources(
            Environment(
                ref=ref("environment"),
                revision=Revision(local=1),
                created_at=datetime.now(UTC),
                spec=EnvironmentSpec(name="test", execution="self_hosted"),
            )
        ),
    )

    async def plan(preparation):
        return SessionSpec(
            agent=ref("agent"),
            agent_revision=Revision(local=1),
            environment=ref("environment"),
            config_revision=REVISION.local,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire.handle)) as client:
        runtime = SelfHostedHostRuntime(
            transport_factory=lambda config, scope: DirectTransport(
                client, base_url="https://harness", token="offline"
            ),
            journal=None,
            usage_store=None,
            account_scope_id=str(TENANT),
            session_plan=plan,
            authorization=lambda scope, kind, id_: scope == SCOPE,
            model="gpt-6-luna",
            price=ProviderPrice(
                provider="selfhosted",
                model="gpt-6-luna",
                checked_on=date(2026, 1, 1),
                input=Decimal(2),
                output=Decimal(10),
                cache_read=Decimal("0.5"),
            ),
            infrastructure_usd=Decimal(0),
            agents=agents,
            environments=environments,
        )
        deps = replace(
            deps_for(None, db_session_factory),
            channel_backends=True,
            state_store=store,
            turn_runtimes={PROFILE: runtime},
        )
        request = ProviderPreparationRequest(
            deps,
            replace(admission(), backend_revision=REVISION),
            SCOPE,
            TENANT,
            "slack",
            "caller",
            "thread",
            ACCOUNT,
            True,
            cast(object, None),
            None,
            datetime.now(UTC) + timedelta(seconds=20),
            lambda: datetime.now(UTC),
        )
        yield request, wire, store, observations


async def test_prepare_reuses_same_tenant_bound_session_without_a_turn(composed):
    request, wire, _, _ = composed
    first = await prepare_selfhosted(request)
    second = await prepare_selfhosted(request)
    assert first.session_ref == second.session_ref
    assert not first.reused and second.reused
    assert wire.posts == 0


@pytest.mark.parametrize("prior_observations", [0, 501])
async def test_two_real_host_turns_keep_roots_and_usage_separate(composed, prior_observations):
    from daimon.core.turn.outcomes import drain_outcomes

    request, wire, store, observations = composed
    finalized = []

    async def finalize():
        finalized.append(True)

    runtime = replace(
        request.deps.turn_runtimes[PROFILE],
        finalize=finalize,
        skipped_notice=lambda: "Daimon: skills unavailable here: docx (requires Anthropic).",
    )
    request = replace(request, deps=replace(request.deps, turn_runtimes={PROFILE: runtime}))
    for index in (1, 2):
        prepared = await bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=TENANT,
            platform="slack",
            external_user_id="caller",
            thread_id="thread",
            session_account_id=ACCOUNT,
            reuse_existing=True,
        )

        if index == 1:
            for old in range(prior_observations):
                wire.events.append(
                    dict(
                        seq=old + 1,
                        session_id=prepared.session_ref.id,
                        turn_id=f"old-{old}",
                        at=datetime.now(UTC).isoformat(),
                        type="usage",
                        data=dict(
                            request_id=f"old-{old}",
                            model="gpt-6-luna",
                            input_tokens=1,
                            cached_input_tokens=0,
                            output_tokens=1,
                            reasoning_tokens=0,
                        ),
                    )
                )

        async def reseed():
            return "question"

        outcome = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=TENANT,
            platform="slack",
            thread_id="thread",
            external_user_id="caller",
            user_message=f"question {index}",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            render_interval_s=0.01,
            operation_key=f"selfhosted-{index}",
        )
        await drain_outcomes()
        assert outcome.state.error is None
        assert f"answer {index}" in str(outcome.state.content)
        assert ("skills unavailable here" in str(outcome.state.content)) == (index == 1)
        assert len(finalized) == index
        if index == 2:
            assert "answer 1" not in str(outcome.state.content)
    assert wire.posts == 2
    assert observations
    assert len({row["observation"].id for row in observations}) == 2
    assert all(row["model_id"] == "gpt-6-luna" for row in observations)
    events = await store.read_events(prepared.session_ref.id)
    assert sum(e.type == "session.turn_ended" for e in events) == 2


@pytest.mark.parametrize("composed", ["postgres"], indirect=True)
async def test_unmatched_start_reaches_actual_recorder_as_pending_unknown(composed):
    from datetime import date
    from decimal import Decimal

    from daimon.core.config import SelfhostedBillingSettings, SelfhostedModelPrice
    from daimon.core.selfhosted_usage import admit_selfhosted_usage, record_selfhosted_usage
    from daimon.core.stores.accounting_outbox import list_pending_usage
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart

    request, wire, _, _ = composed
    billing = SelfhostedBillingSettings(
        deployments={"gpt-6-luna": "test-model"},
        models={
            "test-model": [
                SelfhostedModelPrice(
                    effective_from=date(2026, 1, 1), input="2", output="10", cache_read="0.5"
                )
            ]
        },
    )
    received = []

    async def record(observation, binding_id):
        received.append(observation)
        return await record_selfhosted_usage(
            sessionmaker=request.deps.sessionmaker,
            observation=observation,
            binding_id=binding_id,
            tenant_id=TENANT,
            platform_user_id="caller",
            admission_key="inventory-test",
            infrastructure_usd=Decimal(0),
        )

    runtime = replace(request.deps.turn_runtimes[PROFILE], record_usage=record)
    request = replace(request, deps=replace(request.deps, turn_runtimes={PROFILE: runtime}))
    prepared = await prepare_selfhosted(request)
    from daimon.core.stores.mux_state import PostgresStateStore
    from mux.contracts.ids import ThreadRef
    from mux.state.lease import Slot

    binding = await PostgresStateStore(request.deps.sessionmaker).get_binding(
        Slot(
            thread=ThreadRef(channel=REVISION.channel, thread_id=request.thread_id),
            account_id=SCOPE.account_id,
        )
    )
    assert binding is not None
    await admit_selfhosted_usage(
        sessionmaker=request.deps.sessionmaker,
        tenant_id=TENANT,
        binding_id=binding.id,
        session_ref=prepared.session_ref,
        admission_key="inventory-test",
        deployment="gpt-6-luna",
        billing=billing,
    )
    wire.unmatched_start = True
    await prepared.backend.events.send(
        SCOPE,
        prepared.session_ref,
        [UserMessage(content=(TextPart(text="start then crash"),))],
        key="inventory-test",
    )
    observations = await prepared.backend.usage.reconcile(SCOPE, prepared.session_ref)
    assert len(observations) == 1
    assert observations[0].completeness == "unknown"
    assert observations[0].input_tokens is None and observations[0].output_tokens is None
    assert not await prepared._record(observation=observations[0])
    assert received == list(observations)
    async with request.deps.sessionmaker() as session:
        pending = await list_pending_usage(session, tenant_id=TENANT)
    assert any(
        row.observation_id == observations[0].id and row.completeness == "unknown"
        for row in pending.rows
    )


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_turn_finalizer_runs_when_the_turn_raises(composed, monkeypatch, failure):
    from daimon.core.turn import run
    from daimon.core.turn.outcomes import drain_outcomes

    request, _, _, _ = composed
    finalized = []

    async def finalize():
        finalized.append(True)

    prepared = await prepare_selfhosted(request)
    prepared = replace(prepared, _finalize=finalize)

    async def fail(*args, **kwargs):
        raise failure()

    monkeypatch.setattr(run, "run_prepared_turn_impl", fail)
    with pytest.raises(failure):
        await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=TENANT,
            platform="slack",
            thread_id="thread",
            external_user_id="caller",
            user_message="question",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=fail,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        )
    await drain_outcomes()
    assert finalized == [True]


@pytest.mark.parametrize("composed", ["postgres"], indirect=True)
@pytest.mark.parametrize(
    "unmatched,drop_stream,scenario",
    [
        (False, False, "normal"),
        (False, True, "normal"),
        (True, False, "normal"),
        (True, True, "normal"),
        (False, False, "attack"),
        (False, False, "origin"),
        (False, False, "host_deny"),
        (False, False, "late_usage"),
        (False, False, "late_reconcile"),
        (False, False, "revoke_failure"),
        (False, False, "entra"),
        (False, False, "entra_refresh"),
        (False, False, "entra_attack"),
        (False, False, "entra_pool"),
        (False, False, "entra_pool_unbound"),
        (False, False, "entra_pool_origin"),
        (False, False, "ask_accept"),
        (False, False, "ask_deny"),
        (False, False, "ask_disabled"),
        (False, False, "mcp_ask_accept"),
        (False, False, "stored_deny"),
        (False, False, "proxy_default"),
        (False, False, "anthropic_disabled"),
        (False, False, "proxy_safety"),
        (False, False, "inspection_allow_chat"),
        (False, False, "inspection_allow_unattended"),
        (False, False, "inspection_auto_chat"),
        (False, False, "inspection_auto_unattended"),
        (False, False, "long_history"),
        (False, False, "retained_skills"),
    ],
)
async def test_production_shell_admits_before_http_records_and_revokes(
    composed, tmp_path, monkeypatch, unmatched, drop_stream, scenario
):
    import os
    import uuid

    import jwt
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.config import (
        McpCredentialBinding,
        McpSettings,
        SelfhostedBillingSettings,
        SelfhostedModelPrice,
    )
    from daimon.core.stores import mux_state
    from daimon.core.stores import selfhosted_usage as contexts
    from daimon.core.stores.access_policy import set_access_policy
    from daimon.core.stores.accounting_outbox import list_pending_usage
    from daimon.core.stores.mcp_tokens import get_mcp_token
    from daimon.core.stores.selfhosted_resources import SelfHostedAgents, SelfHostedEnvironments
    from daimon.core.stores.tenants import get_tenant, set_funding_mode
    from daimon.core.turn.admission import admit
    from daimon.core.turn.outcomes import drain_outcomes
    from daimon.core.turn.persistence import current_persistence
    from daimon.core.turn.runtimes import build_channel_runtime, ensure_channel_runtime
    from daimon.testing.factories import make_account, make_platform_principal, make_tenant_config
    from mux.contracts.extensions import ExtensionConfig
    from mux.contracts.ids import SkillRef, ThreadRef
    from mux.contracts.resources import MCPConnection, ToolSpec
    from mux.state.lease import Slot
    from pydantic import SecretStr

    from .test_selfhosted_admission import ForbiddenAnthropic

    request, wire, _, _ = composed
    async with request.deps.sessionmaker() as db, db.begin():
        tenant = await get_tenant(db, TENANT)
        assert tenant is not None
        account = await make_account(db, tenant=tenant, id=ACCOUNT)
        await make_platform_principal(
            db, tenant=tenant, account=account, platform="slack", external_id="caller"
        )
        await make_tenant_config(db, tenant=tenant, agent_name="test", environment_name="test")
        await set_funding_mode(db, tenant_id=TENANT, funding_mode="operator_funded")
        await set_access_policy(
            db,
            tenant_id=TENANT,
            policy=TenantAccessPolicy(
                enabled_backends=("anthropic", "selfhosted"),
            ),
        )
        await mux_state.put_config_revision(db, REVISION)
    attachment_scope = SCOPE
    if scenario == "retained_skills":
        from daimon.core.skills.ingest import bundle_from_files
        from daimon.core.stores.channel_skills import add_channel_skill
        from daimon.core.stores.neutral_catalog import PostgresNeutralCatalog

        catalog = PostgresNeutralCatalog(
            request.deps.sessionmaker, provider="selfhosted", account_scope_id=SCOPE.tenant_id
        )
        async with request.deps.sessionmaker() as db, db.begin():
            uploader = await make_account(db, tenant=tenant)
        attachment_scope = SCOPE.model_copy(
            update={"account_id": str(uploader.id), "principal_id": "uploader"}
        )
        for name, owner in (
            ("retained", attachment_scope),
            ("channel-upload", attachment_scope),
        ):
            for version in ("v1", "v2"):
                bundle = bundle_from_files(
                    {
                        "SKILL.md": (
                            f"---\nname: {name}\ndescription: Retained context.\n---\n{version}"
                        ).encode(),
                        "asset.bin": version.encode(),
                    }
                )
                await catalog.put_skill(
                    owner,
                    skill_id=name,
                    version=version,
                    agent_name="test",
                    display_title=name,
                    bundle=bundle,
                )
        async with request.deps.sessionmaker() as db, db.begin():
            await add_channel_skill(
                db,
                tenant_id=TENANT,
                platform=request.platform,
                channel_id=REVISION.channel.channel_id,
                skill_id="channel-upload",
                version="v1",
                name="channel-upload",
                owner_agent_name="test",
                actor_account_id=uploader.id,
            )
    agent = await SelfHostedAgents(request.deps.sessionmaker).create(
        attachment_scope,
        AgentSpec(
            name="test",
            model=ModelRef(provider="selfhosted", id="gpt-6-luna"),
            skills=(SkillRef(id="retained", version="v1"),)
            if scenario == "retained_skills"
            else None,
            tools=(
                ToolSpec(name="write", kind="builtin", permission="ask"),
                ToolSpec(
                    name="reports",
                    kind="mcp_toolset",
                    permission="auto" if scenario.startswith("inspection") else "ask",
                ),
            ),
            mcp_servers=(
                MCPConnection(
                    name="reports",
                    url="https://reports.example/mcp",
                    credential_ref="HARNESS_MCP_CRED_REPORTS",
                    tool_policy={"allowed_tools": ["summarize", "get_report"]},
                ),
            ),
        ),
        key="production-agent",
    )
    pool = scenario.startswith("entra_pool")
    environment = await SelfHostedEnvironments(request.deps.sessionmaker).create(
        SCOPE,
        EnvironmentSpec(
            name="test",
            execution="self_hosted",
            native_config=ExtensionConfig(
                namespace="selfhosted.environment",
                version=1,
                value={
                    "session_pool": pool,
                    "harness_url": "https://pool.example"
                    if pool
                    else (
                        "https://attacker.example"
                        if scenario in ("attack", "origin", "entra_attack")
                        else "https://harness"
                    ),
                    "auth_mode": "entra" if scenario.startswith("entra") else "bearer_env",
                    "auth_ref": "https://dynamicsessions.io"
                    if pool
                    else (
                        "DAIMON_MCP__JWT_SECRET"
                        if scenario == "attack"
                        else "https://management.azure.com"
                        if scenario == "entra_attack"
                        else "https://harness"
                        if scenario.startswith("entra")
                        else "DAIMON_SELFHOSTED__HARNESS_TOKEN_TEST"
                    ),
                },
            ),
        ),
        key="production-environment",
    )
    request = replace(
        request,
        admission=replace(
            request.admission,
            agent=request.admission.agent.model_copy(update={"id": agent.ref.id}),
            environment=request.admission.environment.model_copy(update={"id": environment.ref.id}),
        ),
        deps=replace(
            request.deps,
            anthropic=ForbiddenAnthropic(),
            public_url="https://mcp.example/mcp",
            mcp=McpSettings(
                public_url="https://mcp.example/mcp",
                jwt_secret=SecretStr("offline-test-signing-material-32-bytes"),
            ),
        ),
    )
    billing = SelfhostedBillingSettings(
        deployments={"gpt-6-luna": "test-model"},
        models={
            "test-model": [
                SelfhostedModelPrice(
                    effective_from=date(2026, 1, 1), input="2", output="10", cache_read="0.5"
                )
            ]
        },
    )
    monkeypatch.setenv("DAIMON_DATABASE__URL", os.environ["DAIMON_DATABASE__TEST_URL"])
    monkeypatch.setenv("DAIMON_BILLING__SELFHOSTED", billing.model_dump_json())
    monkeypatch.setenv(
        "DAIMON_SELFHOSTED__MCP_CREDENTIAL_BINDINGS",
        json.dumps(
            [
                McpCredentialBinding(
                    name="HARNESS_MCP_CRED_REPORTS",
                    origin="https://reports.example",
                    tenants=(TENANT,),
                ).model_dump(mode="json")
            ]
        ),
    )
    monkeypatch.setenv(
        "DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS",
        json.dumps(
            []
            if scenario == "entra_pool_unbound"
            else [
                {
                    "auth_mode": "entra" if scenario.startswith("entra") else "bearer_env",
                    "name": "https://dynamicsessions.io"
                    if pool
                    else "https://harness"
                    if scenario.startswith("entra")
                    else "DAIMON_SELFHOSTED__HARNESS_TOKEN_TEST",
                    "origin": "https://pool.example"
                    if pool and scenario != "entra_pool_origin"
                    else "https://harness",
                    "tenants": [str(TENANT)],
                }
            ]
        ),
    )
    path = tmp_path / "runtime.json"
    path.write_text(
        json.dumps(
            [
                dict(
                    profile=PROFILE,
                    tenant_id=SCOPE.tenant_id,
                    platform="slack",
                    channel_id=REVISION.channel.channel_id,
                    account_id=SCOPE.account_id,
                    config_digest=REVISION.content_digest(),
                    agent_id=agent.ref.id,
                    environment_id=environment.ref.id,
                    infrastructure_usd="0",
                )
            ]
        )
    )
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    monkeypatch.setenv("DAIMON_SELFHOSTED__HARNESS_TOKEN_TEST", "offline")
    # Supply the tempting secret too: refusal must come from destination
    # authority, rather than from the referenced variable being absent.
    monkeypatch.setenv("DAIMON_MCP__JWT_SECRET", "offline-test-signing-material-32-bytes")
    for name in (
        "ANTHROPIC_API_KEY",
        "DAIMON_ANTHROPIC__API_KEY",
        "OPENAI_API_KEY",
        "DAIMON_OPENAI__API_KEY",
        "AZURE_OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    requests, admitted_keys, token_ids = [], [], []
    token_calls, credentials, clients = [], [], []
    if scenario.startswith("entra"):
        import time

        from azure.core.credentials import AccessToken

        class Credential:
            def __init__(self):
                credentials.append(self)

            async def get_token(self, *scopes):
                token_calls.append(scopes)
                return AccessToken(
                    "offline",
                    int(time.time())
                    + (30 if scenario == "entra_refresh" and len(token_calls) == 1 else 3600),
                )

            async def close(self):
                pass

        monkeypatch.setattr("azure.identity.aio.DefaultAzureCredential", Credential)
    if scenario == "revoke_failure":

        async def fail_revoke(*args, **kwargs):
            raise RuntimeError("offline revoke failure")

        monkeypatch.setattr("daimon.core.stores.mcp_tokens.revoke_mcp_token", fail_revoke)
    if scenario == "host_deny":
        from daimon.core.tool_safety import ToolSafetyPolicy

        request = replace(
            request,
            deps=replace(
                request.deps,
                tool_safety=ToolSafetyPolicy(enabled=True, denied=("reports/delete_report",)),
            ),
        )
    approval_case = scenario in (
        "ask_accept",
        "ask_deny",
        "ask_disabled",
        "mcp_ask_accept",
        "stored_deny",
        "proxy_safety",
    ) or scenario.startswith("inspection")
    if approval_case and scenario != "ask_disabled":
        from daimon.core.tool_safety import ToolSafetyPolicy

        request = replace(
            request,
            deps=replace(
                request.deps, tool_safety=ToolSafetyPolicy(enabled=True, agent_rules=True)
            ),
        )
    if scenario == "stored_deny" or scenario.startswith("inspection_allow"):
        from daimon.core.authz import Subject
        from daimon.core.stores.agent_tool_rules import set_rule

        async with request.deps.sessionmaker() as db, db.begin():
            await set_rule(
                db,
                tenant_id=TENANT,
                agent_id=agent.ref.id,
                agent_name="test",
                tool_key="reports/get_report",
                server_url="https://reports.example/mcp",
                rule="deny" if scenario == "stored_deny" else "allow",
                scope="chat_and_routines",
                set_by="admin",
                actor=Subject(is_admin=True),
            )
    cards, actions, executions = [], [], []

    async def confirm(prompt):
        cards.append(prompt)
        return "denied" if scenario == "ask_deny" else "approved"

    late = scenario in ("late_usage", "late_reconcile")
    deferred = []
    cancellations = []

    original_http = httpx.AsyncClient.request

    async def http(self, method, url, **kwargs):
        if isinstance(self._transport, httpx.ASGITransport):
            return await original_http(self, method, url, **kwargs)
        call = httpx.Request(method, url, json=kwargs.get("json"), headers=kwargs["headers"])
        requests.append(call)
        if pool:
            from mux.drivers.selfhosted.transport import sandbox_id

            if method == "POST" and call.url.path == "/v0/turns":
                routed_session = json.loads(call.content)["session_id"]
            elif call.url.path.startswith("/v0/sessions/"):
                routed_session = call.url.path.split("/")[3]
            else:
                routed_session = wire.events[0]["session_id"]
            assert call.url.params["identifier"] == sandbox_id(routed_session)
            assert call.url.host == "pool.example"
            assert call.url.params["session_id"] == routed_session
            assert int(call.url.params["after_seq"]) >= 0
            if "replay_after_seq" in call.url.params:
                assert int(call.url.params["replay_after_seq"]) <= int(call.url.params["after_seq"])
        clients.append(self)
        if call.url.path.endswith("/actions"):
            body = json.loads(call.content)
            actions.append(body)
            if body["decision"] == "allow":
                executions.append(body["action_id"])
            pending = deferred[:]
            deferred.clear()
            wire.events.extend(pending)
            return httpx.Response(200, json={"accepted": True}, request=call)
        if call.url.path.endswith("/cancel"):
            cancellations.append(call)
            if scenario == "late_usage":
                wire.events.extend(deferred)
                deferred.clear()
            return httpx.Response(200, json={"cancelled": True}, request=call)
        if method == "POST" and call.url.path == "/v0/turns":
            body = json.loads(call.content)
            active = current_persistence.get()
            assert active is not None
            key = "selfhosted:turn:" + body["turn_id"]
            from sqlalchemy import text

            async with request.deps.sessionmaker() as occupied, request.deps.sessionmaker() as db:
                writer_pid = await occupied.scalar(text("SELECT pg_backend_pid()"))
                reader_pid = await db.scalar(text("SELECT pg_backend_pid()"))
                assert writer_pid != reader_pid
                admission = await contexts.read_context(
                    db,
                    tenant_id=TENANT,
                    binding_id=active.binding.id,
                    key=contexts.context_key("admission", active.binding.id, key),
                )
            assert admission is not None and admission["admission_key"] == key
            admitted_keys.append(key)
            token = call.headers["X-Daimon-MCP-Authorization"].removeprefix("Bearer ")
            claims = jwt.decode(
                token,
                "offline-test-signing-material-32-bytes",
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
            assert claims["anthropic_disabled"] is True
            token_ids.append(uuid.UUID(claims["jti"]))
            if scenario == "anthropic_disabled":
                # Exercise the exact credential received by daimon-mcp while
                # the production turn is active, before finalizer revocation.
                from daimon.adapters.mcp.server import create_mcp_app
                from daimon.core.config import DatabaseSettings, Settings
                from daimon.testing.asgi import mcp_session

                app = create_mcp_app(
                    settings=Settings(
                        database=DatabaseSettings(url=os.environ["DAIMON_DATABASE__TEST_URL"]),
                        mcp=request.deps.mcp,
                        _env_file=None,
                    ),
                    sessionmaker=request.deps.sessionmaker,
                    anthropic=ForbiddenAnthropic(),
                )
                refused = await mcp_session(
                    app,
                    token=token,
                    method="tools/call",
                    params={
                        "name": "call_tool",
                        "arguments": {"name": "list_agents", "arguments": {}},
                    },
                )
                assert refused["result"]["isError"] is True
                assert "unavailable during a self-hosted turn" in json.dumps(refused)
                neutral = await mcp_session(
                    app,
                    token=token,
                    method="tools/call",
                    params={
                        "name": "call_tool",
                        "arguments": {"name": "now", "arguments": {}},
                    },
                )
                assert neutral["result"].get("isError", False) is False
                assert "content" in neutral["result"]
            assert token.encode() not in call.content
            assert body["model"] == "gpt-6-luna"
            if scenario == "retained_skills":
                skills = {skill["name"]: skill for skill in body["skills"]}
                assert set(skills) == {"retained", "channel-upload"}
                for skill in skills.values():
                    assert skill["files"]["asset.bin"] == "v1"
            assert {
                key: value
                for key, value in body["tool_policy"].items()
                if not key.startswith("daimon-mcp/")
            } == {
                **({"reports/delete_report": "deny"} if scenario == "host_deny" else {}),
                "bash": "deny",
                "read": "deny",
                "edit": "deny",
                "write": "ask",
                "reports/*": "deny",
                "reports/summarize": "ask",
                "reports/get_report": "ask",
            }
            assert body["mcp_servers"][1]["headers_env"] == {
                "Authorization": "HARNESS_MCP_CRED_REPORTS"
            }
        response = wire.handle(call)
        if (
            method == "POST"
            and call.url.path == "/v0/turns"
            and (approval_case or scenario == "proxy_default")
        ):
            body = json.loads(call.content)
            tool = (
                "reports/get_report"
                if scenario == "stored_deny" or scenario.startswith("inspection")
                else "reports/summarize"
                if scenario == "mcp_ask_accept"
                else "daimon-mcp/call_tool"
                if scenario.startswith("proxy")
                else "write"
            )
            if scenario == "proxy_default":
                assert body["tool_policy"].get(tool, "auto") == "auto"
                executions.append("neutral")
            else:
                assert body["tool_policy"].get(tool, "auto") == "ask"
                deferred.extend(wire.events[2:])
                wire.events[:] = wire.events[:2]
                wire.events.append(
                    dict(
                        seq=3,
                        session_id=body["session_id"],
                        turn_id=body["turn_id"],
                        at=datetime.now(UTC).isoformat(),
                        type="tool_use",
                        data=dict(
                            call_id="call-approval",
                            name=tool,
                            executor="mcp" if "/" in tool else "agent",
                            input={"name": "get_current_time", "arguments": {}}
                            if scenario.startswith("proxy")
                            else {"path": "result.txt"},
                        ),
                    )
                )
                wire.events.append(
                    dict(
                        seq=4,
                        session_id=body["session_id"],
                        turn_id=body["turn_id"],
                        at=datetime.now(UTC).isoformat(),
                        type="requires_action",
                        data=dict(
                            action_id="approval",
                            call_id="call-approval",
                            name=tool,
                            input={"name": "get_current_time", "arguments": {}}
                            if scenario.startswith("proxy")
                            else {"path": "result.txt"},
                        ),
                    )
                )
                for event in deferred:
                    event["seq"] += 2
        if late and method == "POST" and call.url.path == "/v0/turns":
            deferred.extend(wire.events[1:])
            measured = deferred[0]
            wire.events[:] = [
                wire.events[0],
                {
                    **measured,
                    "type": "model_request_started",
                    "data": {"request_id": "request-1", "model": "gpt-6-luna"},
                },
            ]
            for event in deferred:
                event["seq"] += 1
        if late and method == "GET":
            response = httpx.Response(200, json={"events": wire.events, "complete": not deferred})
        response.request = call
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", http)
    wire.unmatched_start = unmatched
    dropped = []
    if drop_stream:
        from daimon.core.turn.io import TurnConnectionLost
        from daimon.core.turn.selfhosted_io import _SelfHostedStream

        original_next = _SelfHostedStream.__anext__

        async def disconnect_after_record(stream):
            event = await original_next(stream)
            if not dropped:
                dropped.append(True)
                await stream.close()
                raise TurnConnectionLost("offline reader dropped after first durable record")
            return event

        monkeypatch.setattr(_SelfHostedStream, "__anext__", disconnect_after_record)
    assert ensure_channel_runtime(PROFILE)
    admitted = await admit(
        request.deps,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        channel_id=REVISION.channel.channel_id,
        thread_id=request.thread_id,
        now=request.now(),
    )
    assert admitted.grant is not None and admitted.backend_revision == REVISION
    assert admitted.agent.id == agent.ref.id and admitted.account_id == ACCOUNT
    request = replace(request, admission=admitted)
    request = replace(
        request,
        deps=replace(request.deps, turn_runtimes={}, turn_runtime_factory=build_channel_runtime),
    )
    if scenario in ("attack", "origin", "entra_attack", "entra_pool_unbound", "entra_pool_origin"):
        from daimon.core.turn.errors import AdmissionDenied

        with pytest.raises(AdmissionDenied):
            await bind_session_impl(
                request.deps,
                admitted,
                tenant_id=TENANT,
                platform="slack",
                external_user_id="caller",
                thread_id=request.thread_id,
                session_account_id=ACCOUNT,
                reuse_existing=True,
            )
        assert requests == []
        assert token_calls == [] and credentials == []
        return
    prepared = await bind_session_impl(
        request.deps,
        admitted,
        tenant_id=TENANT,
        platform="slack",
        external_user_id="caller",
        thread_id=request.thread_id,
        session_account_id=ACCOUNT,
        reuse_existing=True,
    )
    assert isinstance(prepared.runtime, SelfHostedHostRuntime)

    async def reseed():
        return "question"

    outcome = await run_prepared_turn(
        request.deps,
        prepared,
        tenant_id=TENANT,
        platform="slack",
        thread_id="thread",
        external_user_id="caller",
        user_message="question",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        render_interval_s=0.01,
        operation_key="production-shell",
        confirm_write=confirm,
        attended=not scenario.endswith("unattended"),
        # Leave room for durable admission before testing a missing terminal event.
        deadline=datetime.now(UTC) + timedelta(seconds=5) if late else None,
    )
    await drain_outcomes()
    if approval_case:
        assert actions == [
            dict(
                action_id="approval",
                decision="deny" if scenario in ("ask_deny", "stored_deny") else "allow",
            )
        ]
        assert len(cards) == (
            0
            if scenario in ("stored_deny", "proxy_safety") or scenario.startswith("inspection")
            else 1
        )
        assert bool(executions) == (scenario not in ("ask_deny", "stored_deny"))
    if scenario == "proxy_default":
        assert executions == ["neutral"] and cards == [] and actions == []
    assert requests and wire.posts == 1 and len(admitted_keys) == 1
    assert bool(dropped) == drop_stream
    if late:
        assert cancellations and wire.posts == 1
        if scenario == "late_reconcile":
            async with request.deps.sessionmaker() as db:
                unresolved = await list_pending_usage(db, tenant_id=TENANT)
                assert unresolved.rows and unresolved.rows[0].completeness == "unknown"
            wire.events.extend(deferred)
            deferred.clear()
            # A new runtime has no in-memory turn state. Durable replay must
            # still adopt the old admission and settle the late measured revision.
            previous_runtime = prepared.runtime
            prepared = await bind_session_impl(
                request.deps,
                admitted,
                tenant_id=TENANT,
                platform="slack",
                external_user_id="caller",
                thread_id=request.thread_id,
                session_account_id=ACCOUNT,
                reuse_existing=True,
            )
            assert prepared.runtime is not previous_runtime and prepared.reused
            assert wire.posts == 1
    elif not unmatched:
        assert outcome.state.error is None
    else:
        from daimon.core.turn.termination import TerminationReason

        assert outcome.state.termination is TerminationReason.INTERRUPTED
    if scenario.startswith("entra"):
        if pool:
            assert token_calls == [("https://dynamicsessions.io/.default",)]
        assert len(credentials) == 1
        assert len(token_calls) == (2 if scenario == "entra_refresh" else 1)
        assert len({id(client) for client in clients}) == 1
    if pool:
        await prepared.backend.events._c.transport.request(
            "GET",
            f"/v0/sessions/{prepared.session_ref.id}/events?replay_after_seq=0&after_seq=0",
            after_seq=0,
            session_id=prepared.session_ref.id,
        )
        assert any("replay_after_seq" in call.url.params for call in requests)
    observations = await prepared.backend.usage.reconcile(SCOPE, prepared.session_ref)
    assert len(observations) == 1
    await prepared._record(observation=observations[0])
    await prepared._record(observation=observations[0])
    async with request.deps.sessionmaker() as db:
        pending = await list_pending_usage(db, tenant_id=TENANT)
        token = await get_mcp_token(db, jti=token_ids[0])
        binding = await mux_state.PostgresStateStore(request.deps.sessionmaker).get_binding(
            Slot(
                thread=ThreadRef(
                    channel=REVISION.channel,
                    thread_id=request.thread_id,
                ),
                account_id=SCOPE.account_id,
            )
        )
        assert binding is not None
        receipt = await contexts.read_context(
            db,
            tenant_id=TENANT,
            binding_id=binding.id,
            key=contexts.context_key("receipt", binding.id, observations[0].id),
        )
    assert receipt is not None and receipt["admission"]["admission_key"] == admitted_keys[0]
    assert token is not None
    assert (token.revoked_at is None) == (scenario == "revoke_failure")
    if unmatched:
        assert any(
            row.observation_id == observations[0].id and row.reason == "usage_unknown"
            for row in pending.rows
        )
    else:
        assert not pending.rows

    if scenario == "long_history":
        import time

        # This checkpoint represents receipts already durably accounted for by
        # preceding invocations, including 1,000 separate model requests.
        template = wire.events[0]
        for index in range(1000):
            for kind in (
                "status_running",
                "model_request_started",
                "usage",
                "agent_message_delta",
                "agent_message",
                "turn_ended",
            ):
                wire.events.append(
                    {
                        **template,
                        "seq": len(wire.events) + 1,
                        "turn_id": f"prior-{index}",
                        "type": kind,
                        "data": {"request_id": f"old-{index}", "model": "gpt-6-luna"},
                    }
                )
        floor = len(wire.events)
        async with request.deps.sessionmaker() as db, db.begin():
            await contexts.advance_replay_cursor(
                db, tenant_id=TENANT, binding_id=binding.id, seq=floor
            )
        previous_runtime = prepared.runtime
        requests.clear()
        started = time.perf_counter()
        prepared = await bind_session_impl(
            request.deps,
            admitted,
            tenant_id=TENANT,
            platform="slack",
            external_user_id="caller",
            thread_id=request.thread_id,
            session_account_id=ACCOUNT,
            reuse_existing=True,
        )
        assert prepared.runtime is not previous_runtime
        outcome = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=TENANT,
            platform="slack",
            thread_id="thread",
            external_user_id="caller",
            user_message="next question",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            render_interval_s=0.01,
            operation_key="after-long-history",
        )
        elapsed = time.perf_counter() - started
        await drain_outcomes()
        assert outcome.state.error is None
        assert elapsed < 1, f"turn with 1,000 prior requests took {elapsed:.3f}s"
        assert wire.posts == 2
        gets = [call for call in requests if call.method == "GET"]
        assert gets and all(int(call.url.params["after_seq"]) >= floor for call in gets)

        # Recover the same invocation after a fresh runtime has already loaded
        # the later accounting checkpoint. Its saved baseline must still win.
        prepared = await bind_session_impl(
            request.deps,
            admitted,
            tenant_id=TENANT,
            platform="slack",
            external_user_id="caller",
            thread_id=request.thread_id,
            session_account_id=ACCOUNT,
            reuse_existing=True,
        )
        recovered = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=TENANT,
            platform="slack",
            thread_id="thread",
            external_user_id="caller",
            user_message="next question",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda cancel: RecordingLifecycle(),
            render_interval_s=0.01,
            operation_key="after-long-history",
        )
        await drain_outcomes()
        assert recovered.state.error is None and "answer 2" in str(recovered.state.content)
        assert wire.posts == 2


async def test_preparation_rechecks_disabled_operator_switch_before_harness_io(
    composed, monkeypatch
):
    from daimon.core.turn.errors import AdmissionDenied

    request, wire, _, _ = composed
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "false")
    with pytest.raises(AdmissionDenied, match="backend_unsupported"):
        await prepare_selfhosted(request)
    assert wire.posts == 0 and wire.events == []


async def test_batched_host_approvals_submit_independent_idempotent_actions(composed, monkeypatch):
    from daimon.core.turn.selfhosted_io import SelfHostedTurnIO
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart

    request, wire, _, _ = composed
    prepared = await prepare_selfhosted(request)
    await prepared.backend.events.send(
        SCOPE,
        prepared.session_ref,
        [UserMessage(content=(TextPart(text="question"),))],
        key="approval-turn",
    )
    wire.events = wire.events[:2]
    for action in ("one", "two"):
        wire.events.append(
            dict(
                seq=len(wire.events) + 1,
                session_id=prepared.session_ref.id,
                turn_id=wire.events[0]["turn_id"],
                at=datetime.now(UTC).isoformat(),
                type="requires_action",
                data=dict(
                    action_id=action, call_id="call-" + action, name="write", input={"path": action}
                ),
            )
        )
    actions = []
    original = wire.handle

    def handle(call):
        if call.url.path.endswith("/actions"):
            body = json.loads(call.content)
            actions.append(body)
            wire.events.append(
                dict(
                    seq=len(wire.events) + 1,
                    session_id=prepared.session_ref.id,
                    turn_id=wire.events[0]["turn_id"],
                    at=datetime.now(UTC).isoformat(),
                    type="tool_result",
                    data=dict(
                        call_id="call-" + body["action_id"], output="executed", is_error=False
                    ),
                )
            )
            return httpx.Response(200, json={"accepted": True})
        return original(call)

    class Persistence:
        def __init__(self, prefix="current"):
            self.prefix = prefix

        async def mutate(self, session, kind, payload, call, response, status):
            return await call(self.prefix + ":approval:" + session.id + ":" + kind)

    monkeypatch.setattr(wire, "handle", handle)

    # The injected HTTP client's dispatcher was bound at construction.
    async def http(self, method, url, **kwargs):
        call = httpx.Request(method, url, json=kwargs.get("json"), headers=kwargs["headers"])
        response = handle(call)
        response.request = call
        return response

    monkeypatch.setattr(httpx.AsyncClient, "request", http)
    io = SelfHostedTurnIO(
        prepared.backend,
        SCOPE,
        prepared.session_ref,
        persistence=Persistence(),
        root_turn_id=wire.events[0]["turn_id"],
    )
    confirmations = [
        dict(
            type="user.tool_confirmation",
            tool_use_id="selfhosted:tool:call-" + action,
            result="allow",
        )
        for action in ("one", "two")
    ]
    from mux.errors import ProviderError

    stale = SelfHostedTurnIO(
        prepared.backend,
        SCOPE,
        prepared.session_ref,
        persistence=Persistence("stale"),
        root_turn_id="stale-root",
    )
    with pytest.raises(ProviderError):
        await stale.send(confirmations[:1])
    assert actions == []
    await io.send(confirmations[:1])
    restarted = await prepare_selfhosted(request)
    assert restarted.backend is not prepared.backend
    assert restarted.session_ref == prepared.session_ref
    pending = await restarted.backend.events.reconcile(SCOPE, restarted.session_ref)
    assert [action.id for action in pending.required_actions] == ["two"]
    recovered = SelfHostedTurnIO(
        restarted.backend,
        SCOPE,
        restarted.session_ref,
        persistence=Persistence(),
        root_turn_id=wire.events[0]["turn_id"],
    )
    await recovered.send(confirmations)
    await recovered.send(confirmations)
    settled = await restarted.backend.events.reconcile(SCOPE, restarted.session_ref)
    assert settled.required_actions == ()
    assert actions == [
        dict(action_id="one", decision="allow"),
        dict(action_id="two", decision="allow"),
    ]


@pytest.mark.parametrize("delta_count", [0, 5100])
async def test_interrupt_journals_canonical_stop_before_releasing_occupancy(
    composed, monkeypatch, delta_count
):
    from daimon.core.turn.selfhosted_io import SelfHostedTurnIO
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart

    request, wire, _, _ = composed
    prepared = await prepare_selfhosted(request)
    await prepared.backend.events.send(
        SCOPE,
        prepared.session_ref,
        [UserMessage(content=(TextPart(text="question"),))],
        key="cancel-turn",
    )
    root = wire.events[0]["turn_id"]
    wire.events.pop()
    append_message_deltas(wire, delta_count)
    cancellations, recorded, released = [], [], []
    original = wire.handle

    def handle(call):
        if call.url.path.endswith("/cancel"):
            cancellations.append(call.url.path)
            wire.events.append(
                dict(
                    seq=len(wire.events) + 1,
                    session_id=prepared.session_ref.id,
                    turn_id=root,
                    at=datetime.now(UTC).isoformat(),
                    type="turn_ended",
                    data={"outcome": "interrupted"},
                )
            )
            return httpx.Response(200, json={"cancelled": True})
        return original(call)

    async def http(self, method, url, **kwargs):
        call = httpx.Request(method, url, json=kwargs.get("json"), headers=kwargs["headers"])
        response = handle(call)
        response.request = call
        return response

    class Persistence:
        async def mutate(self, session, kind, payload, call, response, status):
            assert kind == "cancel" and payload == {"turn_id": root}
            return await call("cancel-operation")

        async def record(self, session, event, *, source_key=None):
            assert session == prepared.session_ref
            recorded.append(event)

        async def stopped(self, key, session):
            assert recorded[-1].type == "session.turn_ended"
            assert recorded[-1].turn_id == root
            released.append((key, session))

    monkeypatch.setattr(httpx.AsyncClient, "request", http)
    io = SelfHostedTurnIO(
        prepared.backend,
        SCOPE,
        prepared.session_ref,
        persistence=Persistence(),
        root_turn_id=root,
    )
    stopped = await io.interrupt(timeout_s=10)
    assert stopped.stopped and stopped.outcome == "interrupted"
    assert len(cancellations) == 1
    assert released == [(stopped.receipt_operation_id, prepared.session_ref)]


async def test_replay_pages_past_five_thousand_records_in_one_turn(composed, monkeypatch):
    from daimon.core.turn.reducers import apply
    from daimon.core.turn.selfhosted_io import SelfHostedTurnIO
    from daimon.core.turn.state import TextBlock, TurnState
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart

    request, wire, _, _ = composed
    prepared = await prepare_selfhosted(request)
    receipt = await prepared.backend.events.send(
        SCOPE,
        prepared.session_ref,
        (UserMessage(content=(TextPart(text="question"),)),),
        key="long-replay",
    )
    message, ended = wire.events[-2:]
    wire.events[:] = wire.events[:2]
    append_message_deltas(wire, 5100)
    wire.events.extend(
        [
            {**message, "seq": len(wire.events) + 1},
            {**ended, "seq": len(wire.events) + 2},
        ]
    )
    cursors, recorded = [], []
    original = prepared.backend.events.list

    async def list_events(scope, session, *, page):
        if page.order == "asc":
            cursors.append(int(page.cursor or "0"))
        return await original(scope, session, page=page)

    monkeypatch.setattr(prepared.backend.events, "list", list_events)

    class Persistence:
        async def record(self, session, event, *, source_key=None):
            recorded.append(event.sequence)

    async def baseline(session, roots, after):
        return (), True, 0

    io = SelfHostedTurnIO(
        prepared.backend,
        SCOPE,
        prepared.session_ref,
        persistence=Persistence(),
        root_turn_id=receipt.turn_id,
        baseline_loader=baseline,
    )
    replayed = await io.replay(timeout_s=10)
    state = TurnState()
    for display in replayed:
        state = apply(state, display)
    assert [block.text for block in state.content if isinstance(block, TextBlock)] == ["answer 1"]
    assert state.stop_reason is not None and state.stop_reason.type == "end_turn"
    assert cursors == list(range(0, 5101, 100))
    assert recorded == list(range(1, len(wire.events) + 1))

    async def stalled_page(scope, session, *, page):
        result = await original(scope, session, page=page)
        return result.model_copy(update={"next_cursor": page.cursor})

    from mux.errors import ProviderError

    monkeypatch.setattr(prepared.backend.events, "list", stalled_page)
    with pytest.raises(ProviderError) as caught:
        await io.replay(timeout_s=10)
    assert caught.value.native_code == "host_history_cursor"


@pytest.mark.parametrize("explicit", [None, False, True])
@pytest.mark.parametrize("approval_first", [False, True])
async def test_live_and_replayed_approval_permissions_match(composed, explicit, approval_first):
    from daimon.core.tool_safety import ToolSafetyPolicy, decide_tool_call
    from daimon.core.turn.approvals import tool_calls_for
    from daimon.core.turn.reducers import apply
    from daimon.core.turn.selfhosted_io import SelfHostedTurnIO
    from daimon.core.turn.state import ToolUseBlock, TurnState
    from mux.contracts.actions import UserMessage
    from mux.contracts.events import TextPart
    from mux.contracts.ids import PageRequest

    request, wire, _, _ = composed
    prepared = await prepare_selfhosted(request)
    receipt = await prepared.backend.events.send(
        SCOPE,
        prepared.session_ref,
        (UserMessage(content=(TextPart(text="question"),)),),
        key="replay-approval",
    )
    wire.events[:] = wire.events[:2]
    wire.events.append(
        dict(
            seq=3,
            session_id=prepared.session_ref.id,
            turn_id=receipt.turn_id,
            at=datetime.now(UTC).isoformat(),
            type="requires_action",
            data=dict(action_id="approval", call_id="call", name="reports/get_report", input={}),
        )
    )
    tool_use = {
        **wire.events[-1],
        "type": "tool_use",
        "data": {
            "call_id": "call",
            "name": "reports/get_report",
            "input": {},
            "executor": "mcp",
        },
    }
    if approval_first:
        wire.events.append({**tool_use, "seq": 4})
    else:
        wire.events[-1] = {**wire.events[-1], "seq": 4}
        wire.events.insert(-1, tool_use)

    class Persistence:
        async def record(self, *args, **kwargs):
            pass

    async def baseline(session, roots, after):
        return (), False, 0

    io = SelfHostedTurnIO(
        prepared.backend,
        SCOPE,
        prepared.session_ref,
        persistence=Persistence(),
        root_turn_id=receipt.turn_id,
        explicit_ask=(lambda name: explicit) if explicit is not None else None,
        baseline_loader=baseline,
    )
    page = await prepared.backend.events.list(SCOPE, prepared.session_ref, page=PageRequest())
    live = []
    for value in page.data:
        live.extend(frame.native for frame in await io.accept_record(value))
    replayed = await io.replay()
    live_tools = [value for value in live if value.type == "agent.mcp_tool_use"]
    replayed_tools = [value for value in replayed if value.type == "agent.mcp_tool_use"]
    assert live_tools and replayed_tools
    assert all(value.id == "selfhosted:tool:call" for value in live_tools + replayed_tools)
    states = []
    for displays in (live, replayed):
        state = TurnState()
        for display in displays:
            state = apply(state, display)
        states.append(state)
    blocks = [
        [block for block in state.content if isinstance(block, ToolUseBlock)] for state in states
    ]
    assert blocks[0] == blocks[1]
    assert len(blocks[0]) == 1
    expected_ask = explicit is not False
    assert blocks[0][0].evaluated_permission == ("ask" if expected_ask else "allow")
    for state in states:
        (call,) = tool_calls_for(state, ["selfhosted:tool:call"])
        assert decide_tool_call(ToolSafetyPolicy(enabled=False), call, attended=True).outcome == (
            "ask" if expected_ask else "allow"
        )
        assert decide_tool_call(ToolSafetyPolicy(enabled=False), call, attended=False).outcome == (
            "deny" if expected_ask else "allow"
        )
