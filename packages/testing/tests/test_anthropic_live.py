"""Mock-transport proof only: private temporary ledgers, no key/paid calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import Mock

import httpx
import pytest
from daimon.testing import anthropic_live as live
from daimon.testing.conformance.budget import BudgetConfig
from daimon.testing.isolated_sdk import TLS_PROXY_ENV_VARS
from daimon.testing.ma import send_events_response
from daimon.testing.ma_models import ma_session
from mux.contracts.ids import ResourceRef, Revision, Scope
from mux.contracts.resources import SessionSpec
from mux.errors import ProviderError

from packages.testing.tests.conformance.test_budget import receipts
from packages.testing.tests.conformance.test_exact_spend import anthropic_session_guard

SCOPE = Scope(
    tenant_id="test-tenant",
    account_id="test-account",
    principal_id="probe",
    authorization_id="offline-owned",
)


def ref(id_: str, kind: str) -> ResourceRef:
    return ResourceRef(
        id=id_,
        kind=kind,
        provider="anthropic",
        account_scope_id="test-owned",
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
    )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in tuple(os.environ):
        if (
            name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GEMINI_", "GOOGLE_"))
            or name.upper() in TLS_PROXY_ENV_VARS
        ):
            monkeypatch.delenv(name)


@pytest.fixture
def signed_price_clock(monkeypatch):
    """The immutable financial fixture is valid only on 2026-10-11."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            at = datetime(2026, 10, 11, 19, tzinfo=UTC)
            return at.replace(tzinfo=None) if tz is None else at.astimezone(tz)

    monkeypatch.setattr(live, "datetime", Clock)
    return Clock.now(UTC)


def admission(tmp_path, monkeypatch, package="A7"):
    guard = anthropic_session_guard(tmp_path)
    config = BudgetConfig.load(guard.config_path)
    monkeypatch.setattr(
        live, "HOST_CONFIG_SHA256", hashlib.sha256(guard.config_path.read_bytes()).hexdigest()
    )
    # Guard's public checkpoint exposes its ledger identity; no private API used.
    checkpoint = json.loads(guard.checkpoint_path.read_text())
    monkeypatch.setattr(live, "LEDGER_ID", checkpoint["binding"]["ledger_id"])
    args = live.Admission(
        package,
        {"credential_file": "not-read.env"},
        "a" * 64,
        guard.config_path,
        guard.spend_path,
        live.LiveBinding(
            scope=SCOPE,
            account_scope_id="test-owned",
            environment=ref("env-test", "environment"),
            agent=ref("agent-test", "agent"),
            database_url="unused-owned-db",
        ),
        tmp_path / "evidence",
        300 if package == "A7" else 600,
        "b" * 40,
    )
    return args, guard, config


class Native:
    def __init__(self, fault=""):
        self.fault = fault
        self.requests = []
        self.created = 0

    async def handle(self, request):
        path = request.url.path
        self.requests.append((request.method, path))
        if path == "/v1/sessions":
            self.created += 1
            if self.fault == "lost-create":
                raise httpx.ReadError("ack lost", request=request)
            return httpx.Response(
                200,
                json=ma_session(
                    id=f"sess-{self.created}", agent_id="agent-test", model=live.MODEL
                ).model_dump(mode="json"),
            )
        if path.endswith("/archive"):
            if self.fault == "lost-archive":
                raise httpx.ReadError("archive ack lost", request=request)
            return httpx.Response(
                200,
                json=ma_session(id=path.split("/")[-2], model=live.MODEL).model_dump(mode="json"),
            )
        if path.endswith("/events"):
            if self.fault == "lost-send" and any(
                item["type"] == "user.message" for item in json.loads(request.content)["events"]
            ):
                raise httpx.ReadError("send ack lost", request=request)
            return send_events_response()
        raise AssertionError("unexpected mock-native request")


async def create(context):
    return await context.driver.sessions.create(
        SCOPE,
        SessionSpec(
            agent=ref("agent-test", "agent"),
            agent_revision=Revision(local=1, native="1"),
            environment=ref("env-test", "environment"),
            config_revision=0,
        ),
        key="one-attempt",
    )


async def test_reservation_precedes_key_client_and_sessions_and_settles_once(tmp_path, monkeypatch):
    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()
    order = []

    def key(path):
        assert len(guard.report()) == 1 and guard.report()[0].held_usd == Decimal(".35")
        order.append("key")
        return "offline-explicit-key"

    def transport():
        assert order == ["key"]
        order.append("client")
        return httpx.MockTransport(native.handle)

    async def invoke(context):
        assert context.client.max_retries == 0
        await create(context)
        return {"status": "pass"}

    result = await live.run_live(args, invoke, secret_loader=key, transport_factory=transport)
    assert order == ["key", "client"]
    assert native.requests == [
        ("POST", "/v1/sessions"),
        ("POST", "/v1/sessions/sess-1/events"),
        ("POST", "/v1/sessions/sess-1/archive"),
    ]
    rows = receipts(guard)
    assert len(rows) == 2
    assert result["spend"]["actual_usd"] is None and Decimal(
        result["spend"]["held_usd"]
    ) == Decimal(".35")
    receipt = json.loads((args.output / "receipt.json").read_text())
    assert (
        receipt["native_budget_sent"] is False and receipt["provider_token_caps_enforced"] is False
    )
    assert receipt["created_resources"] == [["session", hashlib.sha256(b"sess-1").hexdigest()]]
    assert receipt["archived_sessions"] == [hashlib.sha256(b"sess-1").hexdigest()]
    assert "offline-explicit-key" not in (args.output / "receipt.json").read_text()
    with pytest.raises(live.LiveRefused, match="already has an attempt"):
        await live.run_live(
            replace(args, output=tmp_path / "different"),
            invoke,
            secret_loader=Mock(side_effect=AssertionError),
        )
    assert native.created == 1 and len(guard.report()) == 1


@pytest.mark.parametrize("fault", ["lost-create", "lost-send", "lost-archive"])
async def test_lost_ack_never_reposts_and_keeps_full_hold(tmp_path, monkeypatch, fault):
    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native(fault)

    async def invoke(context):
        session = await create(context)
        from mux.contracts.actions import UserMessage
        from mux.contracts.events import TextPart

        await context.driver.events.send(
            SCOPE, session.ref, (UserMessage(content=(TextPart(text="probe"),)),), key="one-send"
        )
        return {"status": "pass"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(native.handle),
    )
    assert native.requests.count(("POST", "/v1/sessions")) == 1
    assert native.requests.count(("POST", "/v1/sessions/sess-1/archive")) <= 1
    assert guard.report()[0].actual_usd is None and guard.report()[0].held_usd == Decimal(".35")


async def test_total_deadline_interrupts_archives_then_settles(tmp_path, monkeypatch):
    args, guard, _ = admission(tmp_path, monkeypatch)
    args = replace(args, deadline_s=1)
    native = Native()
    cancelled = asyncio.Event()

    async def invoke(context):
        await create(context)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    result = await asyncio.wait_for(
        live.run_live(
            args,
            invoke,
            secret_loader=lambda _: "offline-key",
            transport_factory=lambda: httpx.MockTransport(native.handle),
        ),
        2,
    )
    assert cancelled.is_set(), result
    assert native.requests[-2:] == [
        ("POST", "/v1/sessions/sess-1/events"),
        ("POST", "/v1/sessions/sess-1/archive"),
    ]
    assert guard.report()[0].status == "failed" and guard.report()[0].held_usd == Decimal(".35")


def usage(id_, incoming=1000, output=100):
    return {
        "id": id_,
        "type": "span.model_request_end",
        "model_usage": {
            "input_tokens": incoming,
            "output_tokens": output,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
        },
    }


@pytest.mark.parametrize("package,threshold", [("A7", ".28"), ("F2-Anthropic", ".52")])
def test_stream_monitor_prices_tiers_ttl_and_cumulative_stop(
    tmp_path, monkeypatch, package, threshold
):
    args, _, config = admission(tmp_path, monkeypatch, package)
    provider = config.providers["anthropic"]
    monitor = live.UsageMonitor(
        provider.models[live.MODEL], provider.session_prices[0], Decimal(threshold)
    )
    monitor.observe(usage("first", 200000, 100))
    assert monitor.cost > Decimal(".10") and monitor.observed_thresholds == ["first"]
    with pytest.raises(live.LiveStopped, match="stop threshold"):
        monitor.observe(usage("second", 1000000, 100))
    assert len(monitor.observations) == 2


def test_missing_counter_is_unknown_and_not_below_threshold(tmp_path, monkeypatch):
    _, _, config = admission(tmp_path, monkeypatch)
    provider = config.providers["anthropic"]
    monitor = live.UsageMonitor(
        provider.models[live.MODEL], provider.session_prices[0], Decimal(".28")
    )
    event = usage("partial")
    event["model_usage"]["input_tokens"] = None
    with pytest.raises(live.LiveStopped, match="unknown"):
        monitor.observe(event)
    assert monitor.observations[("monitor", "partial")]["model_usage"]["input_tokens"] is None


