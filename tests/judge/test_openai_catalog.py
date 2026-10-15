"""N3 native SDK fixtures executed by the real G1 fenced host, offline only."""

import copy
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from daimon.testing.provider_fixtures import BackendFixture, ScenarioFixture, load_provider_fixtures
from daimon.testing.provider_replay import ProviderTape, WireFrame, WireReply

from . import openai_catalog_host as host
from . import openai_catalog_replay as consumer
from .catalog_runner import ScenarioPlan, load_catalog, plan_scenario
from .headless_executor import execute_openai_cell

REPO = Path(__file__).resolve().parents[2]
INDEX = REPO / "packages/testing/fixtures/target53/index.json"
CATALOG = INDEX.parent / "catalog"


async def test_actual_discord_table_posts_fail_the_catalog_predicate() -> None:
    pack = load_provider_fixtures(INDEX, catalog_root=CATALOG)
    scenario, fixture = pack.select("QA-NEW37-MARKDOWN-TABLE-READABLE", "openai")
    entry = next(e for e in load_catalog(CATALOG) if e.id == scenario.scenario_id)
    plan = plan_scenario(entry, CATALOG, "openai", run_id="test")
    # Planner over the public projection still binds the original source bytes.
    plan = plan.model_copy(
        update={"scenario": plan.scenario.model_copy(update={"sha256": scenario.source.sha256})}
    )
    result = await execute_openai_cell(
        plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
    )
    assert result.status == "FAIL"
    assert result.reported_status == "EXPECTED-FAIL"
    assert result.host_outcome.status == "PASS"
    assert result.evidence.turns[0].texts
    assert result.evidence.turns[0].text_capture_complete
    assert result.catalog_outcome.checks[0].status == "FAIL"
    assert result.discord_surfaces[0].boundary == "discord_adapter_posts"
    assert result.request_count == 6
    assert len(result.evidence.turns) == len(result.headless_outputs) == 1
    assert result.evidence.turns[0].root_turn_id == fixture.turns[0].root_id
    assert result.evidence.turns[0].terminals[0].authority == "record"
    assert result.rendered_text[0].startswith("| Fruit |")
    assert not result.evidence.turns[0].card_capture_complete


def selected(id_: str) -> tuple[ScenarioPlan, ScenarioFixture, BackendFixture]:
    pack = load_provider_fixtures(INDEX, catalog_root=CATALOG)
    scenario, fixture = pack.select(id_, "openai")
    entry = next(e for e in load_catalog(CATALOG) if e.id == id_)
    entry = entry.model_copy(update={"sha256": scenario.source.sha256})
    return plan_scenario(entry, CATALOG, "openai", run_id="test"), scenario, fixture


async def test_every_frozen_openai_cell_retains_checks_and_bound_roots() -> None:
    pack = load_provider_fixtures(INDEX, catalog_root=CATALOG)
    entries = {e.id: e for e in load_catalog(CATALOG)}
    executed = 0
    for scenario in pack.scenarios:
        _, fixture = pack.select(scenario.scenario_id, "openai")
        entry = entries[scenario.scenario_id].model_copy(update={"sha256": scenario.source.sha256})
        plan = plan_scenario(entry, CATALOG, "openai", run_id="all")
        result = await execute_openai_cell(
            plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
        )
        assert len(result.catalog_outcome.checks) == len(plan.assertions) + len(
            plan.global_assertions
        )
        assert not any(
            t.card_capture_complete or t.tool_capture_complete or t.approval_capture_complete
            for t in result.evidence.turns
        )
        if result.evidence.turns:
            executed += 1
            assert result.host_outcome.status == "PASS"
            captured = [
                t for t in fixture.turns if t.turn in {e.turn for e in result.evidence.turns}
            ]
            assert result.request_count == 6 * len(captured)
            assert [t.root_turn_id for t in result.evidence.turns] == [
                t.root_id
                for t in fixture.turns
                if t.turn in {e.turn for e in result.evidence.turns}
            ]
            assert all(t.terminal_capture_complete for t in result.evidence.turns)
            assert len(result.headless_outputs) == len(result.evidence.turns)
            if len(result.evidence.turns) > 1 and len(
                {t.session_id for t in result.evidence.turns}
            ) < len(result.evidence.turns):
                assert len({t.slot_id for t in result.evidence.turns}) < len(result.evidence.turns)
        else:
            assert result.status == "PENDING" and result.request_count == 0
    assert executed > 0


