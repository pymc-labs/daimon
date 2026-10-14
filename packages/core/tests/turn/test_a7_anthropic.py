"""Offline preparation, never an authenticated Anthropic client or certificate."""

from __future__ import annotations

import json
import math
import os
import uuid

import httpx
import pytest
from daimon.core.constants import MA_MAX_RETRIES
from daimon.core.turn.io import default_mux_turn_io
from daimon.testing import a7_anthropic as a7
from daimon.testing.factories import make_tenant
from daimon.testing.ma import send_events_response
from daimon.testing.ma_transport import ScriptedReply, ScriptedTransport
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import async_sessionmaker

TENANT = uuid.UUID(int=4704)
SCOPE = Scope(
    tenant_id=str(TENANT), account_id="a7", principal_id="offline", authorization_id="probe"
)
QUIET = {"cpu_count": 4.0, "load1": 1.0, "cpu_some_avg10_percent": 1.0}
LOADED = {"cpu_count": 4.0, "load1": 9.0, "cpu_some_avg10_percent": 99.0}


@pytest.fixture(autouse=True)
def no_operator_provider_env():
    # Do not save, print or read operator values in order to sanitize them.
    if any(
        name.upper().startswith(("ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_"))
        for name in os.environ
    ):
        pytest.skip("offline SDK positives require no ambient provider configuration")


@pytest.fixture
async def replay(db_engine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    a7.require_owned_sessionmaker(sm)
    async with sm.begin() as db:
        await make_tenant(db, id=TENANT)
    instance = a7.StateReplay(sm, SCOPE)
    yield instance
    await instance.close()


@pytest.mark.parametrize("cold", [True, False])
async def test_diagnostic_measures_real_postgres_claim_journal_and_actual_io_classes(
    replay, cold, monkeypatch, tmp_path
):
    monkeypatch.setenv("DAIMON_TURN__PATH", "legacy")
    evidence = await a7.benchmark(
        replay,
        namespace=uuid.uuid4().hex,
        iterations=2,
        warmups=1,
        cold_client=cold,
        diagnostic=True,
        sample=lambda: LOADED,
    )
    assert evidence["status"] == "diagnostic"
    assert evidence["state_store"] == "postgres"
    assert evidence["external_provider_calls"] == 0
    assert evidence["quiet_host"] is False
    assert evidence["certification_gate_passed"] is False
    assert evidence["client_mode"] == ("cold-diagnostic" if cold else "warm")
    assert len(evidence["pairs"]) == 3
    assert [p["order"] for p in evidence["pairs"]] == [
        ("legacy", "mux"),
        ("mux", "legacy"),
        ("legacy", "mux"),
    ]
    for pair in evidence["pairs"]:
        old, new = pair["turns"]["legacy"], pair["turns"]["mux"]
        assert old["io_class"] == "LegacyTurnIO" and new["io_class"] == "MuxTurnIO"
        assert old["selected_path"] == "legacy" and new["selected_path"] == "mux"
        assert old["sql_statements"] == 0 < new["sql_statements"]
        assert new["journal_records"] == 3 and new["claim_accepted"]
        assert new["lease_released"]
        assert old["journal_records"] == 0
        assert old["claim_accepted"] is None and old["lease_released"] is None
        assert old["digest"] == new["digest"]
        assert old["http_calls"] == new["http_calls"] == 2
        for turn in (old, new):
            assert 0 <= turn["first_event_ms"] <= turn["duration_ms"]
    assert os.environ["DAIMON_TURN__PATH"] == "legacy"
    assert len(replay.trace.requests) == 12
    assert not replay.trace.replies and not replay.trace.violations
    artifact = tmp_path / "a7-diagnostic.json"
    artifact.write_text(json.dumps(evidence))
    assert "offline question" not in artifact.read_text()
    assert "offline answer" not in artifact.read_text()
    assert len(evidence["source"]["head"]) == 40


@pytest.mark.parametrize("cold", [False, True])
async def test_actual_host_diagnostics_never_claim_certification(replay, cold, tmp_path):
    evidence = await a7.benchmark(
        replay,
        namespace=uuid.uuid4().hex,
        iterations=2,
        warmups=2,
        cold_client=cold,
        diagnostic=True,
    )
    assert evidence["status"] == "diagnostic" and not evidence["certification_gate_passed"]
    assert evidence["quiet_host"] is a7.QuietPolicy().accepts(evidence["host_samples"])
    assert len(evidence["host_samples"]) >= 2
    target = tmp_path / ("cold-client.json" if cold else "existing-client.json")
    target.write_text(json.dumps(evidence, indent=2) + "\n")
    print("A7 diagnostic artifact:", target)


@pytest.mark.parametrize("connection", [False, True])
@pytest.mark.parametrize("unsafe_first", [False, True])
async def test_a7_rejects_routed_foreign_engine_before_store_or_client(
    monkeypatch, connection, unsafe_first
):
    from daimon.core._models import Tenant
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

    good = create_async_engine("postgresql+asyncpg://offline:fixture@localhost/daimon_test_nc_N4")
    bad = create_async_engine("postgresql+asyncpg://offline:fixture@remote.invalid/operator")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(None)
        raise AssertionError("foreign A7 factory reached store/client/socket")

    monkeypatch.setattr(a7, "PostgresStateStore", forbidden)
    monkeypatch.setattr(a7, "offline_client", forbidden)
    for engine in (good, bad):
        event.listen(engine.sync_engine, "do_connect", forbidden)
    route = AsyncConnection(bad) if connection else bad
    entries = [(Tenant, good), (Tenant.__table__, route)]
    if unsafe_first:
        entries.reverse()
    try:
        with pytest.raises(ValueError, match="owned local lane"):
            a7.StateReplay(async_sessionmaker(good, binds=dict(entries)), SCOPE)
        assert calls == []
    finally:
        await good.dispose()
        await bad.dispose()


@pytest.mark.parametrize(
    "name",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_WORKSPACE_ID",
        "ANTHROPIC_WEBHOOK_KEY",
        "ANTHROPIC_UNKNOWN_FUTURE_SETTING",
        "OPENAI_API_KEY",
        "OPENAI_CUSTOM_HEADERS",
        "OPENAI_UNKNOWN_FUTURE_SETTING",
        "GOOGLE_API_KEY",
        "GOOGLE_UNKNOWN_FUTURE_SETTING",
        "GEMINI_API_KEY",
        "GEMINI_UNKNOWN_FUTURE_SETTING",
    ],
)
@pytest.mark.parametrize("value", ["", "offline-invented-value"])
async def test_sdk_constructor_copy_and_transport_refuse_ambient_names(name, value, monkeypatch):
    trace = ScriptedTransport()
    async with a7.offline_client(trace) as client:
        assert client.max_retries == MA_MAX_RETRIES == 8
        assert str(client.base_url) == "https://anthropic.invalid"
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match="ambient"):
            a7.offline_client(trace)
        with pytest.raises(ValueError, match="ambient"):
            type(client)()
        for copy in (client.copy, client.with_options):
            with pytest.raises(ValueError, match="ambient"):
                copy(max_retries=0)
        # Guarded transport refuses even if the operator changes env after construction.
        with pytest.raises(ValueError, match="ambient"):
            await client._client.send(httpx.Request("GET", "https://anthropic.invalid/refused"))
        assert not trace.requests