@pytest.mark.parametrize(
    "content", [b"ANTHROPIC_API_KEY=offline-test-key\n", b"offline-test-bearer"]
)
def test_explicit_secret_never_sources_file(tmp_path, content):
    path = tmp_path / "synthetic.env"
    path.write_bytes(content)
    path.chmod(0o600)
    assert live.explicit_secret(path, env_file=content.startswith(b"ANTHROPIC")) in {
        "offline-test-key",
        "offline-test-bearer",
    }
    path.chmod(0o644)
    with pytest.raises(live.LiveRefused):
        live.explicit_secret(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        live.explicit_secret(link)


@pytest.mark.parametrize(
    "name", ["ANTHROPIC_API_KEY", "OPENAI_CUSTOM_HEADERS", "SSLKEYLOGFILE", "HTTPS_PROXY"]
)
async def test_hostile_environment_refuses_before_reserve_or_key(tmp_path, monkeypatch, name):
    args, guard, _ = admission(tmp_path, monkeypatch)
    monkeypatch.setenv(name, "unread-hostile")
    loader = Mock(side_effect=AssertionError("key must not be read"))
    with pytest.raises(ValueError):
        await live.run_live(args, Mock(), secret_loader=loader)
    assert not guard.report()
    loader.assert_not_called()


def test_changed_go_and_financial_envelope_refuse_without_effects(tmp_path):
    approval = tmp_path / "approval.json"
    approval.write_text("{}")
    go = tmp_path / "go.json"
    go.write_text('{"live_go":true}')
    with pytest.raises(live.LiveRefused):
        live.admit(
            package="A7",
            approval_path=approval,
            go_path=go,
            go_digest="0" * 64,
            config_path=tmp_path / "missing-config",
            ledger_path=tmp_path / "missing-ledger",
            binding_path=tmp_path / "missing-binding",
            output=tmp_path / "output",
            source_head="a" * 40,
            source_clean=True,
        )
    assert not (tmp_path / "output").exists()


async def test_real_f2_scripted_flow_uploads_default_skills_and_uses_ports(tmp_path, monkeypatch):
    """Same executable callback as live; only HTTP replies/credentials are synthetic."""
    import importlib.util
    from pathlib import Path

    from daimon.testing.anthropic_live_cases import default_manifest, f2_default
    from daimon.testing.ma import sse_response
    from mux.contracts.ids import ModelRef

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "f2_live_script", root / "packages/mux/tests/test_default_capability_anthropic.py"
    )
    script_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script_module)
    script_module.MODEL = ModelRef(provider="anthropic", id=live.MODEL)
    manifest = default_manifest(root, "https://mcp.owned.example/mcp")
    script = script_module.Script(manifest)
    from collections import deque

    from daimon.testing.f2_native_bounds import native_budget, native_tools

    bounded_replies = []
    bounded_agent = None
    for reply in script.replies:
        if reply.path.startswith("/v1/agents"):
            payload = reply.response.json()
            payload["tools"] = native_tools()
            bounded_agent = payload
            reply = replace(reply, response=httpx.Response(200, json=payload))
        elif reply.path == "/v1/sessions":
            payload = reply.response.json()
            payload["agent"] = bounded_agent
            payload["budget"] = native_budget()
            reply = replace(reply, response=httpx.Response(200, json=payload))
        bounded_replies.append(reply)
    script.replies = deque(bounded_replies)
    args, guard, _ = admission(tmp_path, monkeypatch, "F2-Anthropic")
    bearer = tmp_path / "synthetic-bearer"
    bearer.write_text("offline-only-bearer")
    bearer.chmod(0o600)
    args = replace(
        args,
        binding=args.binding.model_copy(
            update={
                "mcp_url": manifest.mcp.url,
                "mcp_bearer_file": bearer,
                "environment": ref(script_module.ENVIRONMENT.id, "environment"),
            }
        ),
    )
    calls = []

    async def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "id": "vault_f2",
                    "type": "vault",
                    "display_name": "f2-offline",
                    "created_at": script_module.NOW,
                    "updated_at": script_module.NOW,
                    "metadata": {},
                },
            )
        if path == "/v1/vaults/vault_f2/credentials":
            body = json.loads(request.content)
            assert body["auth"]["token"] == "offline-only-bearer"
            return httpx.Response(
                200,
                json={
                    "id": "credential_f2",
                    "type": "vault_credential",
                    "vault_id": "vault_f2",
                    "display_name": "daimon-mcp",
                    "created_at": script_module.NOW,
                    "updated_at": script_module.NOW,
                    "metadata": {},
                    "auth": {"type": "static_bearer", "mcp_server_url": manifest.mcp.url},
                },
            )
        if (
            request.method == "POST"
            and path.endswith("/events")
            and any(e["type"] == "user.interrupt" for e in json.loads(request.content)["events"])
        ):
            return send_events_response()
        if path.endswith("/archive") or request.method == "DELETE":
            return httpx.Response(200, json={})
        if request.method == "GET" and path == "/v1/sessions/sess_f1":
            return httpx.Response(
                200,
                json={
                    **ma_session(id="sess_f1", model=live.MODEL).model_dump(mode="json"),
                    "agent": bounded_agent,
                    "budget": native_budget(),
                },
            )
        if request.method == "GET" and path == "/v1/sessions/sess_f1/events":
            return httpx.Response(200, json={"data": [], "next_page": None})
        if path.endswith("/events/stream"):
            events = script_module.native_turn(manifest)
            for event in events:
                event["processed_at"] = datetime.now(UTC).isoformat()
            script.replies.popleft()
            return sse_response(events)
        return script.dispatch(request)

    result = await live.run_live(
        args,
        f2_default,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    assert result["result"]["status"] == "pass", result
    assert calls.count(("POST", "/v1/skills")) == 11
    assert calls.count(("POST", "/v1/sessions")) == 1
    assert ("POST", "/v1/sessions/sess_f1/archive") in calls
    assert ("POST", "/v1/vaults/vault_f2/archive") in calls
    assert not script.replies and not script.violations
    assert guard.report()[0].actual_usd is None
    assert (args.output / "f2-normalized.json").exists()


@pytest.mark.parametrize("acknowledged", [True, False])
async def test_create_only_acceptance_callback_reserves_and_retires_without_model_work(
    tmp_path, monkeypatch, acknowledged
):
    from dataclasses import fields

    from daimon.testing.f2_budget_acceptance import AcceptanceAdmission, acceptance
    from daimon.testing.f2_native_bounds import native_budget, native_tools
    from daimon.testing.ma_models import ma_agent

    base, guard, _ = admission(tmp_path, monkeypatch, "F2-Anthropic")
    args = AcceptanceAdmission(**{item.name: getattr(base, item.name) for item in fields(base)})
    calls = []
    agent = ma_agent(id="agent-test", model=live.MODEL, tools=native_tools())
    session = ma_session(id="sess-probe", agent=agent).model_dump(mode="json")
    if acknowledged:
        session["budget"] = native_budget()

    async def wire(request):
        assert len(guard.report()) == 1 and len(receipts(guard)) == 1
        calls.append((request.method, request.url.path))
        if calls[-1] == ("GET", "/v1/agents/agent-test"):
            return httpx.Response(200, json=agent.model_dump(mode="json"))
        if calls[-1] == ("POST", "/v1/sessions"):
            assert json.loads(request.content)["budget"] == native_budget()
            return httpx.Response(200, json=session)
        if calls[-1] == ("GET", "/v1/sessions/sess-probe"):
            return httpx.Response(200, json=session)
        if calls[-1] == ("POST", "/v1/sessions/sess-probe/archive"):
            return httpx.Response(200, json=session)
        if calls[-1] == ("POST", "/v1/sessions/sess-probe/events"):
            assert [event["type"] for event in json.loads(request.content)["events"]] == [
                "user.interrupt"
            ]
            return send_events_response()
        pytest.fail("create-only acceptance dispatched unexpected native work")

    result = await live.run_live(
        args,
        acceptance,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(wire),
    )
    assert result["result"]["status"] == ("pass" if acknowledged else "failed")
    assert calls.count(("POST", "/v1/sessions")) == 1
    assert calls.count(("POST", "/v1/sessions/sess-probe/archive")) == 1
    if acknowledged:
        assert ("GET", "/v1/sessions/sess-probe") in calls
    assert len(receipts(guard)) == 2
    assert guard.report()[0].actual_usd is None
    assert guard.report()[0].held_usd == Decimal(".02")
    if acknowledged:
        assert result["result"]["user_messages"] == 0
        assert result["result"]["model_tokens_requested"] == 0
        assert result["result"]["enforcement_tested"] is False


async def test_sse_monitor_triggers_interrupt_archive_before_another_session(tmp_path, monkeypatch):
    from daimon.testing.ma import sse_response

    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def handler(request):
        if request.url.path.endswith("/events/stream"):
            return sse_response([usage("expensive", 1000000, 1)])
        return await native.handle(request)

    async def invoke(context):
        session = await create(context)
        stream = await context.driver.events.open_stream(SCOPE, session.ref)
        async for _ in stream:
            pass
        await create(context)
        return {"status": "pass"}

    result = await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    assert result["result"]["status"] == "failed"
    assert native.created == 1
    assert native.requests[-2:] == [
        ("POST", "/v1/sessions/sess-1/events"),
        ("POST", "/v1/sessions/sess-1/archive"),
    ]
    evidence = json.loads((args.output / "receipt.json").read_text())
    assert evidence["stream_stop_reason"] == "streamed cost reached the stop threshold"
    assert guard.report()[0].held_usd > Decimal(".35")
    assert guard.report()[0].admission_blocked is True


async def test_second_session_requires_first_archive_ack_and_third_is_refused(
    tmp_path, monkeypatch
):
    args, _, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def invoke(context):
        first = await create(context)
        with pytest.raises(ProviderError):
            await create(context)
        await context.driver.session_admin.archive(SCOPE, first.ref, key="leg1-cleanup")
        second = await create(context)
        await context.driver.session_admin.archive(SCOPE, second.ref, key="leg2-cleanup")
        with pytest.raises(ProviderError):
            await create(context)
        return {"status": "pass"}

    result = await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(native.handle),
    )
    assert result["result"]["status"] == "pass", result
    assert native.created == 2


@pytest.mark.parametrize("package", ["A7", "F2-Anthropic"])
def test_missing_go_live_cli_pending_never_reads_key_or_reserves(tmp_path, monkeypatch, package):
    import argparse

    from daimon.testing import anthropic_live_cases

    loader = Mock(side_effect=AssertionError("no key read"))
    monkeypatch.setattr(live, "explicit_secret", loader)
    monkeypatch.setattr(anthropic_live_cases, "explicit_secret", loader)
    args = argparse.Namespace(
        live_binding=None,
        authorization=None,
        go_receipt=None,
        go_sha256=None,
        budget_config=None,
        spend_ledger=None,
        output=tmp_path / "pending",
    )
    live.live_command(package, args)
    receipt = json.loads((args.output / "receipt.json").read_text())
    assert (
        receipt["status"] == "PENDING"
        and receipt["credential_reads"] == 0
        and receipt["reserved_usd"] == "0"
    )
    loader.assert_not_called()


