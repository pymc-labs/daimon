"""An agent request with no tools of its own keeps Gemini's built-ins off.

Gemini treats an empty tool list as its defaults (code_execution, google_search,
url_context), so such a request allows only the inert `daimon_no_tools` sentinel.
If the model calls it anyway, the host answers once and the turn continues.
"""

from __future__ import annotations

from daimon.core.turn.termination import TerminationReason
from mux.drivers.gemini.transport import NO_TOOLS_FUNCTION
from structlog.testing import capture_logs

from .test_gemini_termination import NativeHost, run
from .test_gemini_termination import clean_sdk_settings as clean_sdk_settings
from .test_gemini_termination import native_host as native_host
from .test_gemini_termination import owned_database_preflight as owned_database_preflight


async def test_a_call_to_the_sentinel_is_answered_once_and_the_turn_completes(
    native_host: NativeHost,
) -> None:
    native_host.wire.status = "requires_action"
    native_host.wire.function_name = NO_TOOLS_FUNCTION
    with capture_logs() as logs:
        outcome = await run(native_host)
    assert outcome.state.termination == TerminationReason.COMPLETED
    (answer,) = native_host.wire.results
    assert [item["type"] for item in answer] == ["function_result"]
    assert answer[0]["name"] == NO_TOOLS_FUNCTION
    assert "answer directly" in str(answer[0]["result"])
    assert [e["event"] for e in logs if "no_tools" in e["event"]] == ["turn.gemini_no_tools_called"]
    # The sentinel never reaches the person: no tool card, just the answer.
    assert "daimon_no_tools" not in str(outcome.state.content)
    assert "direct" in str(outcome.state.content), "the continuation's answer is delivered"


async def test_a_second_call_ends_the_turn_in_plain_words_and_the_next_message_works(
    native_host: NativeHost,
) -> None:
    from daimon.core.turn.notices import render_termination_notice

    native_host.wire.status = "requires_action"
    native_host.wire.function_name = NO_TOOLS_FUNCTION
    native_host.wire.repeat_calls = 1
    with capture_logs() as logs:
        outcome = await run(native_host)
    assert outcome.state.termination is not None and outcome.state.termination.is_failure
    events = [e["event"] for e in logs if "no_tools" in e["event"]]
    assert events == ["turn.gemini_no_tools_called", "turn.gemini_no_tools_loop"]
    # Both pauses were answered, so nothing is left waiting on an action.
    assert len(native_host.wire.results) == 2
    notice = render_termination_notice(outcome.state.termination, state=outcome.state)
    assert notice is not None and "couldn't answer that without tools" in notice.title
    native_host.wire.status = "completed"
    again = await run(native_host, operation_key="after-the-loop")
    assert again.state.termination == TerminationReason.COMPLETED


async def test_a_real_function_call_still_ends_as_requires_action(native_host: NativeHost) -> None:
    native_host.wire.status = "requires_action"
    outcome = await run(native_host)
    assert outcome.state.termination == TerminationReason.REQUIRES_ACTION
    assert native_host.wire.results == []


async def test_a_later_replay_never_shows_or_reanswers_the_sentinel(
    native_host: NativeHost,
) -> None:
    from .test_gemini_terminal_usage import codec

    native_host.wire.status = "requires_action"
    native_host.wire.function_name = NO_TOOLS_FUNCTION
    await run(native_host)
    assert len(native_host.wire.results) == 1
    # A reconnect, or a fresh process, replays the whole history.
    io, _ = await codec(native_host)
    shown = await io.replay()
    assert NO_TOOLS_FUNCTION not in str([event.model_dump() for event in shown])
    assert not any(
        getattr(getattr(event, "stop_reason", None), "type", None) == "requires_action"
        for event in shown
    )
    assert len(native_host.wire.results) == 1, "the answered pause is history, never re-answered"


async def test_a_sentinel_answered_from_history_follows_the_continuation_at_once(
    native_host: NativeHost,
) -> None:
    import asyncio
    import time

    # The pause is only in history (the asking interaction's stream stays silent):
    # the replay answers it and the stream must move to the continuation, not wait
    # out the 120 s read timeout on the old interaction.
    native_host.wire.status = "requires_action"
    native_host.wire.function_name = NO_TOOLS_FUNCTION
    native_host.wire.silent_stream = True
    started = time.monotonic()
    outcome = await asyncio.wait_for(run(native_host), timeout=60)
    assert outcome.state.termination == TerminationReason.COMPLETED
    assert time.monotonic() - started < 20
    assert len(native_host.wire.results) == 1