async def test_single_attempt_copy_keeps_real_sdk_wire_without_ambient_defaults():
    trace = ScriptedTransport()
    trace.queue(ScriptedReply("POST", "/v1/sessions/offline/events", send_events_response()))
    async with a7.offline_client(trace) as client:
        copied = client.with_options(max_retries=0)
        assert copied.max_retries == 0 and client.max_retries == 8
        io = default_mux_turn_io(copied, SCOPE, "offline", read_timeout_s=120)
        await io.send(({"type": "user.message", "content": [{"type": "text", "text": "x"}]},))
        assert len(trace.requests) == 1 and not trace.violations and not trace.replies


@pytest.mark.parametrize(
    "field,value",
    [
        ("load1", 2.01),
        ("load1", -1.0),
        ("load1", math.nan),
        ("cpu_count", 0.0),
        ("cpu_some_avg10_percent", 10.01),
        ("cpu_some_avg10_percent", -1.0),
        ("cpu_some_avg10_percent", math.inf),
    ],
)
def test_fixed_quiet_policy_refuses_load_pressure_and_nonfinite_samples(field, value):
    assert a7.QuietPolicy().accepts([QUIET, QUIET])
    assert not a7.QuietPolicy().accepts([QUIET, QUIET | {field: value}])
    assert not a7.QuietPolicy().accepts([QUIET])


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"iterations": 199, "quiet_window_go": True},
        {"warmups": 19, "quiet_window_go": True},
        {"iterations": 1, "diagnostic": True},
        {"warmups": -1, "diagnostic": True},
        {"namespace": " ", "diagnostic": True},
    ],
)
async def test_missing_go_or_insufficient_pairs_refuses_before_preparation(options):
    class NoReplay:
        def __getattr__(self, name):
            raise AssertionError("invalid run must never touch replay")

    with pytest.raises(ValueError):
        await a7.benchmark(NoReplay(), **({"namespace": "offline"} | options))


@pytest.mark.skipif(
    os.environ.get("DAIMON_A7_QUIET_GO") != "yes", reason="explicit quiet-host GO only"
)
async def test_a7_quiet_host_benchmark(replay, tmp_path):
    """Detached opt-in runner; no provider or credential access even after GO."""
    evidence = await a7.benchmark(
        replay,
        namespace=uuid.uuid4().hex,
        quiet_window_go=True,
    )
    target = os.environ.get("DAIMON_A7_OUTPUT")
    from pathlib import Path

    path = Path(target) if target else tmp_path / "a7-quiet-host.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n")
    assert evidence["quiet_host"], "loaded-host result retained; rerun after quiet-window GO"
    assert evidence["certification_gate_passed"], "raw timings retained; A7 latency gate failed"
