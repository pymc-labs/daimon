"""No network: actual SDK serializes the documented C03 create/readback policy."""

from __future__ import annotations

import json
from copy import deepcopy

import httpx
import pytest
from mux.contracts.actions import UserMessage
from mux.contracts.events import TextPart
from mux.contracts.ids import ModelRef, Revision
from mux.contracts.resources import AgentSpec, SessionSpec
from mux.drivers.openai import OpenAIDriver
from mux.drivers.openai.session_controls import SessionControls
from mux.drivers.openai.transport import SDKTransport
from mux.drivers.openai.turn import MemoryRecoveryJournal
from mux.drivers.openai.usage import MemoryUsageRevisions
from mux.errors import ProviderError
from mux.testing.drivers.openai.conformance_c03 import C03Policy
from mux.testing.isolated_sdk import build_async_openai

from .conftest import REF, SCOPE

AGENT = {
    "id": "a",
    "created_at": 0,
    "name": "offline-c03",
    "model": "gpt-6-luna",
    "tools": [],
    "service_tier": "default",
    "multi_agent": {"enabled": False},
}
SESSION = {
    "id": "s",
    "agent": AGENT,
    "environment": {"type": "none"},
    "status": "idle",
    "required_actions": [],
    "metadata": {},
}


class PolicyTransport(SDKTransport):
    """Same explicit request/readback seam installed by the lane runner."""

    def __init__(self, sdk):
        super().__init__(sdk)
        self.policy = C03Policy()

    async def request(self, method, path, *, body=None, query=None, key=None):
        body = self.policy.request(method, path, body)
        value = await super().request(method, path, body=body, query=query, key=key)
        self.policy.response(method, path, value)
        return value


@pytest.mark.asyncio
async def test_actual_sdk_create_has_no_tools_environment_delegation_or_auto_tier():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=SESSION if "/sessions" in request.url.path else AGENT)

    async with build_async_openai(
        api_key="offline",
        base_url="https://openai.invalid/v1",
        transport=httpx.MockTransport(handle),
    ) as sdk:
        transport = PolicyTransport(sdk)
        driver = OpenAIDriver(
            transport,
            account_scope_id="project",
            authorization=lambda *args: True,
            profile_id="openai.conversation_only",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
            session_controls=SessionControls(model="gpt-6-luna", multi_agent_enabled=False),
        )
        agent = await driver.agents.create(
            SCOPE,
            AgentSpec(
                name="offline-c03", model=ModelRef(provider="openai", id="gpt-6-luna"), tools=()
            ),
            key="create-agent",
        )
        session = await driver.sessions.create(
            SCOPE,
            SessionSpec(agent=agent.ref, agent_revision=Revision(local=0), config_revision=1),
            key="create-session",
            initial=UserMessage(content=(TextPart(text="initial"),)),
        )
        assert session.continuity.workspace == "none"
        await driver.sessions.retrieve(SCOPE, session.ref)
    posts = [request for request in seen if request.method == "POST"]
    agent_body, session_body = [json.loads(request.content) for request in posts]
    for actual in (agent_body, session_body["agent"]):
        assert actual["tools"] == []
        assert actual["service_tier"] == "default"
        assert actual["multi_agent"] == {"enabled": False}
        assert actual["model"] == "gpt-6-luna"
    assert session_body["environment"] == {"type": "none"}
    assert len(session_body["input"]) == 1
    assert "spend_control" not in session_body
    assert all(request.headers["OpenAI-Beta"] == "agents=v1" for request in seen)
    assert transport.policy.session_creates == 1
    assert transport.policy.event_posts == 0


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/agents"),
        ("GET", "/agents/a"),
        ("POST", "/agents/sessions"),
        ("GET", "/agents/sessions/s"),
    ],
)
@pytest.mark.parametrize(
    "field,value",
    [
        ("tools", [{"type": "function"}]),
        ("tools", None),
        ("service_tier", "auto"),
        ("multi_agent", {"enabled": True}),
        ("model", "gpt-6-astra"),
    ],
)
def test_every_create_or_readback_refuses_native_policy_drift(method, path, field, value):
    result = deepcopy(SESSION if "/sessions" in path else AGENT)
    agent = result["agent"] if "/sessions" in path else result
    agent[field] = value
    with pytest.raises(ValueError, match="native tools/model/tier/delegation"):
        C03Policy().response(method, path, result)


@pytest.mark.parametrize("environment", [{"type": "openai_hosted"}, {}, None])
def test_empty_tools_does_not_admit_a_hosted_or_unknown_environment(environment):
    policy = C03Policy()
    with pytest.raises(ValueError, match="environment must be none"):
        policy.request(
            "POST", "/agents/sessions", {"environment": environment, "input": ["initial"]}
        )
    with pytest.raises(ValueError, match="native environment"):
        policy.response("GET", "/agents/sessions/s", {**SESSION, "environment": environment})
    policy.response("DELETE", "/agents/sessions/s", {})


