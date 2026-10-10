"""Actual SDK/driver/journal captures, corrupt tapes, and oracle refusal paths."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue

from . import headless_executor as executor
from .catalog_runner import ScenarioPlan, build_matrix, sha
from .test_catalog_runner import HEAD, planned, source, write_source

FIXTURES = Path(__file__).with_name("fixtures") / "catalog_anthropic.json"


def scripted(plan: ScenarioPlan) -> executor.ScenarioReplay:
    turns: list[executor.ReplayTurn] = []
    for invocation in plan.invocations:
        if invocation.operation != "host_turn":
            continue
        if invocation.turn is None or invocation.text is None:
            raise AssertionError("fixture has no host turn")
        text = invocation.text.replace("{nonce}", "KEPT")
        root = f"input-{invocation.turn}"
        events: list[dict[str, JsonValue]] = [
            {
                "id": root,
                "type": "user.message",
                "processed_at": "2026-10-10T00:00:00Z",
                "content": [{"type": "text", "text": text}],
            },
            {
                "id": f"running-{invocation.turn}",
                "type": "session.status_running",
                "processed_at": "2026-10-10T00:00:00Z",
            },
            {
                "id": f"answer-{invocation.turn}",
                "type": "agent.message",
                "processed_at": "2026-10-10T00:00:00Z",
                "content": [{"type": "text", "text": "KEPT"}],
            },
            {
                "id": f"ended-{invocation.turn}",
                "type": "session.status_idle",
                "processed_at": "2026-10-10T00:00:00Z",
                "stop_reason": {"type": "end_turn"},
            },
        ]
        turns.append(
            executor.ReplayTurn(turn=invocation.turn, request_text=text, events=tuple(events))
        )
    return executor.ScenarioReplay(
        scenario_id=plan.scenario.id,
        scenario_sha256=plan.scenario.sha256,
        values={"nonce": "KEPT"},
        turns=tuple(turns),
    )


async def test_real_turn_captures_authoritative_roots_and_reuses_persisted_session(
    tmp_path: Path,
) -> None:
    row = source()
    row["surface"] = "headless"  # Isolated host-only fixture has no Discord global predicates.
    row["assert"] = [
        {"kind": "done_within_s", "turn": 1, "max": 10.0},
        {"kind": "done_within_s", "turn": 2, "max": 10.0},
    ]
    plan = planned(tmp_path, row)
    result = await executor.execute_cell(plan, scripted(plan))
    assert result.status == "PASS"
    assert result.request_count == 6  # Frozen session GET, open stream, send input, twice.
    assert [t.root_turn_id for t in result.evidence.turns] == ["input-1", "input-2"]
    assert len({t.session_id for t in result.evidence.turns}) == 1
    assert len({t.slot_id for t in result.evidence.turns}) == 1
    assert [t.terminals[0].authority for t in result.evidence.turns] == ["record", "record"]
    assert all(
        t.terminal_capture_complete and t.session_capture_complete for t in result.evidence.turns
    )
    assert len(result.host_outcome.checks) == 5
    assert result.host_outcome.status == "PASS"
    assert result.rendered_text == ("KEPT", "KEPT")


async def test_original_and_global_assertions_remain_pending_not_dropped_for_credit(
    tmp_path: Path,
) -> None:
    plan = planned(tmp_path, source())
    result = await executor.execute_cell(plan, scripted(plan))
    assert result.status == "PENDING" and result.host_outcome.status == "PASS"
    assert len(result.catalog_outcome.checks) == len(plan.assertions) + len(plan.global_assertions)
    assert all(c.status == "PENDING" for c in result.catalog_outcome.checks)


@pytest.mark.parametrize("backend", ["openai", "gemini"])
async def test_unwired_backends_stay_pending_with_zero_dispatch(
    tmp_path: Path, backend: str
) -> None:
    plan = planned(tmp_path, source(), cast(Any, backend))
    result = await executor.execute_cell(plan, None)
    assert result.status == "PENDING" and result.request_count == 0
    assert not result.evidence.turns
    assert [g.code for g in result.gaps] == ["HOST_BACKEND_PENDING_G1_G2"]


@pytest.mark.parametrize(
    "step",
    [
        {"do": "admin", "tool": "cli", "args": "delete all"},
        {"do": "wait", "s": 1920},
        {"do": "channel_message", "text": "seed"},
        {"do": "burst", "texts": ["first", "second"], "interval_s": 1},
    ],
)
async def test_missing_setup_context_wait_and_concurrency_never_fake_execution(
    tmp_path: Path,
    step: dict[str, Any],
) -> None:
    row = source()
    row["steps"].insert(1, step)
    plan = planned(tmp_path, row)
    result = await executor.execute_cell(plan, scripted(plan))
    assert result.status == "PENDING" and result.request_count == 0
    assert any(
        g.code in {"STEP_BINDING_UNAVAILABLE", "ROUTING_BINDING_UNAVAILABLE"} for g in result.gaps
    )


async def test_attachment_cannot_be_replaced_with_embedded_text(tmp_path: Path) -> None:
    row = source()
    row["steps"][1]["file"] = "fixtures/sales.csv"
    fixture = tmp_path / "fixtures/sales.csv"
    fixture.parent.mkdir()
    fixture.write_text("region,revenue\nwest,100\n")
    plan = planned(tmp_path, row)
    result = await executor.execute_cell(plan, scripted(plan))
    assert result.status == "PENDING" and result.request_count == 0
    assert any(g.code == "ATTACHMENT_BINDING_UNAVAILABLE" for g in result.gaps)


@pytest.mark.parametrize("mutation", ["source_hash", "request", "missing_turn", "extra_turn"])
async def test_replay_identity_or_input_drift_refuses_before_dispatch(
    tmp_path: Path, mutation: str
) -> None:
    plan = planned(tmp_path, source())
    body = scripted(plan).model_dump(mode="json")
    if mutation == "source_hash":
        body["scenario_sha256"] = "0" * 64
    elif mutation == "request":
        body["turns"][0]["request_text"] = "foreign"
        body["turns"][0]["events"][0]["content"][0]["text"] = "foreign"
    elif mutation == "missing_turn":
        body["turns"].pop()
    else:
        body["turns"][1]["turn"] = 3
    replay = executor.ScenarioReplay.model_validate(body)
    with pytest.raises(ValueError):
        await executor.execute_cell(plan, replay)


@pytest.mark.parametrize(
    "mutation", ["foreign_root", "unread_tail", "duplicate_id", "unacknowledged_start"]
)
async def test_corrupt_native_records_cannot_make_completed_evidence(
    tmp_path: Path, mutation: str
) -> None:
    plan = planned(tmp_path, source())
    body = scripted(plan).model_dump(mode="json")
    events = body["turns"][0]["events"]
    if mutation == "foreign_root":
        foreign = dict(events[0], id="foreign-input")
        events.insert(2, foreign)
    elif mutation == "unread_tail":
        events.append(
            dict(events[-1], id="unread-conflict", stop_reason={"type": "retries_exhausted"})
        )
    elif mutation == "duplicate_id":
        events[1]["id"] = events[0]["id"]
    else:
        events[0]["type"] = "session.status_running"
    with pytest.raises(ValueError):
        replay = executor.ScenarioReplay.model_validate(body)
        await executor.execute_cell(plan, replay)


async def test_native_failed_turn_is_a_real_oracle_failure(tmp_path: Path) -> None:
    plan = planned(tmp_path, source())
    body = scripted(plan).model_dump(mode="json")
    body["turns"][0]["events"][-1]["stop_reason"] = {"type": "retries_exhausted"}
    replay = executor.ScenarioReplay.model_validate(body)
    result = await executor.execute_cell(plan, replay)
    assert result.status == "FAIL"
    assert result.host_outcome.checks[0].code == "TURN_NOT_COMPLETED"


def test_admission_and_sse_refusals_survive_optimized_python(tmp_path: Path) -> None:
    plan = planned(tmp_path, source())
    replay = scripted(plan).model_dump(mode="json")
    replay["turns"][0]["events"].append(dict(replay["turns"][0]["events"][-1], id="unread"))
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"plan": plan.model_dump(mode="json"), "replay": replay}))
    script = """
