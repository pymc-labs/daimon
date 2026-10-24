"""Real SDK + mock HTTP: no org connection, key read or live authorization."""

from __future__ import annotations

import copy
import json
import os
from argparse import Namespace

import httpx
import pytest
from daimon.testing.anthropic_live import MODEL, LiveRefused
from daimon.testing.f2_native_bounds import (
    install_f2_bounds,
    native_budget,
    native_tools,
    verify_tools,
)
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.isolated_sdk import SDK_ENV_VARS, build_async_anthropic


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in tuple(os.environ):
        if name in SDK_ENV_VARS or name.upper().startswith(
            ("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_")
        ):
            monkeypatch.delenv(name)


@pytest.mark.parametrize(
    "fault",
    [
        "",
        "missing_ack",
        "missing_get",
        "altered_get",
        "default_enabled",
        "web_enabled",
        "extra_mcp",
        "delegation",
    ],
)
async def test_sdk_budget_and_effective_tool_readback_before_any_message(fault):
    calls = []
    tools = native_tools()
    agent = ma_agent(id="ag_probe", model=MODEL, tools=tools).model_dump(mode="json")

    async def wire(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/v1/sessions":
            body = json.loads(request.content)
            assert body["budget"] == {
                "type": "limit",
                "max_list_cost": {"amount": "25", "currency": "USD"},
            }
            assert "extra_body" not in body
            payload = ma_session(
                id="sesn_probe",
                agent=ma_agent(
                    **{
                        "id": "ag_probe",
                        "model": MODEL,
                        "tools": tools,
                    }
                ),
            ).model_dump(mode="json")
            if fault != "missing_ack":
                payload["budget"] = native_budget()
            return httpx.Response(200, json=payload)
        assert request.method == "GET" and request.url.path == "/v1/sessions/sesn_probe"
        payload = ma_session(id="sesn_probe", model=MODEL).model_dump(mode="json")
        payload["agent"] = copy.deepcopy(agent)
        payload["budget"] = native_budget()
        if fault == "missing_get":
            del payload["budget"]
        elif fault == "altered_get":
            payload["budget"]["max_list_cost"]["amount"] = "26"
        elif fault == "default_enabled":
            payload["agent"]["tools"][0]["default_config"]["enabled"] = True
        elif fault == "web_enabled":
            payload["agent"]["tools"][0]["configs"][-1]["enabled"] = True
        elif fault == "extra_mcp":
            payload["agent"]["tools"][1]["configs"].append(
                {"name": "create_routine", "enabled": True}
            )
        elif fault == "delegation":
            payload["agent"]["multiagent"] = {"type": "multiagent_20261001"}
        return httpx.Response(200, json=payload)

    client = build_async_anthropic(transport=httpx.MockTransport(wire))
    install_f2_bounds(client)
    try:
        if fault:
            with pytest.raises(LiveRefused):
                await client.beta.sessions.create(agent="ag_probe", environment_id="env_probe")
        else:
            reply = await client.beta.sessions.create(agent="ag_probe", environment_id="env_probe")
            assert reply.id == "sesn_probe"
        assert calls[0] == ("POST", "/v1/sessions")
        assert len(calls) == (1 if fault == "missing_ack" else 2)
        assert not any("events" in path for _, path in calls)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "fault",
    ["", "implicit_defaults", "unknown_builtin", "duplicate_config", "wrong_mcp", "ask_policy"],
)
def test_full_toolset_validation_refuses_default_and_configuration_widening(fault):
    raw = {"tools": native_tools(), "multiagent": None}
    if fault == "implicit_defaults":
        del raw["tools"][0]["default_config"]
    elif fault == "unknown_builtin":
        raw["tools"][0]["configs"].append({"name": "future_paid_tool", "enabled": True})
    elif fault == "duplicate_config":
        raw["tools"][0]["configs"][-1] = raw["tools"][0]["configs"][0]
    elif fault == "wrong_mcp":
        raw["tools"][1]["mcp_server_name"] = "foreign"
    elif fault == "ask_policy":
        raw["tools"][0]["configs"][0]["permission_policy"] = {"type": "always_ask"}
    if fault:
        with pytest.raises(LiveRefused):
            verify_tools(raw)
    else:
        verify_tools(raw)


