from __future__ import annotations

import json
import subprocess
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from qa.live.config import Alerts, Config, Pricing, Target
from qa.live.cost import Ledger
from qa.live.discord import CHANNEL_MARKER, DiscordBackend
from qa.live.evaluate import evaluate
from qa.live.model_probe import ModelEvidence, ProbeRequest, probe
from qa.live.report import Alerter, Result
from qa.live.runner import Executor
from qa.live.schema import MODEL, Assertion, Scenario
from qa.live.tests.conftest import FakeBackend, FakeJudge
from qa.live.tests.test_discord import FakeDriver
from qa.live.types import Message, Pending, Turn, Usage, WatchTimeout, utcnow


@pytest.mark.parametrize("stuck", [False, True])
def test_watch_timeout_evaluates_evidence_and_alerts(
    stuck: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
) -> None:
    def timeout(turn: Turn, timeout: float) -> None:
        turn.ended_at = utcnow()
        if stuck:
            turn.first_visible_s = 0.5
            turn.messages = [{"id": "card", "content": "Working on it", "working": True}]
        raise WatchTimeout("watch timed out")

    monkeypatch.setattr(backend, "collect", timeout)
    scenario.assertions = [
        Assertion(kind="no_silent_drop", turn=1, maximum=10),
        Assertion(kind="done_within_s", turn=1, maximum=60),
        Assertion(kind="card_finalized", turn=1),
    ]
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "FAIL"
    statuses = {check.kind: check.status for check in result.checks}
    assert statuses["no_silent_drop"] == ("PASS" if stuck else "FAIL")
    assert statuses["done_within_s"] == statuses["card_finalized"] == "FAIL"
    assert backend.sent == 1 and backend.events[-1] == "delete"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kw: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    Alerter(
        Alerts(inbox=str(tmp_path / "inbox"), command=["fake-alert"]), tmp_path / "alerts.json"
    ).notify(result, tmp_path / "run.json")
    assert len(calls) == 1 and "FAIL" in calls[0][-1]


def test_no_trigger_has_zero_charge(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
) -> None:
    def unavailable(roles: set[str]) -> None:
        raise Pending("preflight unavailable")

    monkeypatch.setattr(backend, "preflight", unavailable)
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "PENDING" and backend.sent == 0
    row = json.loads(ledger.path.read_text())
    assert row["usd"] == row["actual_usd"] == 0
    assert row["accounting"] == "no_trigger"
    assert json.loads(ledger.reservations.read_text()) == {}


@pytest.mark.parametrize("models", [[], ["claude-sonnet-4-5"]])
def test_missing_or_wrong_outcome_model_fails(
    models: list[str],
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
) -> None:
    monkeypatch.setattr(
        backend, "usage", lambda turn: Usage(usd=0.02, source="turn_outcomes", models=models)
    )
    result = Executor(backend, judge, ledger, pricing, "staging").run(scenario)
    assert result.status == "FAIL"
    assert any(check.kind == "model" and check.status == "FAIL" for check in result.checks)


def test_model_refusal_is_before_trigger_and_free(
    monkeypatch: pytest.MonkeyPatch,
    backend: FakeBackend,
    judge: FakeJudge,
    ledger: Ledger,
    pricing: Pricing,
    scenario: Scenario,
) -> None:
    def refuse(channel: str) -> None:
        raise ValueError("non-Haiku agent")

    monkeypatch.setattr(backend, "verify_model", refuse)
    assert Executor(backend, judge, ledger, pricing, "prod").run(scenario).status == "FAIL"
    assert backend.sent == 0 and backend.events[-1] == "delete"
    assert json.loads(ledger.path.read_text())["usd"] == 0


@pytest.mark.parametrize(
    "model,pinned,passes",
    [
        ("claude-haiku-5-5", True, True),
        ("claude-haiku-5-5", False, False),
        (MODEL, True, False),
        ("claude-opus-4-6", True, False),
        ("", True, False),
    ],
)
def test_prod_probe_requires_matching_live_channel_and_haiku_pin(
    model: str,
    pinned: bool,
    passes: bool,
    monkeypatch: pytest.MonkeyPatch,
    pricing: Pricing,
) -> None:
    target = Target(
        enabled=True,
        guild_id="745261709622771773",
        guild_allowlist=["745261709622771773"],
        qa_agent_name="haiku-qa",
        model_probe=["fake-probe"],
    )
    backend = DiscordBackend(Config(pricing=pricing, prod=target), "prod", driver=FakeDriver())
    backend.owned.add("parent")
    evidence = ModelEvidence(
        guild_id=target.guild_id,
        channel_id="parent",
        agent_name="haiku-qa",
        agent_id="agent-qa",
        model=model,
        channel_pinned=pinned,
    )
    calls: list[dict[str, object]] = []

    def read(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, evidence.model_dump_json())

    monkeypatch.setattr(subprocess, "run", read)
    if passes:
        backend.verify_model("parent")
    else:
        with pytest.raises(ValueError, match="Haiku"):
            backend.verify_model("parent")
    assert json.loads(str(calls[0]["input"]))["channel_id"] == "parent"