async def test_fenced_followup_reuses_the_session_and_excludes_old_output(tmp_path: Path) -> None:
    # A separate, newly pinned host-only microcase uses N3's native two-turn
    # recipe. No waits/tools are removed from a scored TARGET-53 scenario.
    import yaml
    from daimon.testing.provider_replay import SourcePin

    from .catalog_runner import sha
    from .test_catalog_runner import planned

    pack = load_provider_fixtures(INDEX, catalog_root=CATALOG)
    template = next(
        s for s in pack.scenarios if s.scenario_id == "QA-NEW10-SPLIT-ANSWER-AND-CHUNK-REPLY"
    )
    original = next(p for p in template.providers if p.backend == "openai")
    row = {
        "id": "QA-HOST-REUSE",
        "title": "isolated G1 followup",
        "set": "A",
        "surface": "headless",
        "setup": [],
        "steps": [
            {"do": "new_channel"},
            {"do": "mention", "text": original.turns[0].request_text},
            {"do": "wait_done"},
            {"do": "thread_reply", "mention": True, "text": original.turns[1].request_text},
            {"do": "wait_done"},
        ],
        "assert": [
            {"kind": "text_absent", "turn": 1, "pattern": "NEVER_FORBIDDEN"},
            {"kind": "text_absent", "turn": 2, "pattern": "NEVER_FORBIDDEN"},
        ],
        "teardown": [],
    }
    plan = planned(tmp_path, row, "openai")
    invocations = [i for i in plan.invocations if i.operation == "host_turn"]
    turns = tuple(
        t.model_copy(
            update={
                "turn": i.turn,
                "location": i.location,
                "channel": i.channel,
                "thread": i.thread,
                "completion_gate": None,
            }
        )
        for t, i in zip(original.turns, invocations, strict=True)
    )
    fixture = original.model_copy(update={"turns": turns, "gaps": ()})
    pin = SourcePin(path=f"scenarios/{plan.scenario.filename}", sha256=plan.scenario.sha256)
    # Derive all execution fields from the microcase's actual pinned YAML.
    document = yaml.safe_load((tmp_path / pin.path).read_bytes())
    if sha((tmp_path / pin.path).read_bytes()) != pin.sha256:
        raise AssertionError("microcase pin changed")
    scenario = template.model_copy(
        update={
            "scenario_id": plan.scenario.id,
            "source": pin,
            "projection": pin,
            "scenario": document,
            "values": {},
            "assets": (),
            "providers": (fixture, template.providers[1]),
        }
    )
    result = await execute_openai_cell(
        plan, scenario, fixture, source_root=tmp_path, repository_root=REPO
    )
    assert result.status == result.host_outcome.status == "PASS"
    assert result.request_count == 12
    assert len({t.session_id for t in result.evidence.turns}) == 1
    assert len({t.slot_id for t in result.evidence.turns}) == 1
    assert [t.root_turn_id for t in result.evidence.turns] == [t.root_id for t in turns]
    assert len({o.evidence_id for o in result.headless_outputs}) == 2
    # Last turn's visible return comes from its own native answer, not replayed history.
    answers = [
        cast(dict[str, Any], f["item"])
        for f in turns[1].frames
        if f.get("type") == "agent.session.turn.item.done"
    ]
    answer = next(item for item in answers if item.get("role") == "assistant")
    assert result.rendered_text[1] == answer["content"][0]["text"]