async def test_native_agent_wire_has_disabled_defaults_and_exact_mcp_allowlist():
    calls = []

    async def wire(request):
        calls.append((request.method, request.url.path))
        body = json.loads(request.content)
        assert body["tools"] == native_tools()
        assert body["model"] == MODEL
        return httpx.Response(
            200,
            json=ma_agent(
                id="ag_f2",
                model=MODEL,
                tools=body["tools"],
            ).model_dump(mode="json"),
        )

    client = build_async_anthropic(transport=httpx.MockTransport(wire))
    install_f2_bounds(client)
    try:
        await client.beta.agents.create(name="f2", model=MODEL, tools=[])
        assert calls == [("POST", "/v1/agents")]
        with pytest.raises(LiveRefused):
            await client.beta.agents.create(name="foreign", model="other-model")
        assert len(calls) == 1
    finally:
        await client.close()


@pytest.mark.parametrize("fault", ["missing_signature", "modified_go", "dirty_source"])
def test_acceptance_requires_independently_pinned_go_before_keys_or_requests(
    tmp_path, monkeypatch, fault
):
    from daimon.testing import anthropic_live as live
    from daimon.testing import f2_budget_acceptance as probe

    authorization, go_path = tmp_path / "approval.json", tmp_path / "go.json"
    authorization.write_text('{"purpose":"F2-native-budget-acceptance"}')
    go_path.write_text("{}")
    monkeypatch.setattr(
        probe, "source_pin", lambda: {"head": "a" * 40, "clean": fault != "dirty_source"}
    )
    monkeypatch.setattr(
        live,
        "explicit_secret",
        lambda *_args, **_kwargs: pytest.fail("credential read before signed admission"),
    )
    args = Namespace(
        spend_ledger=tmp_path / "private-ledger",
        go_receipt=go_path,
        authorization=authorization,
        go_sha256=live.digest_json({}) if fault != "modified_go" else "b" * 64,
        authorization_sha256=live.digest_json(json.loads(authorization.read_text()))
        if fault != "missing_signature"
        else "c" * 64,
        live_binding=tmp_path / "binding.json",
    )
    args.live_binding.write_text("{}")
    with pytest.raises(LiveRefused):
        probe.admit_probe(args)


async def test_acceptance_wire_never_permits_message_or_thread_work():
    from daimon.testing.f2_budget_acceptance import AcceptanceTransport

    transport = AcceptanceTransport(trust_env=False, retries=0)
    try:
        for method, path in (
            ("POST", "/v1/sessions/s/events"),
            ("GET", "/v1/sessions/s/threads/t/stream"),
            ("POST", "/v1/agents"),
        ):
            with pytest.raises(LiveRefused):
                await transport.handle_async_request(
                    httpx.Request(method, "https://api.anthropic.com" + path)
                )
    finally:
        await transport.aclose()