def test_delete_retries_and_sweep_only_stale_marked_qa_channels(
    monkeypatch: pytest.MonkeyPatch,
    pricing: Pricing,
) -> None:
    config = Config(pricing=pricing, staging=Target(enabled=True))
    driver = FakeDriver()
    backend = DiscordBackend(config, "staging", driver=driver)

    def snowflake(hours: int) -> str:
        return str(
            (int((utcnow() - timedelta(hours=hours)).timestamp() * 1000) - 1420070400000) << 22
        )

    old, fresh = snowflake(2), snowflake(0)
    channels: list[Message] = [
        {
            "id": old,
            "name": "qa-orphan",
            "parent_id": config.staging.category_id,
            "topic": CHANNEL_MARKER,
        },
        {
            "id": fresh,
            "name": "qa-current",
            "parent_id": config.staging.category_id,
            "topic": CHANNEL_MARKER,
        },
        {"id": "123", "name": "qa-other-worker", "parent_id": config.staging.category_id},
    ]
    deletes: list[str] = []

    def call(method: str, path: str, **kwargs: object) -> object:
        if method == "GET":
            return channels
        deletes.append(path)
        if len(deletes) < 3:
            raise SystemExit("transient HTTP error")
        return {}

    sleeps: list[float] = []
    monkeypatch.setattr(driver, "call", call)
    monkeypatch.setattr("qa.live.discord.time.sleep", sleeps.append)
    backend.sweep_orphans()
    assert deletes == [f"/channels/{old}"] * 3
    assert sleeps == [2, 4] and not backend.owned


def test_three_pending_alerts_dedupe_and_recover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kw: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    alerts = Alerter(
        Alerts(inbox=str(tmp_path / "inbox"), command=["fake-alert"]), tmp_path / "state.json"
    )
    pending = Result("run", "QA-TEST", "staging")
    alerts.notify(pending, tmp_path / "evidence.json")
    alerts.notify(pending, tmp_path / "evidence.json")
    assert not calls
    alerts.notify(pending, tmp_path / "evidence.json")
    alerts.notify(pending, tmp_path / "evidence.json")
    assert len(calls) == 1 and "PENDING" in calls[0][-1]
    pending.status = "PASS"
    alerts.notify(pending, tmp_path / "evidence.json")
    assert len(calls) == 2 and "RECOVERY" in calls[-1][-1]
    assert json.loads(alerts.state_path.read_text())["staging:QA-TEST"]["pending_count"] == "0"