import asyncio, importlib, json, sys
sys.path.insert(0, sys.argv[2])
e = importlib.import_module("judge.headless_executor")
c = importlib.import_module("judge.catalog_runner")
x = json.load(open(sys.argv[1]))
async def check():
    p = c.ScenarioPlan.model_validate(x["plan"])
    try:
        await e.execute_cell(p, e.ScenarioReplay.model_validate(x["replay"]))
    except ValueError:
        pass
    else:
        raise RuntimeError("unread SSE was certified under -O")
    for backend in ("openai", "gemini"):
        # No channels or turns: provider gate must still refuse before dispatch.
        p = p.model_copy(update={"backend": backend, "bindings": (), "invocations": ()})
        r = await e.execute_cell(p, None)
        if r.status != "PENDING" or r.request_count:
            raise RuntimeError("unwired backend dispatched or earned credit")
asyncio.run(check())
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, str(inputs), str(Path(__file__).parents[1])],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode())


def test_committed_tapes_are_explicitly_offline_and_cover_real_catalog_source_hashes() -> None:
    raw = json.loads(FIXTURES.read_text())
    replays = [executor.ScenarioReplay.model_validate(row) for row in raw]
    assert len(replays) == 5 and sum(len(r.turns) for r in replays) == 6
    assert {r.agent_model for r in replays} == {"claude-haiku-5-5"}
    assert all(len(r.scenario_sha256) == 64 for r in replays)
    tool_only = next(r for r in replays if r.scenario_id.startswith("QA-I3-"))
    assert not any(event["type"] == "agent.message" for event in tool_only.turns[0].events)