@pytest.mark.parametrize(
    "native_ack,claim_status",
    [
        ("queued", "actual"),
        ("processed", "actual"),
        ("processed", "missing"),
        ("processed", "pending"),
        ("processed", "sent"),
        ("processed", "outcome_unknown"),
        ("processed", "failed"),
    ],
)
async def test_actual_a7_host_pair_uses_two_fresh_sessions_and_mux_journal(
    db_preflight, db_engine, db_clean, tmp_path, monkeypatch, native_ack, claim_status
):
    import uuid

    from daimon.testing import anthropic_live_cases as cases
    from daimon.testing.db import require_owned_sessionmaker
    from daimon.testing.factories import make_tenant
    from daimon.testing.ma import sse_response
    from daimon.testing.ma_models import ma_agent
    from sqlalchemy.ext.asyncio import async_sessionmaker

    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    require_owned_sessionmaker(sm)
    tenant = uuid.uuid4()
    async with sm.begin() as db:
        await make_tenant(db, id=tenant)
    scope = Scope(
        tenant_id=str(tenant), account_id="a7", principal_id="probe", authorization_id="offline"
    )
    args, guard, _ = admission(tmp_path, monkeypatch)

    def scoped(id_, kind):
        return ResourceRef(
            id=id_,
            kind=kind,
            provider="anthropic",
            account_scope_id="test-owned",
            tenant_id=str(tenant),
            account_id="a7",
        )

    args = replace(
        args,
        binding=args.binding.model_copy(
            update={
                "scope": scope,
                "environment": scoped("env-test", "environment"),
                "agent": scoped("agent-test", "agent"),
            }
        ),
    )
    monkeypatch.setattr(cases, "create_async_engine", lambda _: db_engine)
    original_get_operation = cases.PostgresStateStore.get_operation

    async def read_claim(store, scope, key):
        operation = await original_get_operation(store, scope, key)
        if operation is None or claim_status == "actual":
            return operation
        if claim_status == "missing":
            return None
        return operation.model_copy(
            update={"operation": operation.operation.model_copy(update={"status": claim_status})}
        )

    monkeypatch.setattr(cases.PostgresStateStore, "get_operation", read_claim)
    native = Native()

    async def handler(request):
        path = request.url.path
        if path == "/v1/agents/agent-test":
            return httpx.Response(
                200, json=ma_agent(id="agent-test", model=live.MODEL).model_dump(mode="json")
            )
        if path.endswith("/events/stream"):
            stamp = datetime.now(UTC).isoformat()
            return sse_response(
                [
                    {
                        "type": "agent.message",
                        "id": "message-" + path.split("/")[3],
                        "processed_at": stamp,
                        "content": [{"type": "text", "text": "a7-paired-probe-ok"}],
                    },
                    {
                        "type": "session.status_idle",
                        "id": "end-" + path.split("/")[3],
                        "processed_at": stamp,
                        "stop_reason": {"type": "end_turn"},
                    },
                ]
            )
        if request.method == "GET" and path.endswith("/events"):
            return httpx.Response(200, json={"data": [], "next_page": None})
        if request.method == "GET" and path.startswith("/v1/sessions/"):
            return httpx.Response(
                200,
                json=ma_session(id=path.split("/")[3], model=live.MODEL).model_dump(mode="json"),
            )
        if request.method == "POST" and path.endswith("/events") and native_ack == "processed":
            # Real SDK non-null data ACK translates to processed, not queued.
            native.requests.append((request.method, path))
            return send_events_response([])
        return await native.handle(request)

    result = await live.run_live(
        args,
        cases.a7_pair,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    assert result["result"]["status"] == ("pass" if claim_status == "actual" else "failed"), result
    legs = result["result"]["legs"]
    assert [leg["path"] for leg in legs] == ["legacy", "mux"]
    assert legs[0]["session_id"] != legs[1]["session_id"]
    assert legs[0]["journal_records"] == 0 < legs[1]["journal_records"]
    assert [leg["final_text"] for leg in legs] == ["a7-paired-probe-ok"] * 2
    assert [leg["termination"] for leg in legs] == ["completed"] * 2
    for leg in legs:
        file = args.output / f"a7-{leg['path']}-leg.json"
        assert json.loads(file.read_text()) == leg
        assert file.stat().st_mode & 0o777 == 0o600
    saved = json.loads((args.output / "receipt.json").read_text())
    saved_legs = saved["result"]["legs"]
    assert len(saved_legs) == 2
    for expected, projected in zip(legs, saved_legs, strict=True):
        assert projected["path"] == expected["path"]
        assert projected["termination"] == expected["termination"]
        assert projected["journal_records"] == expected["journal_records"]
        assert projected["elapsed_ms"] == expected["elapsed_ms"]
        assert (
            projected["session_id"] == hashlib.sha256(expected["session_id"].encode()).hexdigest()
        )
        assert projected["final_text"] == {
            "sha256": hashlib.sha256(expected["final_text"].encode()).hexdigest(),
            "text_rejected": True,
        }
    if claim_status != "actual":
        assert result["result"]["failure_type"] == "LiveRefused"
    assert native.created == 2
    assert native.requests.index(("POST", "/v1/sessions/sess-1/archive")) < native.requests.index(
        ("POST", "/v1/sessions"), 1
    )
    assert guard.report()[0].held_usd == Decimal(".35")
    assert len(receipts(guard)) == 2


async def test_completed_leg_refuses_credential_bearing_output_before_file_write(
    tmp_path, monkeypatch
):
    args, guard, _ = admission(tmp_path, monkeypatch)

    async def invoke(context):
        with pytest.raises(live.RecordingError):
            context.retain_completed_leg(
                {"path": "legacy", "final_text": "offline-key", "termination": "completed"}
            )
        assert not context.completed_legs
        assert not (args.output / "a7-legacy-leg.json").exists()
        return {"status": "failed"}

    result = await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(
            lambda _: pytest.fail("credential proof made a native request")
        ),
    )
    assert result["result"]["status"] == "failed"
    assert "legs" not in result["result"]
    assert len(receipts(guard)) == 2


@pytest.mark.parametrize("package", ["A7", "F2-Anthropic"])
@pytest.mark.parametrize(
    "corruption",
    [None, "go", "expiry", "source", "recipe", "config", "binding", "financial", "old_envelope"],
)
def test_exact_signed_admission_and_stale_pins(tmp_path, package, corruption, signed_price_clock):
    """Public signed metadata snapshots only; no canonical ledger/key access."""
    from datetime import timedelta
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures/anthropic-live"
    name = "F2-R2" if package == "F2-Anthropic" else package
    approval_path = fixtures / (name + ".json")
    approval = json.loads(approval_path.read_text())
    if corruption == "old_envelope":
        approval_path = fixtures / "F2-Anthropic.json"
        approval = json.loads(approval_path.read_text())
    elif corruption == "financial":
        approval["attempts_per_session"] = 2
        approval_path = tmp_path / "rewritten-approval.json"
        approval_path.write_text(json.dumps(approval))
    config = tmp_path / "config.json"
    config.write_bytes((fixtures / "host-config.json").read_bytes())
    binding = tmp_path / "binding.json"
    binding.write_text(
        live.LiveBinding(
            scope=SCOPE,
            account_scope_id="test-owned",
            environment=ref("env-test", "environment"),
            agent=ref("agent-test", "agent"),
            database_url="postgresql+asyncpg://fixture:fixture@localhost/daimon_test_nc_offline",
            mcp_url="https://mcp.owned.example/mcp",
            mcp_bearer_file=tmp_path / "never-read-bearer",
        ).model_dump_json()
    )
    recipe = tmp_path / "reviewed-recipe.json"
    recipe.write_text('{"model":"claude-haiku-5-5"}')
    install = tmp_path / "install.json"
    install.write_text('{"installed":true}')
    go = {
        "live_go": True,
        "package": package,
        "model": live.MODEL,
        "approval_digest": live.digest_json(approval),
        "merged_source_head": "0bfa591a7ee680f4e5e97d9aad72f204c48edbde",
        "binding_sha256": hashlib.sha256(binding.read_bytes()).hexdigest(),
        "config_sha256": live.HOST_CONFIG_SHA256,
        "ledger_id": live.LEDGER_ID,
        "install_receipt_path": str(install),
        "install_receipt_sha256": hashlib.sha256(install.read_bytes()).hexdigest(),
        "recipe_path": str(recipe),
        "recipe_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
    }
    if package == "F2-Anthropic":
        from daimon.testing.f2_native_bounds import native_budget, native_tools

        go["native_budget"] = native_budget()
        go["native_tools"] = native_tools()
    go_path = tmp_path / "go.json"
    go_path.write_text(json.dumps(go))
    now = signed_price_clock
    clean = True
    if corruption == "go":
        go_path.write_text("{}")
    elif corruption == "expiry":
        now += timedelta(days=1)
    elif corruption == "source":
        clean = False
    elif corruption == "recipe":
        recipe.write_text("{}")
    elif corruption == "config":
        config.write_text("{}")
    elif corruption == "binding":
        binding.write_text("{}")

    def invoke():
        return live.admit(
            package=package,
            approval_path=approval_path,
            go_path=go_path,
            go_digest=live.digest_json(go),
            config_path=config,
            ledger_path=Path(approval["ledger_path"]),
            binding_path=binding,
            output=tmp_path / "not-created",
            source_head=go["merged_source_head"],
            source_clean=clean,
            now=now,
        )

    if corruption:
        with pytest.raises(live.LiveRefused):
            invoke()
    else:
        admitted = invoke()
        assert admitted.hold == Decimal(".35" if package == "A7" else ".65")
        assert admitted.plan.container_allowance.sessions == (2 if package == "A7" else 1)
        assert admitted.deadline_s == (300 if package == "A7" else 600)
    assert not (tmp_path / "not-created").exists() and not (tmp_path / "never-read-bearer").exists()


async def test_export_credential_refusal_preserves_settlement_and_no_secret(tmp_path, monkeypatch):
    args, guard, _ = admission(tmp_path, monkeypatch)

    async def invoke(context):
        return {"status": "pass", "leaked": "offline-guarded-key"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-guarded-key",
        transport_factory=lambda: httpx.MockTransport(Native().handle),
    )
    receipt = (args.output / "receipt.json").read_text()
    assert "offline-guarded-key" not in receipt
    assert json.loads(receipt)["guard_settlement_persisted"] is True
    assert guard.report()[0].held_usd == Decimal(".35")


async def test_f2_lost_credential_ack_retires_vault_without_retry_or_session(tmp_path, monkeypatch):
    from daimon.testing.anthropic_live_cases import f2_default

    args, guard, _ = admission(tmp_path, monkeypatch, "F2-Anthropic")
    bearer = tmp_path / "synthetic-bearer"
    bearer.write_text("offline-only-bearer")
    bearer.chmod(0o600)
    args = replace(
        args,
        binding=args.binding.model_copy(
            update={
                "mcp_url": "https://mcp.owned.example/mcp",
                "mcp_bearer_file": bearer,
            }
        ),
    )
    calls = []

    async def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "id": "vault-lost",
                    "type": "vault",
                    "display_name": "offline",
                    "created_at": "2026-10-11T12:00:00Z",
                    "updated_at": "2026-10-11T12:00:00Z",
                    "metadata": {},
                },
            )
        if path == "/v1/vaults/vault-lost/credentials":
            raise httpx.ReadError("credential acknowledgement lost", request=request)
        if path == "/v1/vaults/vault-lost/archive":
            return httpx.Response(200, json={})
        raise AssertionError("unexpected provider request after credential failure")

    result = await live.run_live(
        args,
        f2_default,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    assert result["result"]["status"] == "failed"
    assert calls == [
        ("POST", "/v1/vaults"),
        ("POST", "/v1/vaults/vault-lost/credentials"),
        ("POST", "/v1/vaults/vault-lost/archive"),
    ]
    assert guard.report()[0].held_usd == Decimal(".65")
    assert len(receipts(guard)) == 2


async def test_caught_stream_stop_cannot_start_a_second_leg_after_archive(tmp_path, monkeypatch):
    from daimon.testing.ma import sse_response

    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def handler(request):
        if request.url.path.endswith("/events/stream"):
            return sse_response([usage("expensive", 1000000, 1)])
        return await native.handle(request)

    async def invoke(context):
        session = await create(context)
        stream = await context.driver.events.open_stream(SCOPE, session.ref)
        with pytest.raises(live.LiveStopped):
            async for _ in stream:
                pass
        await context.driver.session_admin.archive(SCOPE, session.ref, key="cleanup")
        with pytest.raises(ProviderError):
            await create(context)
        return {"status": "fail"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(handler),
    )
    assert native.created == 1
    assert guard.report()[0].held_usd > Decimal(".35")
    assert guard.report()[0].admission_blocked is True


async def test_monitor_signal_cleans_up_a_catching_callback_and_preserves_overrun(
    tmp_path, monkeypatch
):
    from daimon.testing.anthropic_live_cases import collect_usage
    from daimon.testing.ma import sse_response

    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def handler(request):
        if request.url.path.endswith("/events/stream"):
            return sse_response([usage("proof-overrun", 1000000, 1)])
        if request.method == "GET":
            raise httpx.ReadError("terminal walk failed", request=request)
        return await native.handle(request)

    async def invoke(context):
        session = await create(context)
        stream = await context.driver.events.open_stream(SCOPE, session.ref)
        try:
            async for _ in stream:
                pass
        except live.LiveStopped:
            with suppress(ProviderError):
                await collect_usage(context, session.ref)
            await asyncio.Event().wait()
        return {"status": "pass"}

    result = await asyncio.wait_for(
        live.run_live(
            args,
            invoke,
            secret_loader=lambda _: "offline-key",
            transport_factory=lambda: httpx.MockTransport(handler),
        ),
        timeout=5,
    )
    row = guard.report()[0]
    assert result["result"]["status"] == "failed"
    assert row.actual_usd is None and row.held_usd > Decimal(".35")
    assert row.admission_blocked is True
    assert len(receipts(guard)) == 2
    assert native.requests[-2:] == [
        ("POST", "/v1/sessions/sess-1/events"),
        ("POST", "/v1/sessions/sess-1/archive"),
    ]


async def test_cancellation_resistant_callback_cannot_grow_inventory_or_mutate_after_settlement(
    tmp_path, monkeypatch
):
    args, guard, _ = admission(tmp_path, monkeypatch)
    args = replace(args, deadline_s=1)
    native = Native()
    release = asyncio.Event()
    escaped = []
    finished = asyncio.Event()

    async def invoke(context):
        await create(context)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            escaped.append(context)
            with pytest.raises(ProviderError):
                await create(context)
            await release.wait()
            with pytest.raises(ProviderError):
                await create(context)
            finished.set()
        return {"status": "fail"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(native.handle),
    )
    assert escaped and escaped[0].transport.closed
    assert native.created == 1 and len(receipts(guard)) == 2
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=2)
    assert native.created == 1 and len(receipts(guard)) == 2


