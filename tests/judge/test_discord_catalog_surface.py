"""The catalog receives actual Discord posts, independently of host returns."""

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.discord_surface import DiscordSurfaceCapture
from daimon.testing.outcome_oracle import RunEvidence, TurnEvidence, evaluate

from .discord_catalog_surface import CatalogLifecycle, bind_discord_text
from .headless_executor import execute_cell
from .test_catalog_runner import planned, source
from .test_headless_executor import scripted


def evidence(started_s: float) -> TurnEvidence:
    return TurnEvidence(
        turn=1, slot_id="slot", session_id="session", root_turn_id="root", started_s=started_s
    )


async def delivered(text: str, *, terminal: bool = True) -> DiscordSurfaceCapture:
    lc = CatalogLifecycle(
        surface="discord",
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
    if lc.capture is None:
        raise AssertionError("adapter was not captured")
    return lc.capture


async def test_long_answer_binds_all_actual_chunks_and_excludes_retired_draft(
    tmp_path: Path,
) -> None:
    row = source()
    row["surface"] = "discord"
    row["assert"] = [
        {"kind": "text_present", "turn": 1, "pattern": "DISCORD_TAIL"},
        {"kind": "text_absent", "turn": 1, "pattern": "Working on it"},
    ]
    plan = planned(tmp_path, row)
    tape = scripted(plan).model_dump(mode="json")
    answer = "x" * 4500 + "DISCORD_TAIL"
    tape["turns"][0]["events"][2]["content"][0]["text"] = answer
    result = await execute_cell(plan, scripted(plan).model_validate(tape))
    turn = result.evidence.turns[0]
    capture = result.discord_surfaces[0]
    assert result.headless_outputs[0].text == answer
    assert result.rendered_text[0] == answer
    assert "DISCORD_TAIL" in "\n".join(t.text for t in turn.texts)
    assert len(capture.messages) >= 3
    assert all(len(m.content) <= 2000 for m in capture.messages)
    assert [t.message_id for t in turn.texts] == [m.message_id for m in capture.messages]
    assert turn.text_capture_complete
    assert [c.status for c in result.catalog_outcome.checks[:2]] == ["PASS", "PASS"]
    assert "Working on it" in " ".join(capture.events[0].message.embed_texts)
    assert "Working on it" not in " ".join(t.text for t in turn.texts)
    assert not turn.card_capture_complete and not turn.tool_capture_complete


async def test_partial_adapter_window_keeps_absence_pending() -> None:
    capture = await delivered("answer", terminal=False)
    turn = bind_discord_text(
        evidence(capture.started_s), capture, expected_id="surface", observed_s=capture.observed_s
    )
    assert not turn.text_capture_complete
    report = evaluate(
        RunEvidence(scenario_id="partial", backend="anthropic", turns=(turn,)),
        [{"kind": "text_absent", "turn": 1, "pattern": "forbidden"}],
    )
    assert report.status == "PENDING"


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "foreign"),
        ("root_turn_id", "foreign"),
        ("evidence_id", "foreign"),
        ("started_s", 0.0),
    ],
)
async def test_capture_must_match_independently_acknowledged_window(field: str, value: Any) -> None:
    capture = await delivered("answer")
    corrupt = capture.model_copy(update={field: value})
    with pytest.raises(ValueError, match="acknowledged root window"):
        bind_discord_text(
            evidence(capture.started_s),
            corrupt,
            expected_id="surface",
            observed_s=capture.observed_s,
        )


async def test_duplicate_or_foreign_messages_and_future_observations_refuse() -> None:
    capture = await delivered("answer")
    message = capture.messages[0]
    corruptions = [
        capture.model_copy(update={"messages": (message, message)}),
        capture.model_copy(
            update={"messages": (message.model_copy(update={"channel_id": "foreign"}),)}
        ),
        capture.model_copy(
            update={
                "messages": (message.model_copy(update={"observed_s": capture.observed_s + 1}),)
            }
        ),
    ]
    for corrupt in corruptions:
        with pytest.raises(ValueError, match="post identity or order"):
            bind_discord_text(
                evidence(capture.started_s),
                corrupt,
                expected_id="surface",
                observed_s=capture.observed_s,
            )


async def test_attachment_pixels_keep_text_coverage_unknown() -> None:
    from daimon.testing.discord_surface import DiscordSurfaceHarness

    harness = DiscordSurfaceHarness(
        evidence_id="surface", session_id="session", root_turn_id="root", render_tables=True
    )

    async def invoke(lc: Any) -> None:
        await lc.post_initial()
        await lc.on_terminal_success(
            TurnState(content=[TextBlock(kind="text", text="| A | B |\n|---|---|\n| 1 | 2 |")])
        )

    capture = await harness.run(invoke)
    turn = bind_discord_text(
        evidence(capture.started_s), capture, expected_id="surface", observed_s=capture.observed_s
    )
    assert any(m.attachment_names for m in capture.messages)
    assert not turn.text_capture_complete


async def test_final_message_cannot_be_replaced_by_headless_text() -> None:
    capture = await delivered("actual")
    message = capture.messages[0].model_copy(update={"content": "headless forged"})
    corrupt = capture.model_copy(update={"messages": (message,)})
    with pytest.raises(ValueError, match="actual post history"):
        bind_discord_text(
            evidence(capture.started_s),
            corrupt,
            expected_id="surface",
            observed_s=capture.observed_s,
        )


async def test_billing_exempt_capture_cannot_close_accounting_text_domain() -> None:
    capture = await delivered("FOOTER-OK")
    turn = bind_discord_text(
        evidence(capture.started_s),
        capture,
        expected_id="surface",
        observed_s=capture.observed_s,
        accounting_required=True,
    )
    report = evaluate(
        RunEvidence(scenario_id="footer", backend="openai", turns=(turn,)),
        [
            {"kind": "text_present", "turn": 1, "pattern": "FOOTER-OK"},
            {"kind": "text_present", "turn": 1, "pattern": "\\$0\\.01"},
        ],
    )
    assert "FOOTER-OK" in turn.texts[0].text
    assert all(check.status == "PENDING" for check in report.checks)
    assert not turn.text_capture_complete


def test_binding_refusals_survive_optimized_python() -> None:
    script = """
import asyncio, sys
sys.path.insert(0,sys.argv[1])
from judge.test_discord_catalog_surface import delivered,evidence
from judge.discord_catalog_surface import bind_discord_text
async def check():
 c=await delivered('ACTUAL')
 t=bind_discord_text(evidence(c.started_s),c,expected_id='surface',observed_s=c.observed_s)
 if not t.text_capture_complete or 'ACTUAL' not in t.texts[0].text: raise RuntimeError('missing actual post')
 for field in ('session_id','root_turn_id','evidence_id'):
  try: bind_discord_text(evidence(c.started_s),c.model_copy(update={field:'foreign'}),expected_id='surface',observed_s=c.observed_s)
  except ValueError: pass
  else: raise RuntimeError('foreign capture accepted')
 p=await delivered('answer',terminal=False)
 t=bind_discord_text(evidence(p.started_s),p,expected_id='surface',observed_s=p.observed_s)
 if t.text_capture_complete: raise RuntimeError('partial capture closed')
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