def test_third_event_post_or_tool_result_cannot_dispatch_even_after_ambiguity():
    policy = C03Policy()
    policy.request(
        "POST", "/agents/sessions", {"environment": {"type": "none"}, "input": ["initial"]}
    )
    body = {"events": [{"type": "agent.session.input.message"}]}
    policy.request("POST", "/agents/sessions/s/events", body)
    policy.request("POST", "/agents/sessions/s/events", body)
    with pytest.raises(ValueError, match="allowance exhausted"):
        policy.request("POST", "/agents/sessions/s/events", body)
    with pytest.raises(ValueError, match="non-user input"):
        policy.request(
            "POST",
            "/agents/sessions/s/events",
            {"events": [{"type": "agent.session.input.tool_result"}]},
        )
    policy.request(
        "POST", "/agents/sessions/s/events", {"events": [{"type": "agent.session.input.cancel"}]}
    )
    assert policy.event_posts == 2


@pytest.mark.asyncio
async def test_no_environment_without_initial_input_refuses_before_sdk_io():
    def forbidden(request):
        pytest.fail("unexpected provider I/O")

    async with build_async_openai(
        api_key="offline",
        base_url="https://openai.invalid/v1",
        transport=httpx.MockTransport(forbidden),
    ) as sdk:
        driver = OpenAIDriver(
            SDKTransport(sdk),
            account_scope_id="project",
            authorization=lambda *args: True,
            profile_id="openai.conversation_only",
            journal=MemoryRecoveryJournal(),
            usage_revisions=MemoryUsageRevisions(),
        )
        with pytest.raises(ProviderError) as refused:
            await driver.sessions.create(
                SCOPE,
                SessionSpec(
                    agent=REF.model_copy(update={"kind": "agent", "id": "a"}),
                    agent_revision=Revision(local=0),
                    config_revision=1,
                ),
                key="create",
            )
    assert refused.value.native_code == "initial_input_required"


@pytest.mark.parametrize(
    "path,row",
    [
        ("/agents/sessions/s/items", {"type": "command_execution"}),
        ("/agents/sessions/s/items", {"type": "mcp_call"}),
        ("/agents/sessions/s/items", {"type": "function_call"}),
        ("/agents/sessions/s/turns", {"subagent_id": "child"}),
        ("/agents/sessions/s/turns", {}),
    ],
)
def test_native_loop_evidence_refuses_new_work(path, row):
    with pytest.raises(ValueError, match="tool or delegation loop"):
        C03Policy().response("GET", path, {"data": [row]})