async def test_stream_and_final_walk_merge_each_native_request_once(tmp_path, monkeypatch):
    from daimon.testing.anthropic_live import merged_usage
    from daimon.testing.conformance.usage_settlement import AnthropicSessionSpend
    from mux.drivers.anthropic.usage import observation_from_event

    args, _, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def invoke(context):
        session = await create(context)
        event = usage("same-proof", 100, 10)
        context.transport.monitor.observe(event, session.ref.id)
        context.evidence.append(
            AnthropicSessionSpend(
                session.ref,
                (
                    observation_from_event(
                        event, session.ref, observed_at=datetime.now(UTC), model_id=live.MODEL
                    ),
                ),
                None,
            )
        )
        evidence = merged_usage(context)
        assert len(evidence) == 1 and len(evidence[0].observations) == 1
        assert evidence[0].usage_complete is False and evidence[0].runtime is None
        return {"status": "pass"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(native.handle),
    )


class FragmentedBytes(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def fragmented_usage(case, event):
    frame = b"event: span.model_request_end\r\ndata: " + json.dumps(event).encode() + b"\r\n\r\n"
    if case == "crlf":
        return [frame[:-3], frame[-3:-1], frame[-1:]]
    if case == "lf":
        lf = frame.replace(b"\r\n", b"\n")
        return [lf[:-1], lf[-1:]]
    if case == "json":
        split = frame.index(b"input_tokens") + 4
        return [frame[:split], frame[split : split + 7], frame[split + 7 :]]
    if case == "multi":
        return [b"event: ping\ndata: {}\n\n" + frame + b"event: ping\ndata: {}\n\n"]
    raise AssertionError(case)


@pytest.mark.parametrize("case", ["crlf", "lf", "json", "multi"])
async def test_sdk_decoded_fragmentation_never_bypasses_the_signed_stop(case, signed_price_clock):
    from pathlib import Path

    from anthropic import AsyncStream

    config = BudgetConfig.load(Path(__file__).parent / "fixtures/anthropic-live/host-config.json")
    provider = config.providers["anthropic"]
    event = usage("fragmented-proof", 1000000, 1)
    chunks = fragmented_usage(case, event)
    baseline = httpx.Response(200, stream=FragmentedBytes(chunks))
    decoded = [
        frame.json()
        async for frame in AsyncStream.raw_events(baseline)
        if frame.event == "span.model_request_end"
    ]
    assert decoded == [event]
    monitor = live.UsageMonitor(
        provider.models[live.MODEL], provider.session_prices[0], Decimal(".28")
    )
    downstream = httpx.Response(
        200, stream=live.MonitoredStream(FragmentedBytes(chunks), monitor, "accepted")
    )
    consumed = []
    with pytest.raises(live.LiveStopped, match="stop threshold"):
        async for frame in AsyncStream.raw_events(downstream):
            consumed.append(frame)
    assert monitor.stop.is_set() and monitor.cost >= Decimal(".50")
    assert list(monitor.observations) == [("accepted", event["id"])]
    assert all(frame.event != "span.model_request_end" for frame in consumed)


@pytest.mark.parametrize("case", ["crlf", "lf", "json", "multi"])
async def test_fragmented_stream_conflicting_walk_and_failed_fetch_keep_verified_floor(
    tmp_path, monkeypatch, case
):
    from daimon.testing.conformance.usage_settlement import AnthropicSessionSpend
    from mux.drivers.anthropic.usage import observation_from_event

    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def wire(request):
        if request.url.path.endswith("/events/stream"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=FragmentedBytes(
                    fragmented_usage(case, usage("conflicting-proof", 1000000, 1))
                ),
            )
        if request.method == "GET":
            raise httpx.ReadError("terminal fetch failed", request=request)
        return await native.handle(request)

    async def invoke(context):
        session = await create(context)
        stream = await context.driver.events.open_stream(SCOPE, session.ref)
        try:
            async for _ in stream:
                pass
        except live.LiveStopped:
            context.evidence.append(
                AnthropicSessionSpend(
                    session.ref,
                    (
                        observation_from_event(
                            usage("conflicting-proof", 100, 1),
                            session.ref,
                            observed_at=datetime.now(UTC),
                            model_id=live.MODEL,
                        ),
                    ),
                    None,
                )
            )
        return {"status": "fail"}

    result = await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(wire),
    )
    row = guard.report()[0]
    assert row.actual_usd is None and row.held_usd >= Decimal(".50")
    assert row.admission_blocked is True and len(receipts(guard)) == 2
    assert len(row.actual_evidence.requests) == 1
    assert row.actual_evidence.requests[0].tokens.input_tokens == 1000000
    assert result["spend"]["actual_usd"] is None
    exported = json.loads((args.output / "receipt.json").read_text())
    assert exported["conflicting_request_evidence"] == [
        hashlib.sha256(b"conflicting-proof").hexdigest()
    ]


async def test_late_inflight_create_ack_is_quarantined_and_retired_without_another_settlement(
    tmp_path, monkeypatch
):
    args, guard, _ = admission(tmp_path, monkeypatch)
    args = replace(args, deadline_s=1)
    release, finished = asyncio.Event(), asyncio.Event()
    contexts, calls = [], []

    async def wire(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/v1/sessions":
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return httpx.Response(
                200, json=ma_session(id="late-session", model=live.MODEL).model_dump(mode="json")
            )
        if path.endswith("/events"):
            assert [e["type"] for e in json.loads(request.content)["events"]] == ["user.interrupt"]
            return send_events_response()
        if path.endswith("/archive"):
            return httpx.Response(
                200, json=ma_session(id="late-session", model=live.MODEL).model_dump(mode="json")
            )
        raise AssertionError(path)

    async def invoke(context):
        contexts.append(context)
        try:
            with suppress(ProviderError):
                await create(context)
        finally:
            finished.set()
        return {"status": "fail"}

    try:
        await asyncio.wait_for(
            live.run_live(
                args,
                invoke,
                secret_loader=lambda _: "offline-key",
                transport_factory=lambda: httpx.MockTransport(wire),
            ),
            timeout=3,
        )
        context = contexts[0]
        assert context.transport.closed and not context.transport.session_ids
        assert not context.transport.authorization.created
        exported = json.loads((args.output / "receipt.json").read_text())
        assert exported["pending_create_acknowledgements"] == 1
        assert exported["reconciliation_required"] is True
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=3)
        assert not context.transport.session_ids and not context.transport.authorization.created
        assert context.transport.orphans == {("session", "late-session")}
        assert calls == [
            ("POST", "/v1/sessions"),
            ("POST", "/v1/sessions/late-session/events"),
            ("POST", "/v1/sessions/late-session/archive"),
        ]
        orphan = json.loads(next(args.output.glob("orphan-*.json")).read_text())
        assert orphan["interrupt_ack"] and orphan["retirement_ack"]
        assert orphan["reconciliation_required"] and orphan["hold_retained"]
        assert guard.report()[0].actual_usd is None and guard.report()[0].held_usd == Decimal(".35")
        assert len(receipts(guard)) == 2
    finally:
        release.set()


async def test_conflicting_more_expensive_walk_cannot_erase_stream_input_limit_latch(
    tmp_path, monkeypatch
):
    from daimon.testing.conformance.usage_settlement import AnthropicSessionSpend
    from mux.drivers.anthropic.usage import observation_from_event

    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def invoke(context):
        session = await create(context)
        context.transport.monitor.observe(usage("limit-proof", 200001, 1), session.ref.id)
        cheaper_input = usage("limit-proof", 0, 1)
        cheaper_input["model_usage"]["cache_creation_input_tokens"] = 190000
        cheaper_input["model_usage"]["cache_creation"]["ephemeral_1h_input_tokens"] = 190000
        context.evidence.append(
            AnthropicSessionSpend(
                session.ref,
                (
                    observation_from_event(
                        cheaper_input,
                        session.ref,
                        observed_at=datetime.now(UTC),
                        model_id=live.MODEL,
                    ),
                ),
                None,
            )
        )
        return {"status": "pass"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(native.handle),
    )
    row = guard.report()[0]
    assert row.actual_usd is None and row.held_usd >= Decimal(".35")
    assert row.admission_blocked is True and len(receipts(guard)) == 2
    assert row.actual_evidence.requests[0].tokens.input_tokens == 190000
    assert row.overrun_evidence.requests[0].tokens.input_tokens == 200001


@pytest.mark.parametrize("collision_source", ["walk", "stream", "cache_walk"])
async def test_cross_session_native_id_collision_quarantines_provenance_and_keeps_floor_and_latch(
    tmp_path, monkeypatch, collision_source
):
    from daimon.testing.conformance.usage_settlement import AnthropicSessionSpend
    from daimon.testing.ma import sse_response
    from mux.drivers.anthropic.usage import observation_from_event

    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()
    contexts = []
    id_ = "same-native-request"

    async def wire(request):
        if request.url.path.endswith("/events/stream"):
            return sse_response([usage(id_, 1000000, 1)])
        return await native.handle(request)

    async def invoke(context):
        contexts.append(context)
        prior = await create(context)
        if collision_source == "stream":
            context.transport.monitor.observe(usage(id_, 100, 1), prior.ref.id)
        elif collision_source == "cache_walk":
            context.transport.monitor.observe(usage(id_, 200001, 1), prior.ref.id)
        await context.driver.session_admin.archive(SCOPE, prior.ref, key="prior-archive")
        session = await create(context)
        if collision_source == "cache_walk":
            event = usage(id_, 0, 1)
            event["model_usage"]["cache_creation_input_tokens"] = 190000
            event["model_usage"]["cache_creation"] = {
                "ephemeral_5m_input_tokens": 0,
                "ephemeral_1h_input_tokens": 190000,
            }
            walk_ref = session.ref
        else:
            stream = await context.driver.events.open_stream(SCOPE, session.ref)
            with pytest.raises(live.LiveStopped):
                async for _ in stream:
                    pass
            event, walk_ref = usage(id_, 100, 1), prior.ref
        context.evidence.append(
            AnthropicSessionSpend(
                walk_ref,
                (
                    observation_from_event(
                        event, walk_ref, observed_at=datetime.now(UTC), model_id=live.MODEL
                    ),
                ),
                None,
            )
        )
        variants = live.verified_usage_variants(context)
        assert set(variants) == {(prior.ref.id, id_), (session.ref.id, id_)}
        selected = live.merged_usage(context)
        assert sum(len(item.observations) for item in selected) == 1
        assert selected[0].session == session.ref
        assert selected[0].observations[0].id == id_
        assert selected[0].runtime is None and selected[0].usage_complete is False
        # The shared global duplicate guard remains intact and conversion accepts
        # our one-witness lower-bound projection without manufacturing a new ID.
        live.actual_from_anthropic_sessions(
            context.reservation, selected, pricing_basis="standard-global"
        )
        return {"status": "fail"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(wire),
    )
    row = guard.report()[0]
    assert row.actual_usd is None and row.admission_blocked is True
    assert len(receipts(guard)) == 2
    assert row.held_usd >= (
        Decimal(".35") if collision_source == "cache_walk" else Decimal(".5000025")
    )
    assert len(row.actual_evidence.requests) == 1
    assert row.actual_evidence.requests[0].id == id_
    if collision_source == "cache_walk":
        assert row.actual_evidence.requests[0].tokens.input_tokens == 190000
        assert row.overrun_evidence.requests[0].tokens.input_tokens == 200001
    else:
        assert row.actual_evidence.requests[0].tokens.input_tokens == 1000000
    context = contexts[0]
    exported = json.loads((args.output / "receipt.json").read_text())
    assert exported["quarantined_native_request_ids"] == [hashlib.sha256(id_.encode()).hexdigest()]
    assert exported["reconciliation_required"] is True
    assert {tuple(item.values()) for item in exported["quarantined_request_provenance"]} == {
        (hashlib.sha256(session.encode()).hexdigest(), hashlib.sha256(id_.encode()).hexdigest())
        for session in context.transport.session_ids
    }
    if collision_source == "stream":
        assert set(context.transport.monitor.observations) == {
            (session, id_) for session in context.transport.session_ids
        }