@pytest.mark.parametrize("change", ["source", "model", "provider", "turn", "text"])
async def test_corrupt_pin_or_selection_refuses_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    change: str,
) -> None:
    plan, scenario, fixture = selected("QA-NEW37-MARKDOWN-TABLE-READABLE")

    def no_transport(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("corrupt fixture reached an SDK transport")

    monkeypatch.setattr(consumer, "provider_replay", no_transport)
    if change == "source":
        scenario = scenario.model_copy(
            update={"source": scenario.source.model_copy(update={"sha256": "0" * 64})}
        )
    elif change == "model":
        fixture = fixture.model_copy(update={"model": "gpt-6-sol"})
    elif change == "provider":
        plan = plan.model_copy(update={"backend": "gemini"})
    elif change in ("turn", "text"):
        first = fixture.turns[0].model_copy(
            update={"turn": 99} if change == "turn" else {"request_text": "foreign input"}
        )
        fixture = fixture.model_copy(update={"turns": (first,)})
    scenario = scenario.model_copy(update={"providers": (fixture, scenario.providers[1])})
    with pytest.raises(ValueError):
        await execute_openai_cell(
            plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
        )


@pytest.mark.parametrize(
    "change", ["receipt_id", "stale", "future", "foreign_root", "unknown_send"]
)
async def test_fresh_receipt_and_owned_root_are_required(
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    plan, scenario, fixture = selected("QA-NEW37-MARKDOWN-TABLE-READABLE")
    if change in ("receipt_id", "stale", "future"):
        original = host.capture_headless_return

        async def corrupt(*args: Any, **kwargs: Any) -> Any:
            output = await original(*args, **kwargs)
            update = (
                {"evidence_id": "foreign"}
                if change == "receipt_id"
                else (
                    {"started_s": 0.0}
                    if change == "stale"
                    else {"returned_s": output.returned_s + 3600}
                )
            )
            return output.model_copy(update=update)

        monkeypatch.setattr(host, "capture_headless_return", corrupt)
    elif change == "unknown_send":
        original_get = host.MemoryStateStore.get_operation

        async def unknown(self: Any, *args: Any, **kwargs: Any) -> Any:
            record = await original_get(self, *args, **kwargs)
            if record is None:
                return None
            return record.model_copy(
                update={"result": {**record.result, "status": "outcome_unknown"}}
            )

        monkeypatch.setattr(host.MemoryStateStore, "get_operation", unknown)
    else:
        original_tape = consumer.host_tape

        def foreign(*args: Any, **kwargs: Any) -> ProviderTape:
            tape = original_tape(*args, **kwargs)
            replies: list[WireReply] = []
            for reply in tape.replies:
                frames = reply.frames
                if frames is not None:
                    altered: list[WireFrame] = []
                    for frame in frames:
                        payload = copy.deepcopy(frame.payload)
                        turn = cast(dict[str, Any], payload["turn"])
                        turn["id"] = "foreign-root"
                        if "item" in payload:
                            cast(dict[str, Any], payload["item"])["turn_id"] = "foreign-root"
                        altered.append(frame.model_copy(update={"payload": payload}))
                    frames = tuple(altered)
                replies.append(reply.model_copy(update={"frames": frames}))
            return tape.model_copy(update={"replies": tuple(replies)})

        monkeypatch.setattr(consumer, "host_tape", foreign)
    with pytest.raises(ValueError):
        await execute_openai_cell(
            plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
        )


@pytest.mark.parametrize("case", ["timeout", "completion_gate", "fixture_gap", "attachment"])
async def test_unbound_catalog_requirements_refuse_before_transport(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    plan, scenario, fixture = selected("QA-NEW37-MARKDOWN-TABLE-READABLE")

    def no_transport(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unbound step reached an SDK transport")

    monkeypatch.setattr(consumer, "provider_replay", no_transport)
    if case == "timeout":
        steps = tuple(
            i.model_copy(update={"source": {**i.source, "timeout_s": 0.001}})
            if i.operation == "wait_done"
            else i
            for i in plan.invocations
        )
        plan = plan.model_copy(update={"invocations": steps})
    elif case == "attachment":
        steps = tuple(
            i.model_copy(update={"fixture": "fixtures/f.pdf"}) if i.operation == "host_turn" else i
            for i in plan.invocations
        )
        plan = plan.model_copy(update={"invocations": steps})
    elif case == "completion_gate":
        fixture = fixture.model_copy(
            update={"turns": (fixture.turns[0].model_copy(update={"completion_gate": "hold"}),)}
        )
    else:
        from daimon.testing.provider_fixtures import FixtureGap

        fixture = fixture.model_copy(
            update={"gaps": (FixtureGap(code="HOST_GATE", location="steps[1]", reason="unbound"),)}
        )
    scenario = scenario.model_copy(update={"providers": (fixture, scenario.providers[1])})
    if case == "timeout":
        with pytest.raises(ValueError, match="pinned source operation"):
            await execute_openai_cell(
                plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
            )
        return
    result = await execute_openai_cell(
        plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
    )
    assert result.status == "PENDING" and result.request_count == 0 and not result.evidence.turns


def test_optimized_refusal_guards_survive(tmp_path: Path) -> None:
    plan, scenario, fixture = selected("QA-NEW37-MARKDOWN-TABLE-READABLE")
    data = tmp_path / "openai-input.json"
    data.write_text(
        __import__("json").dumps(
            {
                "plan": plan.model_dump(mode="json"),
                "scenario": scenario.model_dump(mode="json"),
                "fixture": fixture.model_dump(mode="json"),
            }
        )
    )
    script = """
import asyncio, json, sys
sys.path.insert(0, sys.argv[2])
from judge.catalog_runner import ScenarioPlan
from judge.headless_executor import execute_openai_cell
from daimon.testing.provider_fixtures import ScenarioFixture, BackendFixture
x=json.load(open(sys.argv[1]))
async def main():
 p=ScenarioPlan.model_validate(x['plan'])
 s=ScenarioFixture.model_validate(x['scenario'])
 f=BackendFixture.model_validate(x['fixture'])
 bad=f.model_copy(update={'model': 'gpt-6-sol'})
 try:
  await execute_openai_cell(p,s.model_copy(update={'providers': (bad,s.providers[1])}),bad,source_root=__import__('pathlib').Path(sys.argv[3]),repository_root=__import__('pathlib').Path(sys.argv[4]))
 except ValueError: pass
 else: raise RuntimeError('foreign model earned evidence under -O')
 steps=tuple(i.model_copy(update={'source': {**i.source,'timeout_s':1e-9}}) if i.operation=='wait_done' else i for i in p.invocations)
 try:
  await execute_openai_cell(p.model_copy(update={'invocations': steps}),s,f,source_root=__import__('pathlib').Path(sys.argv[3]),repository_root=__import__('pathlib').Path(sys.argv[4]))
 except ValueError: pass
 else: raise RuntimeError('changed source timeout dispatched under -O')
asyncio.run(main())
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            script,
            str(data),
            str(Path(__file__).parents[1]),
            str(CATALOG),
            str(REPO),
        ],
        capture_output=True,
        timeout=60,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr.decode())


@pytest.mark.parametrize("name", consumer.OFFLINE_SDK_ENV)
async def test_ambient_sdk_options_refuse_without_client_construction(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    plan, scenario, fixture = selected("QA-NEW37-MARKDOWN-TABLE-READABLE")
    monkeypatch.setenv(name, "poisoned-offline-option")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ambient option reached SDK construction")

    monkeypatch.setattr(consumer, "provider_replay", forbidden)
    with pytest.raises(ValueError, match="ambient SDK options"):
        await execute_openai_cell(
            plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
        )


async def test_product_source_provenance_is_not_a_native_behavior_gate() -> None:
    plan, scenario, fixture = selected("QA-NEW37-MARKDOWN-TABLE-READABLE")
    changed = fixture.model_copy(
        update={"codec_source": fixture.codec_source.model_copy(update={"sha256": "0" * 64})}
    )
    providers = tuple(changed if p.backend == "openai" else p for p in scenario.providers)
    source = scenario.model_copy(update={"providers": providers})
    result = await execute_openai_cell(
        plan, source, changed, source_root=CATALOG, repository_root=REPO
    )
    baseline = await execute_openai_cell(
        plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
    )
    assert result.status == baseline.status
    assert result.rendered_text == baseline.rendered_text
    assert result.request_count == baseline.request_count and result.request_count > 0
    assert result.host_outcome == baseline.host_outcome