def test_missing_go_refuses_without_accessing_any_file(monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    location = Path(__file__).resolve().parents[5] / "scripts/qa_openai_c03_gate.py"
    spec = importlib.util.spec_from_file_location("c03_gate", location)
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    monkeypatch.setattr(gate, "digest", lambda path: pytest.fail("unexpected file read"))
    with pytest.raises(ValueError, match="separate lead GO pin"):
        gate.validate(SimpleNamespace())


@pytest.fixture
def go_case(tmp_path, monkeypatch):
    import importlib.util
    from datetime import UTC, datetime, timedelta
    from pathlib import Path
    from types import SimpleNamespace

    location = Path(__file__).resolve().parents[5] / "scripts/qa_openai_c03_gate.py"
    spec = importlib.util.spec_from_file_location("c03_gate", location)
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    monkeypatch.setattr(gate, "LANE", tmp_path)
    notes = tmp_path / "LEAD-NOTES.md"
    monkeypatch.setattr(gate, "LEAD_NOTES", notes)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "ledger_path": str(tmp_path / "spend.md"),
                "providers": {
                    "openai": {
                        "cap_usd": "50",
                        "models": {
                            "gpt-6-luna": {
                                "input": "0.20",
                                "cached_input": "0.02",
                                "cache_write_input": "0.25",
                                "output": "0.75",
                            }
                        },
                    }
                },
            }
        )
    )
    monkeypatch.setattr(gate, "CONFIG_SHA256", gate.digest(config))
    pin = tmp_path / "runner.pin.json"
    pin.write_text("{}")
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    identity = "independently-authorized-attempt"
    credential = "/home/clsandoval/.config/daimon-nc/openai.env"
    approval = {
        "attempt_identity": identity,
        "expires_at": expiry,
        "credential_file": credential,
        "provider": "openai",
        "model": "gpt-6-luna",
        "profile": "openai.conversation_only",
        "attempts_per_case": 1,
        "sessions_authorized": 1,
        "sdk_max_retries": 0,
        "case_deadline_seconds": 300,
        "cleanup_deadline_seconds_total": 60,
        "minimum_hold_usd": "1.0755",
        "maximum_hold_usd": "1.0755",
        "maximum_model_requests": 3,
        "initial_create_turns": 1,
        "distinct_C03_user_input_POSTs_authorized": 2,
        "container_allowance": None,
        "native_session_configuration_required": {
            "environment": {"type": "none"},
            "agent": {
                "model": "gpt-6-luna",
                "tools": [],
                "service_tier": "default",
                "multi_agent": {"enabled": False},
            },
        },
        "probe_plan": {
            "fixture_id": "C03",
            "provider": "openai",
            "model": "gpt-6-luna",
            "limits": {"input_tokens": 3150000, "output_tokens": 384000},
            "container_allowance": None,
        },
        "guard_policy": {
            "expected_config_sha256": gate.CONFIG_SHA256,
            "expected_ledger_id": gate.LEDGER_ID,
            "minimum_hold_usd": "1.0755",
            "maximum_hold_usd": "1.0755",
        },
    }
    envelope = tmp_path / "envelope.json"
    envelope.write_text(json.dumps(approval))
    binding = tmp_path / "binding.json"
    binding.write_text(
        json.dumps(
            {
                "tenant_id": "01234567-89ab-cdef-0123-456789abcdef",
                "account_id": "11234567-89ab-cdef-0123-456789abcdef",
                "provider": "openai",
                "profile": "openai.conversation_only",
                "principal_id": "n11-cert-caller",
                "authorization_id": "lead-go",
                "generation": 1,
                "config_revision": 1,
                "credential_file": credential,
                "attempt_identity": identity,
                "invocation": "n11:cert:offline",
            }
        )
    )
    binding.chmod(0o600)
    go = {
        "fixture_id": "C03",
        "model": "gpt-6-luna",
        "profile": "openai.conversation_only",
        "integration_sha": "a" * 40,
        "attempt_identity": identity,
        "output": str((tmp_path / "output").resolve()),
        "ledger_id": gate.LEDGER_ID,
        "envelope_sha256": gate.digest(envelope),
        "envelope_path": str(envelope),
        "config_sha256": gate.digest(config),
        "runner_pin_sha256": gate.digest(pin),
        "expires_at": expiry,
        "binding_path": str(binding),
        "binding_sha256": gate.digest(binding),
    }
    path = tmp_path / "GO.json"
    args = SimpleNamespace(
        go_file=path,
        integration_sha="a" * 40,
        output=tmp_path / "output",
        budget_config=config,
        runner_pin=pin,
    )

    def pin_go():
        path.write_text(json.dumps(go, indent=2))
        _, args.go_sha256 = gate.read_go(path)
        notes.write_text("LEAD PIN canonical sha256 " + args.go_sha256 + "\n")

    pin_go()
    return gate, args, go, approval, pin_go


@pytest.mark.parametrize(
    "change",
    [None, "model", "config_sha256", "integration_sha", "output", "binding_sha256", "expires_at"],
)
def test_go_scopes_source_config_binding_and_expiry_before_runner(go_case, change):
    gate, args, go, _, pin_go = go_case
    if change is not None:
        go[change] = "2000-01-01T00:00:00+00:00" if change == "expires_at" else "different"
        pin_go()
        with pytest.raises(ValueError):
            gate.validate(args)
        assert not hasattr(args, "c03_binding")
    else:
        assert gate.validate(args)["profile"] == "openai.conversation_only"
        assert args.c03_go["attempt_identity"] == go["attempt_identity"]
        assert not args.output.exists()


@pytest.mark.parametrize("prefix", ["", "LEAD SIGN ", "NOT LEAD PIN ", "> LEAD PIN "])
def test_canonical_go_without_a_lead_pin_refuses_before_financial_reads(
    go_case, monkeypatch, prefix
):
    gate, args, _, _, _ = go_case
    gate.LEAD_NOTES.write_text(prefix + args.go_sha256 + "\n")
    monkeypatch.setattr(gate, "envelope_from_go", lambda *args: pytest.fail("financial read"))
    with pytest.raises(ValueError, match="no LEAD PIN"):
        gate.validate(args)


def test_lead_pin_uses_canonical_go_bytes_not_pretty_file_hash(go_case):
    gate, args, go, _, _ = go_case
    assert gate.digest(args.go_file) != args.go_sha256
    args.go_file.write_text(json.dumps(dict(reversed(list(go.items()))), indent=4))
    assert gate.validate(args)["attempt_identity"] == go["attempt_identity"]