@pytest.mark.parametrize("endpoint", ["events", "threads"])
@pytest.mark.parametrize("package", ["A7", "F2-Anthropic"])
@pytest.mark.parametrize(
    "content_type",
    [
        "text/event-stream",
        "Text/Event-Stream; charset=utf-8",
        "TEXT/EVENT-STREAM",
        None,
        "application/octet-stream",
    ],
)
async def test_stream_endpoint_is_monitored_regardless_of_native_content_type(
    tmp_path, monkeypatch, package, content_type, endpoint
):
    args, guard, _ = admission(tmp_path, monkeypatch, package)
    native = Native()

    async def wire(request):
        if request.url.path.endswith("/stream"):
            headers = {} if content_type is None else {"content-type": content_type}
            return httpx.Response(
                200,
                headers=headers,
                stream=FragmentedBytes(fragmented_usage("crlf", usage("header-proof", 2000000, 1))),
            )
        return await native.handle(request)

    async def invoke(context):
        session = await create(context)
        if endpoint == "threads":
            stream = await context.client.beta.sessions.threads.events.stream(
                session_id=session.ref.id,
                thread_id="root-owned",
            )
        else:
            stream = await context.driver.events.open_stream(SCOPE, session.ref)
        async for _ in stream:
            pytest.fail("over-budget SDK event escaped monitoring")
        return {"status": "pass"}

    result = await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(wire),
    )
    row = guard.report()[0]
    assert row.actual_usd is None and row.held_usd >= Decimal("1") and row.admission_blocked
    assert result["result"]["status"] != "pass" and len(receipts(guard)) == 2
    exported = json.loads((args.output / "receipt.json").read_text())
    stream_requests = [item for item in exported["requests"] if item["route"].endswith("/stream")]
    assert len(stream_requests) == 1
    assert stream_requests[0]["resource_id"] == hashlib.sha256(b"sess-1").hexdigest()
    if endpoint == "threads":
        assert stream_requests[0]["route"] == (
            "/v1/sessions/{resource_id}/threads/{thread_id}/stream"
        )
        assert stream_requests[0]["thread_id"] == hashlib.sha256(b"root-owned").hexdigest()
    else:
        assert stream_requests[0]["route"] == "/v1/sessions/{resource_id}/events/stream"
        assert "thread_id" not in stream_requests[0]


@pytest.mark.parametrize("case", ["open_stream_not_consumed", "late_stream_response"])
async def test_settlement_closes_open_or_inflight_sdk_response_and_refuses_late_delivery(
    tmp_path, monkeypatch, case
):
    args, guard, _ = admission(tmp_path, monkeypatch)
    args = replace(args, deadline_s=1)
    release, opened, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    native, sources, contexts = Native(), [], []

    class Source(FragmentedBytes):
        closed = False

        async def aclose(self):
            self.closed = True

    async def wait_resisting_cancel():
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    async def wire(request):
        if request.url.path.endswith("/stream"):
            if case == "late_stream_response":
                opened.set()
                await wait_resisting_cancel()
            source = Source(fragmented_usage("lf", usage("late-sdk-response", 1000000, 1)))
            sources.append(source)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)
        return await native.handle(request)

    async def invoke(context):
        contexts.append(context)
        try:
            session = await create(context)
            stream = await context.driver.events.open_stream(SCOPE, session.ref)
            if case == "open_stream_not_consumed":
                opened.set()
                await wait_resisting_cancel()
            with suppress(live.LiveStopped, ProviderError):
                async for _ in stream:
                    pytest.fail("post-settlement event escaped")
        except (live.LiveStopped, ProviderError):
            pass
        finally:
            finished.set()
        return {"status": "fail"}

    runner = asyncio.create_task(
        live.run_live(
            args,
            invoke,
            secret_loader=lambda _: "offline-key",
            transport_factory=lambda: httpx.MockTransport(wire),
        )
    )
    try:
        await asyncio.wait_for(opened.wait(), 3)
        await asyncio.wait_for(runner, 3)
        context = contexts[0]
        assert context.transport.closed and context.transport.monitor.settled
        assert all(source.closed for source in sources)
        release.set()
        await asyncio.wait_for(finished.wait(), 3)
        assert all(source.closed for source in sources)
        assert not context.transport.monitor.observations
        assert not list(
            args.ledger_path.parent.glob(live.late_usage_prefix(args.ledger_path) + "*.json")
        )
        assert len(receipts(guard)) == 2 and guard.report()[0].held_usd == Decimal(".35")
        assert all(response.is_closed for response in context.transport.responses)
    finally:
        release.set()


