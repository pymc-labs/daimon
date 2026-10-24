"""A tenant-wide OpenAI agent template: publish each agent version once, then run it.

The public switch, consent, source preparation, catalogue, SDK and session owners
are production paths; only the native HTTP edge is scripted.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.openai_deployment import OpenAIDeployment
from daimon.core.turn.prepare import admitted_session_scope
from daimon.core.turn.runtime_manifest import (
    read_preparation_counterpart,
    root_counterpart_catalog_id,
)
from daimon.core.turn.runtimes import build_channel_runtime, scope_channel_runtimes
from pydantic import ValidationError

from ..test_agent_backend import owned as owned
from .test_declared_drop_preparation import consent_request as consent_request
from .test_openai_full_source import FullWire
from .test_openai_runtime_transport import clients_for

KEY = "OFFLINE_TEMPLATE_OPENAI_KEY"


def template_row(tenant_id: str, **changes: object) -> dict[str, object]:
    return {
        "profile": "openai.persistent_workspace",
        "tenant_id": tenant_id,
        "platform": "*",
        "channel_id": "*",
        "account_id": "*",
        "configuration_grant": {
            "profiles": ["openai.persistent_workspace"],
            "models": ["gpt-6-luna"],
            "thread_modes": ["per_caller"],
            "requires": {},
        },
        "project": "project",
        "environment_id": "native-template",
        "api_key_env": KEY,
        "spend_limit_usd_cents": 50,
        "root_materialization": True,
    } | changes


@pytest.fixture
async def template(consent_request, monkeypatch, tmp_path):
    request, *_ = consent_request
    sessions = request.deps.sessionmaker
    request = replace(
        request,
        scope=admitted_session_scope(
            request.admission, tenant_id=request.tenant_id, session_id="preparation"
        ),
        now=lambda: datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(seconds=60),
        deps=replace(
            request.deps,
            state_store=PostgresStateStore(sessions),
            turn_runtimes={},
            turn_runtime_factory=build_channel_runtime,
        ),
    )
    wire = FullWire(request.scope.tenant_id)
    clients = clients_for(monkeypatch, wire.handle)
    manifest = tmp_path / "runtime.json"
    manifest.write_text(json.dumps([template_row(request.scope.tenant_id)]))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
    monkeypatch.setenv(KEY, "offline")
    yield request, wire, manifest
    assert all(client.is_closed() for client in clients)


def agent_creates(wire: FullWire) -> list[object]:
    return [r for r in wire.requests if r.method == "POST" and r.url.path == "/v1/agents"]


async def test_a_template_publishes_the_agent_version_once_and_plans_its_session(template):
    request, wire, _ = template
    scoped, _ = scope_channel_runtimes(request)
    first = await read_preparation_counterpart(scoped)
    assert first.verified_counterpart == first.counterpart
    assert first.counterpart.catalog_id == root_counterpart_catalog_id(first)
    assert first.counterpart.agent.spec == first.target_spec
    assert len(agent_creates(wire)) == 1
    again, lookup = scope_channel_runtimes(request)
    second = await read_preparation_counterpart(again)
    assert second.counterpart == first.counterpart
    assert len(agent_creates(wire)) == 1, "a second turn reuses the published counterpart"
    runtime = lookup.resolved
    planned = await runtime.session_plan(second)
    assert planned.agent == second.counterpart.agent.ref
    assert planned.environment is not None and planned.environment.id == "native-template"


async def test_two_concurrent_first_turns_publish_one_counterpart(template):
    request, wire, _ = template
    first, _ = scope_channel_runtimes(request)
    second, _ = scope_channel_runtimes(request)
    a, b = await asyncio.gather(
        read_preparation_counterpart(first), read_preparation_counterpart(second)
    )
    assert a.counterpart == b.counterpart and a.counterpart.local_revision == 1
    assert len(agent_creates(wire)) == 1, "exactly one native agent, whatever the race"


async def test_a_template_never_serves_another_tenant(template):
    request, wire, manifest = template
    foreign = template_row("00000000-0000-0000-0000-00000000000b")
    manifest.write_text(json.dumps([foreign]))
    scoped, _ = scope_channel_runtimes(request)
    with pytest.raises(AdmissionDenied):
        await read_preparation_counterpart(scoped)
    assert agent_creates(wire) == []


async def test_the_planned_session_never_uses_another_agents_counterpart(template):
    request, _, _ = template
    scoped, lookup = scope_channel_runtimes(request)
    prepared = await read_preparation_counterpart(scoped)
    runtime = lookup.resolved
    checked = prepared.counterpart
    agent = checked.agent
    other = replace(
        checked,
        agent=agent.model_copy(update={"ref": agent.ref.model_copy(update={"id": "another"})}),
    )
    with pytest.raises(AdmissionDenied):
        await runtime.session_plan(replace(prepared, counterpart=other))
    with pytest.raises(AdmissionDenied):
        await runtime.session_plan(
            replace(prepared, counterpart=replace(checked, catalog_id="pinned"))
        )


@pytest.mark.parametrize(
    "pin",
    [
        {"source_agent_id": "00000000-0000-0000-0000-000000000001"},
        {"source_agent_version": 3},
        {"counterpart_catalog_id": "pinned"},
        {"agent_id": "native"},
        {"worker_materialization": True},
        # Nor attach skills to every agent it publishes, or run uncapped.
        {"skill_ids": ["skill_operator"]},
        {"spend_limit_usd_cents": None},
    ],
)
def test_an_openai_template_row_can_pin_no_identity(pin):
    row = OpenAIDeployment.model_validate(template_row("t"))
    assert row.root_materialization and row.agent_id is None
    with pytest.raises(ValidationError):
        OpenAIDeployment.model_validate({**template_row("t"), **pin})


async def test_a_template_turn_runs_on_the_published_counterpart(template):
    from daimon.core.turn.prepare import bind_session_impl
    from daimon.core.turn.run import run_prepared_turn
    from daimon.testing.turn_fakes import RecordingLifecycle

    request, wire, _ = template
    for index in range(2):
        prepared = await bind_session_impl(
            request.deps,
            request.admission,
            tenant_id=request.tenant_id,
            platform=request.platform,
            external_user_id=request.external_user_id,
            thread_id=request.thread_id,
            session_account_id=request.session_account_id,
            reuse_existing=index != 0,
        )
        assert prepared.session_ref.provider == "openai"

        async def no_recovery():
            pytest.fail("the template turn changed its admitted source/session")

        outcome = await run_prepared_turn(
            request.deps,
            prepared,
            tenant_id=request.tenant_id,
            platform=request.platform,
            thread_id=request.thread_id,
            external_user_id=request.external_user_id,
            user_message="Question",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=no_recovery,
            recovery_lifecycle=lambda _: RecordingLifecycle(),
            operation_key=f"template:{index}",
        )
        assert outcome.state.error is None and not outcome.recovered
    assert len(agent_creates(wire)) == 1
    assert len(wire.posts) == 1, "the second turn reuses the session"
    body = wire.posts[0]
    assert body["agent_id"] == wire.agent["id"]
    assert body["environment"]["environment_template_id"] == "native-template"
    # The native agent is the consented target, losses stated in its instructions.
    assert wire.agent["instructions"].startswith("Authoritative source instructions")
    assert "Unavailable capabilities: memory, sandbox." in wire.agent["instructions"]


@pytest.mark.parametrize("capability", ["custom_tool", "mcp_server"])
async def test_a_template_refuses_what_it_cannot_run_before_any_native_write(template, capability):
    from mux.contracts.resources import MCPConnection, ToolSpec

    request, wire, _ = template
    scoped, lookup = scope_channel_runtimes(request)
    prepared = await read_preparation_counterpart(scoped)
    wire.requests.clear()
    target = prepared.target_spec
    update = (
        {"tools": (ToolSpec(kind="custom", name="lookup", input_schema={"type": "object"}),)}
        if capability == "custom_tool"
        else {"mcp_servers": (MCPConnection(name="caller", url="https://mcp.invalid/mcp"),)}
    )
    unrunnable = replace(prepared, target_spec=target.model_copy(update=update))
    with pytest.raises(AdmissionDenied):
        await lookup.resolved.materialize_root(unrunnable)
    assert wire.requests == []