async def test_full_frozen_matrix_runs_host_turns_and_keeps_other_backends_pending(
    tmp_path: Path,
) -> None:
    target_bytes = FIXTURES.with_name("TARGET-53.txt").read_bytes()
    (tmp_path / "TARGET-53.txt").write_bytes(target_bytes)
    (tmp_path / "SCHEMA.md").write_text("isolated fixture schema")
    (tmp_path / "PROPOSED-KINDS.md").write_text("isolated fixture kinds")
    target_ids = target_bytes.decode().splitlines()
    chosen = "QA-I3-TOOL-ONLY-NO-EMPTY-RESPONSE"
    for id_ in [*target_ids, "QA-EXTRA"]:
        row = source(id_)
        if id_ == chosen:
            row["surface"] = "headless"
            row["assert"] = [
                {"kind": "done_within_s", "turn": 1, "max": 10.0},
                {"kind": "done_within_s", "turn": 2, "max": 10.0},
            ]
        else:
            row["steps"] = []
        write_source(tmp_path, row)
    matrix = build_matrix(tmp_path, integration_sha=HEAD, run_id="fixture")
    plan = next(p for p in matrix.plans if p.scenario.id == chosen and p.backend == "anthropic")
    result = await executor.execute_catalog(
        tmp_path, integration_sha=HEAD, run_id="fixture", replays=(scripted(plan),)
    )
    assert result.target_sha256 == sha(target_bytes)
    assert result.scored_counts == {"PASS": 1, "FAIL": 0, "PENDING": 158}
    assert len(result.cells) == 159 and {c.scenario_id for c in result.cells} == set(target_ids)
    assert sum(c.request_count for c in result.cells) == 6
    assert all(
        c.status == "PENDING" and c.request_count == 0
        for c in result.cells
        if c.backend != "anthropic"
    )
    assert all(c.scenario_id != "QA-EXTRA" for c in result.cells)
    # A frozen hash cannot rescue swapped scoring or an omitted backend column.
    with pytest.raises(ValueError):
        executor.ExecutionReport(
            integration_sha=HEAD,
            target_sha256=result.target_sha256,
            target_ids=result.target_ids,
            cells=result.cells[:-1],
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("backend", "openai"),
        ("profile", "openai.persistent_workspace"),
        ("agent_model", "foreign-model"),
    ],
)
def test_replay_cannot_change_its_provider_profile_or_model(
    tmp_path: Path, field: str, value: str
) -> None:
    plan = planned(tmp_path, source())
    body = scripted(plan).model_dump(mode="json")
    body[field] = value
    with pytest.raises(ValueError):
        executor.ScenarioReplay.model_validate(body)


async def test_explicit_channels_select_distinct_persisted_slots_without_fallback(
    tmp_path: Path,
) -> None:
    row = source()
    row["steps"] = [
        {"do": "new_channel", "ref": "A"},
        {"do": "new_channel", "ref": "B"},
        {"do": "mention", "channel": "B", "text": "Write {nonce}."},
        {"do": "wait_done"},
        {"do": "mention", "channel": "A", "text": "Read it."},
        {"do": "wait_done"},
    ]
    plan = planned(tmp_path, row)
    result = await executor.execute_cell(plan, scripted(plan))
    assert len(result.evidence.turns) == 2
    assert len({t.session_id for t in result.evidence.turns}) == 2
    assert len({t.slot_id for t in result.evidence.turns}) == 2
    assert result.host_outcome.status == "PASS" and result.request_count == 6
    assert not any(c.kind == "same_session" for c in result.host_outcome.checks)