@pytest.mark.parametrize("package", ["A7", "F2-Anthropic"])
@pytest.mark.parametrize("arrival", ["after_settlement", "during_cleanup"])
async def test_late_active_stream_usage_is_durable_and_blocks_both_admission_entry_points(
    tmp_path, monkeypatch, arrival, package, credential=None, credential_format="plain"
):
    from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
    from daimon.testing.ma import sse_response
    from daimon.testing.ma_models import ma_model_usage

    args, guard, _ = admission(tmp_path, monkeypatch, package)
    late_input = 1000000 if package == "A7" else 2000000
    first, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    contexts, native, late_errors = [], Native(), []

    def sdk_usage(id_, incoming):
        if id_ == "late":
            id_ = "sevt_" + "L" * 24
        value = BetaManagedAgentsSpanModelRequestEndEvent(
            id=id_,
            type="span.model_request_end",
            model_request_start_id="start-" + id_,
            model_usage=ma_model_usage(input_tokens=incoming, output_tokens=1),
            processed_at=datetime.now(UTC),
        ).model_dump(mode="json")
        if id_ == "sevt_" + "L" * 24 and credential:
            import base64
            from urllib.parse import quote

            value["model_usage"]["provider_note"] = {
                "plain": credential,
                "base64": base64.b64encode(credential.encode()).decode(),
                "percent": quote(credential, safe=""),
                "half": credential[: len(credential) // 2],
            }[credential_format]
        return value

    class Source(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield sse_response([sdk_usage("first", 10)]).content
            first.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            yield sse_response([sdk_usage("late", late_input)]).content

        async def aclose(self):
            self.closed = True

    source = Source()

    async def wire(request):
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, stream=source)
        if request.url.path.endswith("/events") and arrival == "during_cleanup":
            release.set()
            await finished.wait()
        return await native.handle(request)

    async def invoke(context):
        contexts.append(context)
        if credential:
            context.redaction_secrets += (credential,)
        sink = context.transport.monitor.on_late_usage

        def capture(raw, session_id):
            try:
                sink(raw, session_id)
            except BaseException as error:
                late_errors.append(error)
                raise

        context.transport.monitor.on_late_usage = capture
        try:
            session = await create(context)
            stream = await context.driver.events.open_stream(SCOPE, session.ref)
            async for _ in stream:
                pass
        finally:
            finished.set()
        return {"status": "pass"}

    runner = asyncio.create_task(
        live.run_live(
            args,
            invoke,
            secret_loader=lambda _: "offline-key",
            transport_factory=lambda: httpx.MockTransport(wire),
        )
    )
    try:
        await asyncio.wait_for(first.wait(), 3)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        assert source.closed
        if arrival == "during_cleanup":
            assert finished.is_set() and not late_errors
            assert (
                guard.report()[0].held_usd >= (Decimal(".50") if package == "A7" else Decimal("1"))
                and guard.report()[0].admission_blocked
            )
            assert len(receipts(guard)) == 2
            assert not list(
                args.ledger_path.parent.glob(live.late_usage_prefix(args.ledger_path) + "*.json")
            )
            assert set(contexts[0].transport.monitor.observations) == {
                ("sess-1", "first"),
                ("sess-1", "sevt_" + "L" * 24),
            }
            return
        assert not finished.is_set()
        assert len(receipts(guard)) == 2 and args.output.joinpath("receipt.json").exists()
        release.set()
        await asyncio.wait_for(finished.wait(), 3)
        if credential:
            assert len(late_errors) == 1 and isinstance(late_errors[0], live.RecordingError)
            sidecars = list(
                args.ledger_path.parent.glob(live.late_usage_prefix(args.ledger_path) + "*.json")
            )
            assert len(sidecars) == 1
            persisted = sidecars[0].read_text()
            assert credential not in persisted and "proof" not in json.loads(persisted)
            assert json.loads(persisted)["admission_blocked"]
            assert len(receipts(guard)) == 2
            with pytest.raises(live.LiveRefused, match="unreconciled late usage"):
                live.refuse_late_usage(args.ledger_path)
            return
        assert not late_errors
        sidecars = list(
            args.ledger_path.parent.glob(live.late_usage_prefix(args.ledger_path) + "*.json")
        )
        assert len(sidecars) == 1 and sidecars[0].stat().st_mode & 0o777 == 0o600
        late = json.loads(sidecars[0].read_text())
        assert (
            late["admission_blocked"] and late["reconciliation_required"] and late["hold_retained"]
        )
        assert Decimal(late["retained_hold_usd"]) == args.hold
        assert (
            late["served_model_verified"]
            and late["proof"]["session"]["id"] == hashlib.sha256(b"sess-1").hexdigest()
        )
        assert (
            late["proof"]["id"] == hashlib.sha256(("sevt_" + "L" * 24).encode()).hexdigest()
            and late["proof"]["input_tokens"] == late_input
        )
        assert Decimal(late["verified_monetary_hold_floor_usd"]) >= (
            Decimal(".50") if package == "A7" else Decimal("1")
        )
        assert late["actual_usd"] is None and late["runtime"] is None
        assert len(receipts(guard)) == 2  # Sidecar is a latch, never a second settlement.
        assert set(contexts[0].transport.monitor.observations) == {("sess-1", "first")}
        calls_before = list(native.requests)
        for package in ("A7", "F2-Anthropic"):
            blocked = replace(
                args, package=package, approval_digest="c" * 64, output=tmp_path / package
            )
            with pytest.raises(live.LiveRefused, match="unreconciled late usage"):
                await live.run_live(
                    blocked,
                    invoke,
                    secret_loader=Mock(side_effect=AssertionError("key read")),
                    transport_factory=Mock(side_effect=AssertionError("client constructed")),
                )
            with pytest.raises(live.LiveRefused, match="unreconciled late usage"):
                live.admit(
                    package=package,
                    approval_path=tmp_path / "absent",
                    go_path=tmp_path / "absent",
                    go_digest="d" * 64,
                    config_path=args.config_path,
                    ledger_path=args.ledger_path,
                    binding_path=tmp_path / "absent",
                    output=tmp_path / package,
                    source_head=args.source_head,
                    source_clean=True,
                )
        assert native.requests == calls_before and len(receipts(guard)) == 2
    finally:
        release.set()


async def test_unknown_cache_ttl_original_keeps_the_pinned_guard_monetary_floor(
    tmp_path, monkeypatch
):
    from daimon.testing.conformance.usage_settlement import AnthropicSessionSpend
    from mux.drivers.anthropic.usage import observation_from_event

    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()

    async def invoke(context):
        prior = await create(context)
        await context.driver.session_admin.archive(SCOPE, prior.ref, key="prior-archive")
        session = await create(context)
        partial = usage("x", 0, 0)
        partial["model_usage"]["cache_creation_input_tokens"] = 800000
        del partial["model_usage"]["cache_creation"]
        cached = usage("x", 0, 0)
        cached["model_usage"]["cache_read_input_tokens"] = 1000000
        output = usage("x", 0, 1200000)
        for ref_, records in ((prior.ref, (partial,)), (session.ref, (cached, output))):
            context.evidence.append(
                AnthropicSessionSpend(
                    ref_,
                    tuple(
                        observation_from_event(
                            raw, ref_, observed_at=datetime.now(UTC), model_id=live.MODEL
                        )
                        for raw in records
                    ),
                    None,
                )
            )
        return {"status": "fail"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(native.handle),
    )
    row = guard.report()[0]
    assert row.actual_usd is None and row.held_usd == Decimal(".80") and row.admission_blocked
    assert len(receipts(guard)) == 2
    assert row.actual_evidence.requests[0].tokens.input_cache_write_tokens == 800000
    assert row.actual_evidence.requests[0].tokens.input_cache_write_1h_tokens is None
    assert row.overrun_evidence.requests[0].tokens.input_tokens == 1000000


@pytest.mark.parametrize("cancels", [1, 2, 3])
@pytest.mark.parametrize(
    "point",
    ["main", "bookkeeping", "interrupt", "archive", "retire", "stream-close", "client-close"],
)
async def test_repeated_cancellation_at_every_finalizer_await_preserves_cleanup_and_settlement(
    tmp_path, monkeypatch, cancels, point
):
    args, guard, _ = admission(tmp_path, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    native, contexts = Native(), []
    runner = None

    class Source(FragmentedBytes):
        closed = False

        async def aclose(self):
            self.closed = True
            if point == "stream-close":
                entered.set()
                await release.wait()

    source = Source(fragmented_usage("lf", usage("unused", 10, 1)))

    async def wire(request):
        if request.url.path.endswith("/events/stream"):
            return httpx.Response(200, stream=source)
        if request.url.path.endswith("/events") and point in {"interrupt", "main", "bookkeeping"}:
            if point == "interrupt":
                entered.set()
            await release.wait()
        if request.url.path.endswith("/archive") and point == "archive":
            entered.set()
            await release.wait()
        return await native.handle(request)

    class Wire(httpx.MockTransport):
        async def aclose(self):
            if point == "client-close":
                entered.set()
                await release.wait()
            await super().aclose()

    async def invoke(context):
        contexts.append(context)
        await create(context)
        if point == "main":
            entered.set()
            await release.wait()
        if point == "bookkeeping":

            def recancel(watcher):
                runner.cancel()
                entered.set()

            watchers = [
                watcher
                for watcher in asyncio.all_tasks()
                if getattr(watcher.get_coro(), "cr_frame", None) is not None
                and watcher.get_coro().cr_frame.f_locals.get("self")
                is context.transport.monitor.stop
            ]
            assert len(watchers) == 1
            watchers[0].add_done_callback(recancel)
        if point == "retire":

            async def retire():
                entered.set()
                await release.wait()

            context.retire = retire
        if point == "stream-close":
            await context.driver.events.open_stream(SCOPE, context.ref("sess-1", "session"))
        return {"status": "pass"}

    runner = asyncio.create_task(
        live.run_live(
            args,
            invoke,
            secret_loader=lambda _: "offline-key",
            transport_factory=lambda: Wire(wire),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        for _ in range(cancels - (1 if point == "bookkeeping" else 0)):
            runner.cancel()
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runner, 3)
        context = contexts[0]
        assert context.transport.closed and context.transport.monitor.settled
        assert context.client.is_closed() and context.transport.archived == {"sess-1"}
        assert ("POST", "/v1/sessions/sess-1/events") in native.requests
        assert ("POST", "/v1/sessions/sess-1/archive") in native.requests
        assert len(receipts(guard)) == 2 and guard.report()[0].actual_usd is None
        assert args.output.joinpath("receipt.json").exists()
        if point == "stream-close":
            assert source.closed
    finally:
        release.set()


async def test_native_create_ack_body_is_not_read_twice_before_sdk_consumption(
    tmp_path, monkeypatch
):
    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()
    bodies = []

    class Once(httpx.AsyncByteStream):
        def __init__(self, content):
            self.content, self.reads = content, 0

        async def __aiter__(self):
            self.reads += 1
            if self.reads > 1:
                raise RuntimeError("native response body cannot be replayed")
            yield self.content

    async def wire(request):
        response = await native.handle(request)
        if request.url.path == "/v1/sessions":
            source = Once(response.content)
            bodies.append(source)
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=source)
        return response

    async def invoke(context):
        session = await create(context)
        assert session.ref.id == "sess-1"
        return {"status": "pass"}

    result = await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(wire),
    )
    assert result["result"]["status"] == "pass"
    assert bodies[0].reads == 1 and len(receipts(guard)) == 2


@pytest.mark.parametrize(
    "split",
    ["absent", None, {}, {"ephemeral_5m_input_tokens": 100}, {"ephemeral_1h_input_tokens": 100}],
)
def test_real_a7_cache_write_counts_keep_unknown_ttl_and_use_conservative_monitor(
    split, signed_price_clock
):
    from pathlib import Path

    from daimon.testing.conformance.budget import TokenUsage
    from mux.drivers.anthropic.usage import observation_from_event

    config = BudgetConfig.load(Path(__file__).parent / "fixtures/anthropic-live/host-config.json")
    provider = config.providers["anthropic"]
    price = provider.models[live.MODEL]
    event = usage("real-count-reconstruction", 4, 13)
    event["model_usage"]["cache_creation_input_tokens"] = 642
    if split == "absent":
        del event["model_usage"]["cache_creation"]
    else:
        event["model_usage"]["cache_creation"] = split
    before = json.dumps(event, sort_keys=True)
    at = datetime(2026, 10, 11, 15, tzinfo=UTC)
    observation = observation_from_event(event, ref("session", "session"), observed_at=at)
    durations = split if isinstance(split, dict) else {}
    tokens = TokenUsage(
        input_tokens=646,
        output_tokens=13,
        input_cached_tokens=0,
        input_cache_write_tokens=642,
        input_cache_write_5m_tokens=durations.get("ephemeral_5m_input_tokens"),
        input_cache_write_1h_tokens=durations.get("ephemeral_1h_input_tokens"),
    )
    assert price.actual(tokens, at) is None
    assert live.monitor_token_estimate(price, tokens, at) == Decimal(".0006489")
    monitor = live.UsageMonitor(price, provider.session_prices[0], Decimal(".28"))
    monitor.observe(event, "accepted-session")
    assert not monitor.stop.is_set() and monitor.cost >= Decimal(".0006489")
    assert observation.native_meter == event["model_usage"]
    assert json.dumps(event, sort_keys=True) == before


def test_complete_ttl_split_keeps_exact_pricing_and_stale_price_stays_unknown():
    from pathlib import Path

    from daimon.testing.conformance.budget import TokenUsage

    price = (
        BudgetConfig.load(Path(__file__).parent / "fixtures/anthropic-live/host-config.json")
        .providers["anthropic"]
        .models[live.MODEL]
    )
    tokens = TokenUsage(
        input_tokens=646,
        output_tokens=13,
        input_cached_tokens=0,
        input_cache_write_tokens=642,
        input_cache_write_5m_tokens=642,
        input_cache_write_1h_tokens=0,
    )
    at = datetime(2026, 10, 11, 15, tzinfo=UTC)
    assert (
        live.monitor_token_estimate(price, tokens, at)
        == price.actual(tokens, at)
        == Decimal(".00008715")
    )
    unknown = tokens.model_copy(
        update={"input_cache_write_5m_tokens": None, "input_cache_write_1h_tokens": None}
    )
    assert live.monitor_token_estimate(price, unknown, datetime(2026, 10, 12, tzinfo=UTC)) is None


@pytest.mark.parametrize("split", ["absent", None, {"ephemeral_5m_input_tokens": 10}])
def test_unknown_ttl_conservative_monitor_estimate_can_trigger_stop(split, tmp_path, monkeypatch):
    _, _, config = admission(tmp_path, monkeypatch)
    provider = config.providers["anthropic"]
    event = usage("unknown-write-threshold", 0, 0)
    event["model_usage"]["cache_creation_input_tokens"] = 1000000
    if split == "absent":
        del event["model_usage"]["cache_creation"]
    else:
        event["model_usage"]["cache_creation"] = split
    monitor = live.UsageMonitor(
        provider.models[live.MODEL], provider.session_prices[0], Decimal(".28")
    )
    with pytest.raises(live.LiveStopped, match="stop threshold"):
        monitor.observe(event)
    assert monitor.cost >= Decimal(".28") and monitor.stop.is_set()


@pytest.mark.parametrize(
    "invalid",
    [-1, True, "642", {"ephemeral_5m_input_tokens": -1}, {"ephemeral_5m_input_tokens": 643}],
)
def test_unknown_ttl_does_not_accept_invalid_splits(invalid, tmp_path, monkeypatch):
    _, _, config = admission(tmp_path, monkeypatch)
    provider = config.providers["anthropic"]
    event = usage("invalid-ttl", 4, 13)
    event["model_usage"]["cache_creation_input_tokens"] = 642
    event["model_usage"]["cache_creation"] = invalid
    monitor = live.UsageMonitor(
        provider.models[live.MODEL], provider.session_prices[0], Decimal(".28")
    )
    with pytest.raises(live.LiveStopped, match="malformed"):
        monitor.observe(event)


@pytest.mark.parametrize(
    "suffix,method", [("events/stream", "GET"), ("events", "POST"), ("archive", "POST")]
)
def test_real_length_session_paths_have_audited_typed_receipt_metadata(suffix, method):
    from daimon.testing.conformance.recording import Audit, RecordingError

    id_ = "sesn_" + "A" * 26
    path = f"/v1/sessions/{id_}/{suffix}"
    with pytest.raises(RecordingError, match="ambiguous encoded content"):
        Audit(()).audit(path)
    projected = live.request_metadata([(method, path)], frozenset({("session", id_)}))
    assert projected == [
        {
            "method": method,
            "route": "/v1/sessions/{resource_id}/" + suffix,
            "resource_id": id_,
            "version": None,
        }
    ]
    Audit(()).audit(projected)


@pytest.mark.parametrize("id_", ["Bearer " + "synthetic-only-secret", "sk-" + "x" * 40])
def test_typed_receipt_resource_identity_never_exempts_credentials(id_):
    from daimon.testing.conformance.recording import Audit, RecordingError

    projected = live.request_metadata(
        [("GET", f"/v1/sessions/{id_}/events/stream")], frozenset({("session", id_)})
    )
    with pytest.raises(RecordingError):
        Audit(()).audit(projected)


def test_thread_stream_receipt_audits_identity_and_requires_accepted_parent():
    from daimon.testing.conformance.recording import Audit, RecordingError

    id_ = "sesn_" + "A" * 26
    thread_id = "thread_" + "B" * 26
    path = f"/v1/sessions/{id_}/threads/{thread_id}/stream"
    projected = live.request_metadata([("GET", path)], frozenset({("session", id_)}))
    Audit(()).audit(projected)
    assert projected[0]["thread_id"] == thread_id
    assert projected[0]["resource_id"] == id_
    with pytest.raises(RecordingError, match="unaccepted live request identity"):
        live.request_metadata([("GET", path)], frozenset())
    for secret in ("Bearer synthetic-only-secret", "sk-" + "x" * 40):
        projected = live.request_metadata(
            [("GET", f"/v1/sessions/{id_}/threads/{secret}/stream")],
            frozenset({("session", id_)}),
        )
        with pytest.raises(RecordingError):
            Audit(()).audit(projected)


@pytest.mark.parametrize(
    "method,path,resources",
    [
        ("GET", "/unknown", frozenset()),
        ("GET", "/v1/sessions/foreign/events", frozenset()),
        ("POST", "/v1/sessions", frozenset()),
        ("GET", "/v1/sessions/owned/unknown", frozenset({("session", "owned")})),
        ("GET", "/v1/sessions/owned/events?key=untrusted", frozenset({("session", "owned")})),
    ],
)
def test_receipt_metadata_refuses_unknown_routes_and_foreign_identities(method, path, resources):
    from daimon.testing.conformance.recording import RecordingError

    if path == "/v1/sessions":
        assert live.request_metadata([(method, path)], resources)[0]["resource_id"] is None
    else:
        with pytest.raises(RecordingError):
            live.request_metadata([(method, path)], resources)


@pytest.mark.parametrize(
    "field",
    ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"],
)
def test_unknown_ttl_monitor_never_prices_missing_aggregate_counts(field, tmp_path, monkeypatch):
    _, _, config = admission(tmp_path, monkeypatch)
    provider = config.providers["anthropic"]
    event = usage("missing-aggregate", 4, 13)
    del event["model_usage"]["cache_creation"]
    event["model_usage"]["cache_creation_input_tokens"] = 642
    event["model_usage"][field] = None
    monitor = live.UsageMonitor(
        provider.models[live.MODEL], provider.session_prices[0], Decimal(".28")
    )
    with pytest.raises(live.LiveStopped, match="unknown"):
        monitor.observe(event)


async def test_real_length_native_session_and_unknown_write_ttl_export_without_changing_hold(
    tmp_path, monkeypatch
):
    from daimon.testing.ma import sse_response

    args, guard, _ = admission(tmp_path, monkeypatch)
    native = Native()
    id_ = "sesn_" + "A" * 26
    event = usage("request-live-regression", 4, 13)
    event["processed_at"] = datetime.now(UTC).isoformat()
    event["model_request_start_id"] = "request-start-regression"
    event["model_usage"]["cache_creation_input_tokens"] = 642
    del event["model_usage"]["cache_creation"]

    async def wire(request):
        if request.url.path == "/v1/sessions":
            native.created += 1
            return httpx.Response(
                200,
                json=ma_session(id=id_, agent_id="agent-test", model=live.MODEL).model_dump(
                    mode="json"
                ),
            )
        if request.url.path.endswith("/events/stream"):
            return sse_response([event])
        return await native.handle(request)

    async def invoke(context):
        session = await create(context)
        stream = await context.driver.events.open_stream(SCOPE, session.ref)
        async for _ in stream:
            pass
        return {"status": "pass"}

    await live.run_live(
        args,
        invoke,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(wire),
    )
    receipt = json.loads((args.output / "receipt.json").read_text())
    assert receipt["result"]["status"] == "pass"
    assert receipt["stream_stop_reason"] is None
    assert receipt["observed_request_meters"][0]["cache_creation_input_tokens"] == 642
    assert "cache_creation" not in receipt["observed_request_meters"][0]
    assert any(
        item["route"] == "/v1/sessions/{resource_id}/events/stream"
        and item["resource_id"] == hashlib.sha256(id_.encode()).hexdigest()
        for item in receipt["requests"]
    )
    assert receipt["archived_sessions"] == [hashlib.sha256(id_.encode()).hexdigest()]
    assert len(receipts(guard)) == 2 and guard.report()[0].actual_usd is None
    assert guard.report()[0].held_usd == Decimal(".35")


@pytest.mark.parametrize("credential_format", ["plain", "base64", "percent", "half"])
async def test_f2_late_meter_never_persists_registered_mcp_credential(
    tmp_path, monkeypatch, credential_format
):
    await test_late_active_stream_usage_is_durable_and_blocks_both_admission_entry_points(
        tmp_path,
        monkeypatch,
        "after_settlement",
        "F2-Anthropic",
        credential="mcp-private:fixture+canary-638492",
        credential_format=credential_format,
    )


async def test_budget_acceptance_probe_is_one_create_retrieve_archive_and_no_message(
    tmp_path, monkeypatch
):
    from dataclasses import fields

    from daimon.testing.f2_budget_acceptance import AcceptanceAdmission, acceptance
    from daimon.testing.f2_native_bounds import native_budget, native_tools
    from daimon.testing.ma_models import ma_agent

    args, guard, _ = admission(tmp_path, monkeypatch, "F2-Anthropic")
    args = AcceptanceAdmission(**{field.name: getattr(args, field.name) for field in fields(args)})
    native = Native()
    agent = ma_agent(id="agent-test", model=live.MODEL, tools=native_tools())

    async def wire(request):
        if request.method == "GET" and request.url.path == "/v1/agents/agent-test":
            native.requests.append((request.method, request.url.path))
            return httpx.Response(200, json=agent.model_dump(mode="json"))
        if request.url.path == "/v1/sessions" and request.method == "POST":
            assert json.loads(request.content)["budget"] == native_budget()
        if request.method == "GET" and request.url.path == "/v1/sessions/sess-1":
            native.requests.append((request.method, request.url.path))
            return httpx.Response(
                200,
                json={
                    **ma_session(id="sess-1", model=live.MODEL).model_dump(mode="json"),
                    "agent": agent.model_dump(mode="json"),
                    "budget": native_budget(),
                },
            )
        response = await native.handle(request)
        if (
            request.url.path.startswith("/v1/sessions")
            and request.method in {"POST", "GET"}
            and not request.url.path.endswith("/archive")
        ):
            body = response.json()
            body["budget"] = native_budget()
            body["agent"] = agent.model_dump(mode="json")
            return httpx.Response(200, json=body)
        return response

    result = await live.run_live(
        args,
        acceptance,
        secret_loader=lambda _: "offline-key",
        transport_factory=lambda: httpx.MockTransport(wire),
    )
    assert result["result"]["status"] == "pass", result
    assert result["result"]["user_messages"] == 0
    assert result["result"]["model_tokens_requested"] == 0
    assert result["result"]["enforcement_tested"] is False
    assert native.requests == [
        ("GET", "/v1/agents/agent-test"),
        ("POST", "/v1/sessions"),
        ("GET", "/v1/sessions/sess-1"),
        ("POST", "/v1/sessions/sess-1/archive"),
    ]
    assert len(receipts(guard)) == 2 and guard.report()[0].held_usd == Decimal(".02")
    assert guard.report()[0].actual_usd is None


LATE_CREDENTIAL_ROLES = {
    "api_key": ("current", "fixture.api:648219+alpha"),
    "mcp_primary": ("current", "fixture.mcp:749321+bravo"),
    "vault_static_bearer_alias": ("current-alias", "fixture.mcp:749321+bravo"),
    "mcp_secondary": ("conditional", "fixture.mcp2:836472+charlie"),
    "oauth_access_token": ("conditional", "fixture.access:927351+delta"),
    "oauth_refresh_token": ("conditional", "fixture.refresh:138642+echo"),
    "oauth_client_secret": ("conditional", "fixture.client:249753+foxtrot"),
    "vault_environment_secret": ("conditional", "fixture.env:351864+golf"),
    "skill_environment_secret": ("conditional", "fixture.skill:462975+hotel"),
    "mcp_authorization_token": ("conditional", "fixture.auth:573186+india"),
    "credential_file_secret": ("conditional", "fixture.file:684297+juliet"),
}


def late_credential_encoding(value, form):
    import base64

    if form == "base64":
        return base64.b64encode(value.encode()).decode()
    if form == "percent":
        return "".join(f"%{byte:02X}" for byte in value.encode())
    return value


async def late_credential_probe(
    tmp_path,
    monkeypatch,
    role,
    form,
    *,
    id_fragments=False,
    wrapped=False,
    after_refusal=False,
    native_ids=None,
    valid_ids=False,
):

    from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
    from daimon.testing.ma import sse_response
    from daimon.testing.ma_models import ma_model_usage

    classification, canary = LATE_CREDENTIAL_ROLES[role]
    if id_fragments:
        canary = "fixture_mcp_749321_bravo"
    folder = tmp_path / f"{role}-{form}"
    folder.mkdir(parents=True)
    mp = monkeypatch
    args, guard, _ = admission(folder, mp, "F2-Anthropic")
    # Run fixture preflight without inheriting any ambient SDK auth/headers.
    native, contexts, errors = Native(), [], []
    count = (3 if after_refusal else 2) if form == "split_frames" else (2 if after_refusal else 1)
    if native_ids is not None:
        count = len(native_ids)
    release = asyncio.Event()
    waiting = [asyncio.Event() for _ in range(count)]
    sources = []
    payload = late_credential_encoding(canary, form)
    parts = (
        [payload[: len(payload) // 2], payload[len(payload) // 2 :]]
        if form == "split_frames"
        else [payload]
    )

    if wrapped:
        parts = ["late-" + parts[0], parts[1] + "-extra"]
    if after_refusal:
        parts.append("safe-late-followup")
    if native_ids is not None:
        parts = native_ids

    def event(id_, incoming, note=None):
        raw = BetaManagedAgentsSpanModelRequestEndEvent(
            id=id_,
            type="span.model_request_end",
            model_request_start_id="start-" + id_,
            model_usage=ma_model_usage(input_tokens=incoming, output_tokens=1),
            processed_at=datetime.now(UTC),
        ).model_dump(mode="json")
        if note is not None:
            if id_fragments:
                raw["id"] = note
            else:
                raw["model_usage"]["provider_note"] = note
        return raw

    class Source(httpx.AsyncByteStream):
        def __init__(self, index):
            self.index, self.closed = index, False

        async def __aiter__(self):
            yield sse_response([event(f"first-{self.index}", 10)]).content
            waiting[self.index].set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            frame = sse_response([event(f"late-{self.index}", 2000000, parts[self.index])]).content
            if form == "split_http_chunks":
                # Split inside the credential, not between two complete frames.
                cut = frame.index(payload.encode()) + len(payload) // 2
                yield frame[:cut]
                yield frame[cut:]
            else:
                yield frame

        async def aclose(self):
            self.closed = True

    async def wire(request):
        if request.url.path.endswith("/events/stream"):
            source = Source(len(sources))
            sources.append(source)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=source)
        return await native.handle(request)

    readers = []

    async def invoke(context):
        contexts.append(context)
        context.redaction_secrets += tuple(
            value for name, (_, value) in LATE_CREDENTIAL_ROLES.items() if name != "api_key"
        )
        context.redaction_secrets += (canary,)
        sink = context.transport.monitor.on_late_usage

        def capture(raw, session_id):
            try:
                sink(raw, session_id)
            except BaseException as error:
                errors.append(type(error).__name__)
                raise

        context.transport.monitor.on_late_usage = capture
        session = await create(context)

        async def read():
            stream = await context.driver.events.open_stream(SCOPE, session.ref)
            with suppress(Exception):
                async for _ in stream:
                    pass

        readers.extend(asyncio.create_task(read()) for _ in range(count))
        await asyncio.gather(*readers)
        return {"status": "pass"}  # Synthetic fixture result, never QA certification.

    runner = asyncio.create_task(
        live.run_live(
            args,
            invoke,
            secret_loader=lambda _: LATE_CREDENTIAL_ROLES["api_key"][1],
            transport_factory=lambda: httpx.MockTransport(wire),
        )
    )
    try:
        await asyncio.wait_for(asyncio.gather(*(gate.wait() for gate in waiting)), 5)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runner, 5)
        assert all(source.closed for source in sources)
        assert len(receipts(guard)) == 2
        assert not all(reader.done() for reader in readers)
        release.set()
        await asyncio.wait_for(asyncio.gather(*readers), 5)
        paths = sorted(
            args.ledger_path.parent.glob(live.late_usage_prefix(args.ledger_path) + "*.json")
        )
        sidecars = [json.loads(path.read_text()) for path in paths]
        assert len(sidecars) == count
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
        assert all(
            row["admission_blocked"] and row["reconciliation_required"] and row["hold_retained"]
            for row in sidecars
        )
        assert all(row["actual_usd"] is None and row["runtime"] is None for row in sidecars)
        assert len(receipts(guard)) == 2
        # Inspect only meter content, without hash/id fields confusing tape audit.
        notes = sorted(
            (
                row["proof"]["id"],
                row["proof"]["id"]
                if id_fragments
                else row["proof"]["native_meter"].get("provider_note"),
            )
            for row in sidecars
            if "proof" in row
        )
        retained = [note for _, note in notes if note is not None]
        oracle_rejects = False
        try:
            live.Audit((canary,)).fragments(parts)
        except live.RecordingError:
            oracle_rejects = True
        if native_ids is None:
            assert oracle_rejects, (
                "probe payload must be credential-bearing under full-context audit"
            )
        # A refusal must leave no payload fragments in durable native meters.
        # Rejected provider IDs cannot create a proof, even when the audit
        # cannot reconstruct interleaved or reversed credential fragments.
        if valid_ids:
            serialized = json.dumps(sidecars)
            assert all(part not in serialized for part in parts)
            assert len(retained) == 1 and len(errors) == count - 1
            assert all(not row["native_id_projection"]["shape_rejected"] for row in sidecars)
            assert (
                sum(row["native_id_projection"]["duplicate_rejected"] for row in sidecars)
                == count - 1
            )
            verdict = "PASS"
        else:
            verdict = "PASS" if not retained else "FAIL"
        if id_fragments and not after_refusal and not valid_ids:
            assert len(errors) == count
            for row in sidecars:
                assert row["native_id_projection"]["shape_rejected"]
            assert {row["native_id_projection"]["sha256"] for row in sidecars} == {
                hashlib.sha256(part.encode()).hexdigest() for part in parts
            }
        assert guard.report()[0].actual_usd is None
        assert guard.report()[0].held_usd == args.hold
        assert all(Decimal(row["retained_hold_usd"]) == args.hold for row in sidecars)
        calls_before = list(native.requests)
        for package in ("A7", "F2-Anthropic"):
            blocked = replace(
                args, package=package, approval_digest="c" * 64, output=folder / package
            )
            with pytest.raises(live.LiveRefused, match="unreconciled late usage"):
                await live.run_live(
                    blocked,
                    invoke,
                    secret_loader=Mock(side_effect=AssertionError("key read")),
                    transport_factory=Mock(side_effect=AssertionError("client construction")),
                )
            with pytest.raises(live.LiveRefused, match="unreconciled late usage"):
                live.admit(
                    package=package,
                    approval_path=folder / "absent",
                    go_path=folder / "absent",
                    go_digest="d" * 64,
                    config_path=args.config_path,
                    ledger_path=args.ledger_path,
                    binding_path=folder / "absent",
                    output=folder / package,
                    source_head=args.source_head,
                    source_clean=True,
                )
        assert native.requests == calls_before and len(receipts(guard)) == 2
        return {
            "role": role,
            "classification": classification,
            "form": form,
            "verdict": verdict,
            "credential_sha256": hashlib.sha256(canary.encode()).hexdigest(),
            "sidecars": len(sidecars),
            "retained_meter_fragments": len(retained),
            "late_errors": errors,
            "full_context_payload_oracle": "REFUSE",
            "ledger_rows": 2,
            "A7_F2_readmission": "REFUSED_BEFORE_AUTH_OR_CLIENT",
            "sidecar_mode": "0600",
            "fake_sources_closed": True,
        }
    finally:
        release.set()
        if not runner.done():
            runner.cancel()


@pytest.mark.parametrize("role", list(LATE_CREDENTIAL_ROLES))
@pytest.mark.parametrize(
    "form", ["plain", "base64", "percent", "split_frames", "split_http_chunks"]
)
async def test_all_registered_late_meter_credential_forms_refuse(tmp_path, monkeypatch, role, form):
    result = await late_credential_probe(tmp_path, monkeypatch, role, form)
    assert result["verdict"] == "PASS"


@pytest.mark.parametrize("wrapped", [False, True])
async def test_two_late_native_ids_cannot_complete_a_registered_mcp_credential(
    tmp_path, monkeypatch, wrapped
):
    result = await late_credential_probe(
        tmp_path, monkeypatch, "mcp_primary", "split_frames", id_fragments=True, wrapped=wrapped
    )
    assert result["verdict"] == "PASS"


def test_late_audit_includes_trusted_fields_but_does_not_decode_owned_sha_pins(monkeypatch):
    pin = "f" * 64
    monkeypatch.setattr(live, "HOST_CONFIG_SHA256", pin)
    records = [
        {
            "config_sha256": pin,
            "ledger_id": live.LEDGER_ID,
            "proof": {"id": "safe", "input_tokens": 1},
        }
    ]
    live.audit_late_usage_records(records, ("unrelated-fixture-credential",))
    with pytest.raises(live.RecordingError):
        live.audit_late_usage_records(records, (pin,))


def test_late_audit_includes_cross_frame_fields_and_keys(monkeypatch):
    # Both individual records are safe. Different durable slots assemble the
    # credential in the all-fields stream even without a shared field path.
    first = {"host": "left_", "proof": {"id": "middle_"}}
    second = {"host": "right", "proof": {"id": "safe"}}
    secret = "left_middle_right"
    live.audit_late_usage_records([first], (secret,))
    live.audit_late_usage_records([second], (secret,))
    with pytest.raises(live.RecordingError):
        live.audit_late_usage_records([first, second], (secret,))


async def test_late_enrichment_stays_closed_after_a_split_credential_hit(tmp_path, monkeypatch):
    result = await late_credential_probe(
        tmp_path,
        monkeypatch,
        "mcp_primary",
        "split_frames",
        id_fragments=True,
        wrapped=True,
        after_refusal=True,
    )
    assert result["verdict"] == "PASS" and result["sidecars"] == 3


async def test_late_enrichment_stays_closed_after_a_single_frame_credential_hit(
    tmp_path, monkeypatch
):
    result = await late_credential_probe(
        tmp_path,
        monkeypatch,
        "mcp_primary",
        "plain",
        id_fragments=True,
        after_refusal=True,
    )
    assert result["verdict"] == "PASS" and result["sidecars"] == 2
    assert result["late_errors"] == ["RecordingError"] * 2


@pytest.mark.parametrize("variant", ["interleaved", "reverse", "base64_interleaved"])
async def test_late_native_ids_close_the_three_interleaved_credential_channels(
    tmp_path, monkeypatch, variant
):
    import base64

    canary = "fixture_mcp_749321_bravo"
    first, second = canary[:12], canary[12:]
    cases = {
        "interleaved": ["x" + first, "benignevent123", second + "y"],
        "reverse": [second + "y", "x" + first],
    }
    encoded = base64.b64encode(canary.encode()).decode().rstrip("=")
    cases["base64_interleaved"] = ["x" + encoded[:16], "benignevent123", encoded[16:] + "y"]
    result = await late_credential_probe(
        tmp_path,
        monkeypatch,
        "mcp_primary",
        "split_frames",
        id_fragments=True,
        native_ids=cases[variant],
    )
    assert result["verdict"] == "PASS" and result["ledger_rows"] == 2
    assert result["late_errors"] == ["RecordingError"] * len(cases[variant])


def test_durable_native_identity_hashes_even_valid_shapes_and_flags_duplicates():
    ids = live.DurableNativeIds()
    native = "sevt_" + "A" * 24
    first = ids.project(native)
    assert first == {
        "sha256": hashlib.sha256(native.encode()).hexdigest(),
        "shape_rejected": False,
        "duplicate_rejected": False,
    }
    assert ids.project(native)["duplicate_rejected"] is True
    for invalid in [
        "xcredentialhalf",
        "benignevent123",
        "sevt_" + "A" * 19,
        "sevt_" + "A" * 41,
        native + "\n",
        "sesn_" + "A" * 24 + "_",
    ]:
        assert ids.project(invalid)["shape_rejected"] is True
    assert ids.project("sesn_" + "B" * 24)["shape_rejected"] is False


def test_receipt_projection_closes_native_identity_and_free_text_fields():
    ids = live.DurableNativeIds()
    native = "sevt_" + "A" * 24
    session = "sesn_" + "B" * 24
    text = "some-provider-controlled-text"
    facts = {
        "result": {"status": "pass", "evidence": [text], "unexpected_provider_key": text},
        "created_resources": [["session", session]],
        "archived_sessions": [session],
        "requests": [{"resource_id": session, "thread_id": text, "version": text}],
        "observed_request_threshold_status": [{"id": native, "session_id": session}],
        "spend": {"actual_evidence": {"requests": [{"id": native}]}},
        "observed_request_meters": [{"provider_note": text, "input_tokens": 3}],
        "observed_token_threshold_crossings": [native],
    }
    projected = live.durable_receipt(facts, ids, ())
    serialized = json.dumps(projected)
    assert native not in serialized and session not in serialized and text not in serialized
    assert "unexpected_provider_key" not in serialized and "provider_note" not in serialized
    assert projected["result"]["status"] == "pass"
    assert facts["created_resources"] == [["session", session]]  # Original guard input untouched.
    assert hashlib.sha256(native.encode()).hexdigest() in serialized
    assert any(row["duplicate_rejected"] for row in projected["native_id_projection"])


def test_projected_hashes_remain_in_full_literal_credential_audit():
    ids = live.DurableNativeIds()
    native = "sevt_" + "A" * 24
    secret = hashlib.sha256(native.encode()).hexdigest()
    with pytest.raises(live.RecordingError):
        live.durable_receipt({"id": native}, ids, (secret,))


async def test_duplicate_shape_valid_late_identity_is_hashed_and_closes_enrichment(
    tmp_path, monkeypatch
):
    native = "sevt_" + "A" * 24
    result = await late_credential_probe(
        tmp_path,
        monkeypatch,
        "mcp_primary",
        "split_frames",
        id_fragments=True,
        native_ids=[native, native],
        valid_ids=True,
    )
    assert result["verdict"] == "PASS" and result["sidecars"] == 2
    assert result["late_errors"] == ["RecordingError"]
    assert result["ledger_rows"] == 2