@pytest.mark.parametrize(
    "fault", [None, "financial", "workload_envelope", "tools", "no_go", "expiry"]
)
def test_acceptance_exact_signed_envelope_and_separate_go(tmp_path, monkeypatch, fault):
    import hashlib
    from datetime import UTC, datetime
    from pathlib import Path

    from daimon.testing import anthropic_live as live
    from daimon.testing import f2_budget_acceptance as probe
    from mux.contracts.ids import ResourceRef, Scope

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                datetime(2026, 10, 12, tzinfo=UTC)
                if fault == "expiry"
                else datetime(2026, 10, 11, 19, tzinfo=UTC)
            )

    monkeypatch.setattr(probe, "datetime", Clock)
    fixtures = Path(__file__).parent / "fixtures/anthropic-live"
    name = "F2-R2" if fault == "workload_envelope" else "F2-native-budget-acceptance"
    approval = json.loads((fixtures / (name + ".json")).read_text())
    # Signed public fixture names the authorized host, not the CI runner.
    monkeypatch.setattr(
        Path, "home", classmethod(lambda cls: Path(approval["credential_file"]).parents[2])
    )
    if fault == "financial":
        approval["maximum_user_messages"] = 1
    authorization = tmp_path / "approval.json"
    authorization.write_text(json.dumps(approval))
    source = "c4fe67252654c7d164bf823d04f0522971e83092"
    monkeypatch.setattr(probe, "source_pin", lambda: {"head": source, "clean": True})
    monkeypatch.setattr(
        live, "explicit_secret", lambda *_a, **_k: pytest.fail("admission must not read secrets")
    )
    config = tmp_path / "config.json"
    config.write_bytes((fixtures / "host-config.json").read_bytes())
    scope = Scope(
        tenant_id="offline-tenant",
        account_id="offline-account",
        principal_id="probe",
        authorization_id="offline-owned",
    )
    binding = tmp_path / "binding.json"
    binding.write_text(
        live.LiveBinding(
            scope=scope,
            account_scope_id="offline-owned",
            environment=ResourceRef(
                provider="anthropic",
                kind="environment",
                id="env-offline",
                account_scope_id="offline-owned",
                tenant_id=scope.tenant_id,
                account_id=scope.account_id,
            ),
            agent=ResourceRef(
                provider="anthropic",
                kind="agent",
                id="ag-offline",
                account_scope_id="offline-owned",
                tenant_id=scope.tenant_id,
                account_id=scope.account_id,
            ),
            database_url="postgresql+asyncpg://fixture:fixture@localhost/daimon_test_nc_offline",
            mcp_url="https://mcp.owned.example/mcp",
            mcp_bearer_file=tmp_path / "never-read-secret",
        ).model_dump_json()
    )
    recipe, install = tmp_path / "recipe.json", tmp_path / "install.json"
    recipe.write_text('{"model":"claude-haiku-5-5"}')
    install.write_text('{"installed":true}')
    go = {
        "live_go": fault != "no_go",
        "purpose": "F2-native-budget-acceptance",
        "provider": "anthropic",
        "model": live.MODEL,
        "native_budget": native_budget(),
        "native_tools": [] if fault == "tools" else native_tools(),
        "maximum_user_messages": 0,
        "maximum_session_creates": 1,
        "wall_clock_deadline_seconds_total": 60,
        "config_sha256": live.HOST_CONFIG_SHA256,
        "ledger_id": live.LEDGER_ID,
        "merged_source_head": source,
        "binding_sha256": hashlib.sha256(binding.read_bytes()).hexdigest(),
        "approval_digest": live.digest_json(approval),
        "recipe_path": str(recipe),
        "recipe_sha256": hashlib.sha256(recipe.read_bytes()).hexdigest(),
        "install_receipt_path": str(install),
        "install_receipt_sha256": hashlib.sha256(install.read_bytes()).hexdigest(),
    }
    go_path = tmp_path / "go.json"
    go_path.write_text(json.dumps(go))
    args = Namespace(
        spend_ledger=Path(approval["ledger_path"]),
        go_receipt=go_path,
        authorization=authorization,
        go_sha256=live.digest_json(go),
        authorization_sha256=live.digest_json(approval),
        live_binding=binding,
        budget_config=config,
        output=tmp_path / "never-created",
    )
    if fault:
        with pytest.raises(LiveRefused):
            probe.admit_probe(args)
    else:
        admitted = probe.admit_probe(args)
        assert admitted.approval_digest == (
            "8628b05e1b6d637b152c138a0977dc806e40833e9fc6e1d54794881a9ae063a3"
        )
        assert str(admitted.hold) == "0.02" and admitted.deadline_s == 60
        assert admitted.plan.container_allowance.sessions == 1
    assert not args.output.exists() and not (tmp_path / "never-read-secret").exists()