def test_log_delay_and_wrapped_numeric_scope(
    monkeypatch: pytest.MonkeyPatch, pricing: Pricing
) -> None:
    backend = DiscordBackend(
        Config(pricing=pricing, staging=Target(enabled=True)), "staging", driver=FakeDriver()
    )
    waits: list[float] = []
    monkeypatch.setattr("qa.live.discord.time.sleep", waits.append)
    turn = Turn(1, "1", "parent", utcnow(), ended_at=utcnow(), thread_id="1558189294282219552")
    queries: list[list[str]] = []

    def read(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        queries.append(command)
        payload = {
            "thread_id": int(turn.thread_id or 0),
            "event": "turn.completed",
            "rid": "qa-rid",
        }
        rows = [{"jsonPayload": {"message": json.dumps(payload)}}] if len(queries) == 1 else []
        return subprocess.CompletedProcess(command, 0, json.dumps(rows))

    monkeypatch.setattr(subprocess, "run", read)
    assert (
        backend.logs(
            Assertion(kind="log_absent", turn=1, event="session_preparation.replaced"), turn
        )
        == []
    )
    assert 30 <= waits[0] <= 45
    assert "jsonPayload.thread_id=1558189294282219552" in queries[0][3]
    assert 'jsonPayload.message:"thread_id"' in queries[0][3]
    assert "qa-rid" in queries[1][3]


def test_enabled_and_scenario_b_budget_cadence(ledger: Ledger, pricing: Pricing) -> None:
    config = Config(pricing=pricing)
    with pytest.raises(ValueError, match="disabled"):
        config.target("staging")
    with pytest.raises(ValueError, match="exceed"):
        config.validate_plan()
    safe = Config(
        pricing=Pricing(per_turn_usd=0.05, judge_input_per_million=1, judge_output_per_million=5)
    )
    safe.validate_plan(2)
    with pytest.raises(ValueError, match="allocation"):
        safe.validate_plan(2.01)
    ledger.claim_schedule("canary", "staging")
    ledger.claim_schedule("canary", "prod")
    ledger.claim_schedule("run", "staging")
    for mode, env in [("canary", "staging"), ("canary", "prod"), ("run", "prod")]:
        with pytest.raises(ValueError, match="cadence"):
            ledger.claim_schedule(mode, env)


def test_usage_database_is_required_before_loading_credentials(
    pricing: Pricing, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(pricing=pricing, staging=Target(enabled=True))
    monkeypatch.delenv(config.staging.database_env, raising=False)
    backend = DiscordBackend(config, "staging")
    with pytest.raises(Pending, match="database is required"):
        backend.preflight({"user"})
    assert backend._driver is None


def test_invalid_log_record_shape_cannot_prove_absence(
    monkeypatch: pytest.MonkeyPatch, pricing: Pricing
) -> None:
    backend = DiscordBackend(
        Config(pricing=pricing, staging=Target(enabled=True)), "staging", driver=FakeDriver()
    )
    monkeypatch.setattr(
        subprocess, "run", lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "{}")
    )
    with pytest.raises(Pending, match="malformed"):
        backend._read_logs("scoped query")


@pytest.mark.parametrize(
    "message,expected",
    [
        ({"embeds": [{"fields": [{"name": "field", "value": "only"}]}]}, "FAIL"),
        ({"embeds": [{"title": "title"}]}, "PASS"),
        ({"embeds": [{"description": "description"}]}, "PASS"),
        ({"embeds": [{"footer": {"text": "footer"}}]}, "PASS"),
        ({"content": " "}, "FAIL"),
    ],
)
def test_nonblank_uses_the_approved_text_surfaces(
    message: Message, expected: str, backend: FakeBackend, judge: FakeJudge
) -> None:
    turn = Turn(1, "1", "parent", utcnow(), messages=[message])
    assert (
        evaluate(Assertion(kind="no_blank_message", turn=1), [turn], backend, judge).status
        == expected
    )


@pytest.mark.asyncio
async def test_model_probe_reads_actual_cascade_and_agent_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.adapters.cli import runtime as runtime_module
    from daimon.core import config as config_module
    from daimon.core.defaults import ma_index
    from daimon.core.scope import ResolvedConfig
    from daimon.core.stores import scoped_config_read, tenants

    statements: list[str] = []

    class Session:
        def begin(self):
            return opened(self)

        async def execute(self, statement):
            statements.append(str(statement))

    @asynccontextmanager
    async def opened(value):
        yield value

    fake_runtime = SimpleNamespace(
        sessionmaker=lambda: opened(Session()), deployment_default=None, anthropic=object()
    )
    monkeypatch.setattr(runtime_module, "build_runtime", lambda settings: opened(fake_runtime))
    monkeypatch.setattr(config_module, "load_settings", lambda: object())

    async def tenant(*args):
        return object()

    async def resolved(*args, **kwargs):
        assert kwargs["context"].channel_id == "qa-channel"
        return ResolvedConfig(agent_name="haiku-qa", agent_name_tier="channel")

    async def agent(*args, **kwargs):
        assert kwargs["name"] == "haiku-qa"
        return SimpleNamespace(id="agent-qa", model=SimpleNamespace(id=MODEL))

    monkeypatch.setattr(tenants, "get_tenant", tenant)
    monkeypatch.setattr(scoped_config_read, "resolve", resolved)
    monkeypatch.setattr(ma_index, "find_agent_by_daimon_tag", agent)
    evidence = await probe(
        ProbeRequest(
            guild_id="745261709622771773", channel_id="qa-channel", category_id="qa-category"
        )
    )
    assert evidence.model == MODEL and evidence.channel_pinned
    assert statements == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = '15000ms'"]