def test_pinned_go_cannot_hide_envelope_hash_mismatch(go_case):
    from pathlib import Path

    gate, args, go, _, _ = go_case
    Path(go["envelope_path"]).write_text("{}")
    with pytest.raises(ValueError, match="financial envelope differs"):
        gate.validate(args)


@pytest.mark.parametrize("kind", ["marker", "receipt"])
def test_consumed_identity_refuses_even_with_another_output(go_case, tmp_path, kind):
    gate, args, go, _, _ = go_case
    other = tmp_path / "different-output"
    other.mkdir()
    evidence = other / ("C03.attempt" if kind == "marker" else "C03.receipt.json")
    evidence.write_text(json.dumps({"attempt_identity": go["attempt_identity"]}))
    with pytest.raises(ValueError, match="identity already consumed"):
        gate.validate(args)
    assert not args.output.exists()


def test_old_consumed_identity_joins_legacy_source_marker_to_retained_go(go_case, tmp_path):
    from pathlib import Path

    gate, args, go, approval, pin_go = go_case
    old = "C03-OPENAI-OPTION-A-20261012T053523Z"
    go["attempt_identity"] = old
    approval["attempt_identity"] = old
    envelope = Path(go["envelope_path"])
    envelope.write_text(json.dumps(approval))
    go["envelope_sha256"] = gate.digest(envelope)
    consumed = tmp_path / "old-output"
    consumed.mkdir()
    (consumed / "C03.attempt").write_text("b" * 40 + "\n")
    (tmp_path / "old-GO.json").write_text(
        json.dumps({"attempt_identity": old, "output": str(consumed)})
    )
    pin_go()
    with pytest.raises(ValueError, match="identity already consumed"):
        gate.validate(args)


def test_pinned_go_attempt_must_equal_envelope_and_binding(go_case):
    gate, args, go, _, pin_go = go_case
    go["attempt_identity"] = "a-different-new-identity"
    pin_go()
    with pytest.raises(ValueError, match="envelope attempt differs"):
        gate.validate(args)


def test_attempt_marker_is_shared_by_identity_across_output_directories(go_case):
    import os

    gate, args, go, _, pin_go = go_case
    gate.validate(args)
    marker = gate.attempt_marker(go)
    marker.parent.mkdir()
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps({"attempt_identity": go["attempt_identity"]}))
    go["output"] = str(args.output / "another-output")
    args.output = args.output / "another-output"
    pin_go()
    with pytest.raises(ValueError, match="identity already consumed"):
        gate.validate(args)


def test_changed_lane_recipe_refuses_even_with_a_valid_go(tmp_path):
    import importlib.util
    from pathlib import Path

    location = Path(__file__).resolve().parents[5] / "scripts/qa_openai_c03_gate.py"
    spec = importlib.util.spec_from_file_location("c03_gate", location)
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    manifest = tmp_path / "checkout/packages/mux/tests/drivers/openai/c03_runner_manifest.json"
    manifest.parent.mkdir(parents=True)
    lane = tmp_path / "lane"
    lane.mkdir()
    files = (
        "live_pending.py",
        "live_smoke.py",
        "live_c03.sh",
        "prepare_live_c03.py",
        "offline_live_pending_test.py",
        "offline_live_c03_test.py",
    )
    for name in files:
        (lane / name).write_bytes(b"reviewed recipe")
    manifest.write_text(json.dumps({"files": {name: gate.digest(lane / name) for name in files}}))
    gate.verify_harness(tmp_path / "checkout", lane)
    (lane / "live_pending.py").write_bytes(b"changed after review")
    with pytest.raises(ValueError, match="differs from reviewed commit"):
        gate.verify_harness(tmp_path / "checkout", lane)


def test_signed_envelope_expiry_is_independent_of_go_expiry():
    import importlib.util
    from datetime import UTC, datetime
    from pathlib import Path

    location = Path(__file__).resolve().parents[5] / "scripts/qa_openai_c03_gate.py"
    spec = importlib.util.spec_from_file_location("c03_gate", location)
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    now = datetime(2026, 10, 13, tzinfo=UTC)
    for expiry in ("2026-10-12T23:59:59+00:00", "2026-10-13T00:00:00+00:00", "2026-10-14T00:00:00"):
        with pytest.raises(ValueError, match="financial envelope expired"):
            gate.require_envelope_unexpired({"expires_at": expiry}, now=now)
    gate.require_envelope_unexpired({"expires_at": "2026-10-13T00:00:01+00:00"}, now=now)


def test_legacy_cleanup_uuid_marker_does_not_consume_an_unrelated_identity(go_case, tmp_path):
    gate, args, go, _, _ = go_case
    (tmp_path / "cleanup.attempt").write_text("b" * 32)
    assert gate.validate(args)["attempt_identity"] == go["attempt_identity"]
