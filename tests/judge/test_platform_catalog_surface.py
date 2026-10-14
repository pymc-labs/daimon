"""Actual Slack/Teams posts bind independently of native/headless returns."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import pytest
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.outcome_oracle import RunEvidence, TurnEvidence, evaluate
from daimon.testing.platform_surface import PlatformSurfaceCapture
from daimon.testing.provider_fixtures import load_provider_fixtures

from .catalog_runner import load_catalog, plan_scenario
from .headless_executor import execute_cell, execute_gemini_cell, execute_openai_cell
from .platform_catalog_surface import CatalogLifecycle, bind_platform_text
from .test_catalog_runner import planned, source
from .test_headless_executor import scripted

REPO = Path(__file__).resolve().parents[2]
INDEX = REPO / "packages/testing/fixtures/target53/index.json"
CATALOG = INDEX.parent / "catalog"


def evidence(started_s: float) -> TurnEvidence:
    return TurnEvidence(
        turn=1, slot_id="slot", session_id="session", root_turn_id="root", started_s=started_s
    )


async def delivered(
    platform: str, text: str = "ACTUAL", *, terminal: bool = True
) -> PlatformSurfaceCapture:
    lc = CatalogLifecycle(
        surface=platform,
        evidence_id="surface",
        session_id="session",
        root_turn_id="root",
        model_id="claude-haiku-5-5",
    )

    async def invoke() -> None:
        state = TurnState(content=[TextBlock(kind="text", text=text)])
        if terminal:
            await lc.on_terminal_success(state)
        else:
            await lc.on_render(state)

    await lc.run(invoke)
    if lc.platform_capture is None:
        raise AssertionError("actual platform adapter missing")
    return lc.platform_capture


def bound(capture: PlatformSurfaceCapture, *, accounting_required: bool = False) -> TurnEvidence:
    return bind_platform_text(
        evidence(capture.started_s),
        capture,
        expected_id="surface",
        expected_platform=capture.platform,
        observed_s=capture.observed_s,
        accounting_required=accounting_required,
    )


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_actual_surviving_text_and_retired_draft_have_separate_domains(platform: str) -> None:
    capture = await delivered(platform)
    turn = bound(capture)
    assert turn.text_capture_complete
    assert not turn.terminal_capture_complete
    assert not turn.card_capture_complete and not turn.tool_capture_complete
    assert [t.message_id for t in turn.texts] == [
        m.message_id
        for m in capture.messages
        if any((*m.body_texts, *m.card_texts, *m.control_labels))
    ]
    assert [t.order for t in turn.texts] == sorted(t.order for t in turn.texts)
    assert all(t.observed_s == capture.observed_s for t in turn.texts)
    assert "Working on it" in str(capture.events[0].message.card_texts)
    assert "Working on it" not in "\n".join(t.text for t in turn.texts)
    report = evaluate(
        RunEvidence(scenario_id="isolated", backend="anthropic", turns=(turn,)),
        [
            {"kind": "text_present", "turn": 1, "pattern": "ACTUAL"},
            {"kind": "text_absent", "turn": 1, "pattern": "Working on it"},
        ],
    )
    assert [c.status for c in report.checks] == ["PASS", "PASS"]


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_long_actual_answer_preserves_full_return_and_actual_platform_capture(
    tmp_path: Path,
    platform: str,
) -> None:
    row = source()
    row["surface"] = platform
    row["assert"] = [{"kind": "text_present", "turn": 1, "pattern": "ACTUAL_TAIL"}]
    plan = planned(tmp_path, row)
    tape = scripted(plan).model_dump(mode="json")
    tape["turns"][0]["events"][2]["content"][0]["text"] = "x" * 4500 + "ACTUAL_TAIL"
    result = await execute_cell(plan, scripted(plan).model_validate(tape))
    turn, capture = result.evidence.turns[0], result.platform_surfaces[0]
    assert result.headless_outputs[0].text == "x" * 4500 + "ACTUAL_TAIL"
    assert "ACTUAL_TAIL" in "\n".join(t.text for t in turn.texts)
    assert result.catalog_outcome.checks[0].status == "PASS"
    assert len(result.catalog_outcome.checks) == len(plan.assertions) + len(plan.global_assertions)
    assert turn.root_turn_id == capture.root_turn_id and turn.session_id == capture.session_id
    assert turn.terminals[0].authority == "record"
    assert turn.terminal_capture_complete and turn.session_capture_complete
    assert result.request_count == 6
    if platform == "slack":
        body = next(m for m in capture.messages if "ACTUAL_TAIL" in "".join(m.body_texts))
        assert "ACTUAL_TAIL" not in body.fallback_text
    else:
        answer = next(m for m in capture.messages if "ACTUAL_TAIL" in "".join(m.body_texts))
        assert not answer.card_texts  # Real replacement removes the old Adaptive Card.
        assert any(m.control_labels for m in capture.messages)


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_partial_or_accounting_window_cannot_certify_absence(platform: str) -> None:
    for capture, accounting in [
        (await delivered(platform, terminal=False), False),
        (await delivered(platform), True),
    ]:
        turn = bound(capture, accounting_required=accounting)
        assert not turn.text_capture_complete
        report = evaluate(
            RunEvidence(scenario_id="partial", backend="openai", turns=(turn,)),
            [{"kind": "text_absent", "turn": 1, "pattern": "NEVER_FORBIDDEN"}],
        )
        assert report.status == "PENDING"


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_notification_fallback_never_becomes_visible_text(platform: str) -> None:
    capture = await delivered(platform)
    # A serialized fake-gateway receipt with notification-only text still exposes
    # exactly the body/card/control leaves. It never substitutes the fallback.
    events = tuple(
        e.model_copy(
            update={"message": e.message.model_copy(update={"fallback_text": "NOTIFICATION_ONLY"})}
        )
        for e in capture.events
    )
    messages = tuple(
        m.model_copy(update={"fallback_text": "NOTIFICATION_ONLY"}) for m in capture.messages
    )
    turn = bound(capture.model_copy(update={"events": events, "messages": messages}))
    assert all("NOTIFICATION_ONLY" not in t.text for t in turn.texts)


@pytest.mark.parametrize("platform", ["slack", "teams"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "foreign"),
        ("root_turn_id", "foreign"),
        ("evidence_id", "foreign"),
        ("started_s", 0.0),
    ],
)
async def test_acknowledged_native_identity_and_window_are_required(
    platform: str,
    field: str,
    value: Any,
) -> None:
    capture = await delivered(platform)
    with pytest.raises(ValueError):
        bind_platform_text(
            evidence(capture.started_s),
            capture.model_copy(update={field: value}),
            expected_id="surface",
            expected_platform=platform,
            observed_s=capture.observed_s,
        )


@pytest.mark.parametrize("platform", ["slack", "teams"])
@pytest.mark.parametrize(
    "corruption",
    [
        "platform",
        "future",
        "survivor",
        "duplicate",
        "event_id",
        "creation_order",
        "foreign_destination",
        "unowned_edit",
        "clock_order",
        "opaque_complete",
        "failed_complete",
    ],
)
async def test_corrupted_post_receipts_refuse(platform: str, corruption: str) -> None:
    capture = await delivered(platform)
    events = list(capture.events)
    first = events[0]
    if corruption == "platform":
        with pytest.raises(ValueError):
            bind_platform_text(
                evidence(capture.started_s),
                capture,
                expected_id="surface",
                expected_platform="discord",
                observed_s=capture.observed_s,
            )
        return
    if corruption == "future":
        with pytest.raises(ValueError):
            bind_platform_text(
                evidence(capture.started_s),
                capture,
                expected_id="surface",
                expected_platform=platform,
                observed_s=capture.started_s,
            )
        return
    if corruption == "survivor":
        capture = capture.model_copy(update={"messages": ()})
    elif corruption == "duplicate":
        capture = capture.model_copy(update={"messages": capture.messages * 2})
    elif corruption == "opaque_complete":
        events[0] = first.model_copy(
            update={"message": first.message.model_copy(update={"opaque_text": True})}
        )
    elif corruption == "failed_complete":
        capture = capture.model_copy(update={"delivery_failures": 1})
    elif corruption == "event_id":
        events[0] = first.model_copy(update={"evidence_id": "foreign"})
    elif corruption == "creation_order":
        events[0] = first.model_copy(
            update={"message": first.message.model_copy(update={"order": 2})}
        )
    elif corruption == "foreign_destination":
        events[0] = first.model_copy(
            update={"message": first.message.model_copy(update={"destination_id": "foreign"})}
        )
    elif corruption == "unowned_edit":
        events[0] = first.model_copy(update={"operation": "edit"})
    elif corruption == "clock_order":
        events[0] = first.model_copy(
            update={"message": first.message.model_copy(update={"observed_s": capture.observed_s})}
        )
    capture = capture.model_copy(update={"events": tuple(events)})
    with pytest.raises(ValueError):
        bound(capture)


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_failed_or_double_terminal_delivery_keeps_capture_partial(platform: str) -> None:
    for mode in ("exception", "double"):
        lc = CatalogLifecycle(
            surface=platform,
            evidence_id="surface",
            session_id="session",
            root_turn_id="root",
            model_id="claude-haiku-5-5",
        )

        async def invoke(lc: CatalogLifecycle = lc, mode: str = mode) -> None:
            await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="ACTUAL")]))
            if mode == "exception":
                raise RuntimeError("invoke failed after delivery")
            await lc.on_terminal_success(TurnState())

        if mode == "exception":
            with pytest.raises(RuntimeError, match="invoke failed"):
                await lc.run(invoke)
        else:
            await lc.run(invoke)
        if lc.platform_capture is None:
            raise AssertionError("missing platform receipt")
        assert lc.platform_capture.events
        assert not bound(lc.platform_capture).text_capture_complete


def test_absent_capture_confers_no_platform_credit() -> None:
    turn = evidence(1.0)
    assert (
        bind_platform_text(
            turn, None, expected_id="surface", expected_platform="slack", observed_s=2.0
        )
        == turn
    )
    assert (
        evaluate(
            RunEvidence(scenario_id="none", backend="anthropic", turns=(turn,)),
            [{"kind": "text_absent", "turn": 1, "pattern": "forbidden"}],
        ).status
        == "PENDING"
    )


def test_platform_binding_refusals_survive_optimized_python() -> None:
    script = """
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from judge.test_platform_catalog_surface import delivered, bound
from judge.platform_catalog_surface import evaluate_platform_catalog
from daimon.testing.outcome_oracle import RunEvidence
async def check():
 for p in ('slack', 'teams'):
  c=await delivered(p)
  t=bound(c)
  if not t.text_capture_complete or 'ACTUAL' not in '\\n'.join(x.text for x in t.texts):
   raise RuntimeError('actual posts lost under -O')
  r=evaluate_platform_catalog(RunEvidence(scenario_id='transient',backend='anthropic',turns=(t,)),
    [dict(kind='text_present',turn=1,pattern='Working on it'),dict(kind='text_absent',turn=1,pattern='Working on it')],(c,))
  if [x.status for x in r.checks] != ['FAIL','FAIL']: raise RuntimeError('retired text was ignored under -O')
  for field in ('session_id', 'root_turn_id', 'evidence_id'):
   try: bound(c.model_copy(update={field:'foreign'}))
   except ValueError: pass
   else: raise RuntimeError('foreign capture accepted under -O')
  if bound(await delivered(p,terminal=False)).text_capture_complete:
   raise RuntimeError('partial delivery certified under -O')
asyncio.run(check())
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, str(Path(__file__).parents[1])],
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise AssertionError(result.stderr.decode())


@pytest.mark.parametrize("scenario_id", ["QA-D1-SLACK-FOLLOWUP", "QA-D3-TEAMS-ANSWER-NOT-REPLACED"])
@pytest.mark.parametrize("backend", ("anthropic",))
async def test_frozen_manual_platform_cells_remain_pending_without_dispatch(
    scenario_id: str, backend: Literal["anthropic", "openai", "gemini"]
) -> None:
    pack = load_provider_fixtures(INDEX, catalog_root=CATALOG)
    scenario = next(s for s in pack.scenarios if s.scenario_id == scenario_id)
    entry = next(e for e in load_catalog(CATALOG) if e.id == scenario_id)
    entry = entry.model_copy(update={"sha256": scenario.source.sha256})
    plan = plan_scenario(entry, CATALOG, backend, run_id="manual-control")
    assert not plan.assertions and not plan.invocations
    if backend == "anthropic":
        result = await execute_cell(plan, None)
    else:
        fixture = next(p for p in scenario.providers if p.backend == backend)
        execute = execute_openai_cell if backend == "openai" else execute_gemini_cell
        result = await execute(plan, scenario, fixture, source_root=CATALOG, repository_root=REPO)
    assert result.reported_status == "PENDING" and result.status == "PENDING"
    assert result.request_count == 0 and not result.evidence.turns
    assert not result.platform_surfaces and not result.headless_outputs


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_retired_text_can_fail_forbidden_checks_but_cannot_satisfy_positive_checks(
    platform: str,
) -> None:
    from .platform_catalog_surface import evaluate_platform_catalog

    capture = await delivered(platform)
    turn = bound(capture)
    assert "Working on it" not in " ".join(t.text for t in turn.texts)
    report = evaluate_platform_catalog(
        RunEvidence(scenario_id="transient", backend="anthropic", turns=(turn,)),
        [
            {"kind": "text_present", "turn": 1, "pattern": "ACTUAL"},
            {"kind": "text_present", "turn": 1, "pattern": "Working on it"},
            {"kind": "text_absent", "turn": 1, "pattern": "Working on it"},
            {"kind": "text_absent", "turn": 1, "pattern": "NEVER_FORBIDDEN"},
        ],
        (capture,),
    )
    assert [c.status for c in report.checks] == ["PASS", "FAIL", "FAIL", "PASS"]
    assert capture.events[0].evidence_id in report.checks[2].evidence_ids


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_partial_or_billing_history_cannot_prove_forbidden_absence(platform: str) -> None:
    from .platform_catalog_surface import evaluate_platform_catalog

    for capture, accounting in [
        (await delivered(platform, terminal=False), False),
        (await delivered(platform), True),
    ]:
        turn = bound(capture, accounting_required=accounting)
        report = evaluate_platform_catalog(
            RunEvidence(scenario_id="partial", backend="anthropic", turns=(turn,)),
            [{"kind": "text_absent", "turn": 1, "pattern": "NEVER_FORBIDDEN"}],
            (capture,),
        )
        assert report.status == "PENDING"


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_history_evaluation_refuses_foreign_or_duplicate_receipts(platform: str) -> None:
    from .platform_catalog_surface import evaluate_platform_catalog

    capture = await delivered(platform)
    evidence_ = RunEvidence(scenario_id="identity", backend="anthropic", turns=(bound(capture),))
    for captures in ((capture, capture), (capture.model_copy(update={"root_turn_id": "foreign"}),)):
        with pytest.raises(ValueError):
            evaluate_platform_catalog(evidence_, [{"kind": "no_preamble", "turn": 1}], captures)


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_executor_forbidden_checks_bind_transient_adapter_posts(
    tmp_path: Path, platform: str
) -> None:
    row = source()
    row["surface"] = platform
    row["assert"] = [
        {"kind": "text_present", "turn": 1, "pattern": "KEPT"},
        {"kind": "text_absent", "turn": 1, "pattern": "Working on it"},
    ]
    plan = planned(tmp_path, row)
    result = await execute_cell(plan, scripted(plan))
    assert result.status == "FAIL"
    assert [c.status for c in result.catalog_outcome.checks] == ["PASS", "FAIL"]
    assert "Working on it" not in " ".join(t.text for t in result.evidence.turns[0].texts)
    assert (
        result.platform_surfaces[0].events[0].evidence_id
        in result.catalog_outcome.checks[1].evidence_ids
    )
