"""Every backend's turn IO, through the shared driver, survives the same service behaviour.

Each case is one provider-neutral `Script` that every backend fake plays on its own
wire. A case today's code fails is a strict xfail naming the finding
(FINAL-REVIEW.md F1 to F16) or the PR that fixes it, listed in the backend's
`pending` table in `registry.py`. When that PR lands the case passes, the strict
mark fails CI, and the PR deletes the entry.
"""

from __future__ import annotations

import time
from typing import cast

import pytest
from daimon.core.turn import driver
from daimon.core.turn import gemini as gemini_turn
from daimon.core.turn.notices import render_termination_notice
from daimon.core.turn.termination import TerminationReason
from structlog.testing import capture_logs

from .drive import Result
from .registry import BACKENDS, Backend
from .script import EARLIER, Conn, Script


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "_QUIET_READ_RETRY_DELAYS_S", (0.0, 0.0))
    monkeypatch.setattr(gemini_turn, "STREAM_RETRY_BASE_S", 0.01)
    monkeypatch.setattr(gemini_turn, "ABANDON_TIMEOUT_S", 0.5)


@pytest.fixture(params=sorted(BACKENDS))
def backend(request: pytest.FixtureRequest) -> Backend:
    chosen = BACKENDS[request.param]
    case = request.node.originalname.removeprefix("test_")
    if case in chosen.not_applicable:
        pytest.skip(chosen.not_applicable[case])
    if case in chosen.pending:
        # Only an assertion is the expected failure; any other error still fails.
        request.applymarker(
            pytest.mark.xfail(reason=chosen.pending[case], strict=True, raises=AssertionError)
        )
    return chosen


async def completes(backend: Backend, monkeypatch: pytest.MonkeyPatch, script: Script) -> Result:
    result = await backend.run(monkeypatch, script)
    assert result.state.termination == TerminationReason.COMPLETED, (
        result.state.termination,
        result.state.error,
    )
    assert result.state.error is None
    # Every answer record once, in order, however many connections carried it.
    assert result.text() == script.answer()
    assert len(result.lifecycle.terminal_success) == 1
    assert not result.lifecycle.terminal_failures
    # Recovery never delivers the question twice.
    assert result.sends == 1
    return result


async def test_answers_in_one_stream(backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: one connection carries the whole turn."""
    await completes(backend, monkeypatch, Script())


async def test_create_then_read_immediately(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream closes before anything happens, so the first thing the host sees of
    the turn is a read right after the create. Reads carry the create's second
    without its microseconds (the QTAC78 shape)."""
    script = Script(connections=[Conn(end="eof")], stamps="coarse_read")
    await completes(backend, monkeypatch, script)


async def test_same_second_burst(backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    """Twenty records with one whole-second stamp, over three connections:
    all delivered, once each, in order."""
    script = Script(
        pieces=20,
        stamps="same_second",
        connections=[Conn(7, "read_error"), Conn(7, "hold")],
    )
    await completes(backend, monkeypatch, script)


async def test_clock_skew_reads_behind(backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    """The server serving reads runs 2 s behind the one that stamped the create."""
    script = Script(connections=[Conn(1, "read_error")], stamps="behind")
    await completes(backend, monkeypatch, script)


async def test_clock_skew_reads_ahead(backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    """The server serving reads runs 2 s ahead of the host."""
    script = Script(connections=[Conn(1, "read_error")], stamps="ahead")
    await completes(backend, monkeypatch, script)


async def test_clean_eof_mid_turn(backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    """The service closes the stream cleanly mid-turn (MA does every ~600 s). The
    status check says running, the host reconnects, and the turn completes."""
    script = Script(connections=[Conn(1, "eof")])
    await completes(backend, monkeypatch, script)


async def test_two_clean_eofs_in_a_row(backend: Backend, monkeypatch: pytest.MonkeyPatch) -> None:
    """A long turn sees more than one routine close; neither is a lost connection."""
    script = Script(pieces=4, connections=[Conn(1, "eof"), Conn(1, "eof")])
    await completes(backend, monkeypatch, script)


@pytest.mark.parametrize("drop", ["read_error", "protocol_error"])
async def test_mid_stream_drop_then_resume(
    backend: Backend, monkeypatch: pytest.MonkeyPatch, drop: str
) -> None:
    """The connection drops mid-turn; the reconnect resumes with no gap and no
    duplicate."""
    script = Script(pieces=4, connections=[Conn(2, drop)])  # type: ignore[arg-type]
    await completes(backend, monkeypatch, script)


async def test_resume_across_many_connections(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four connections each carry part of the turn, ending every way but EOF."""
    script = Script(
        pieces=6,
        connections=[Conn(2, "read_error"), Conn(1, "hold"), Conn(2, "protocol_error")],
    )
    await completes(backend, monkeypatch, script)


async def test_two_drops_with_progress_between(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1 (new): the reconnect after a drop delivers records, then drops again. The
    2-attempt drop budget is per stream generation and never resets on progress
    (driver.py:1123), so a long turn on a flaky network is lost."""
    script = Script(pieces=6, connections=[Conn(2, "read_error"), Conn(2, "read_error")])
    await completes(backend, monkeypatch, script)


@pytest.mark.parametrize("drop", ["none", "read_error"])
async def test_restart_resumes_only_this_turn(
    backend: Backend, monkeypatch: pytest.MonkeyPatch, drop: str
) -> None:
    """After a host restart the session already holds an earlier finished turn, and
    the new host IO has no in-memory cursor. This turn's reply, including what a
    reconnect replay recovers, carries only this turn's records."""
    connections = [Conn(1, "read_error")] if drop == "read_error" else [Conn()]
    script = Script(after_restart=True, connections=connections)
    result = await completes(backend, monkeypatch, script)
    assert EARLIER not in result.text()


async def test_status_read_fails_once_after_a_quiet_stream(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR 1 (#1621): one failed status read after a quiet stream no longer loses
    the turn."""
    script = Script(connections=[Conn(1, "hold")], status_failures=1)
    result = await completes(backend, monkeypatch, script)
    assert result.facts["status_failed"] == 1


async def test_replay_fails_once_on_reconnect(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F2(a): the reconnect replay is retried, so one 503 does not end the turn."""
    script = Script(connections=[Conn(1, "read_error")], replay_failures=1)
    result = await completes(backend, monkeypatch, script)
    assert result.facts["replay_failed"] == 1


STUCK = Script(stuck=True, deadline_s=1.0, connections=[Conn(1, "eof")])
"""The service says running forever and nothing moves, with a 1 s ceiling."""


async def stuck(
    backend: Backend, monkeypatch: pytest.MonkeyPatch, *, ceiling: bool = True
) -> Result:
    result = await backend.run(monkeypatch, STUCK)
    if ceiling:
        # The give-up details below only mean something once the turn gives up at
        # its cap; a backend that loses the turn first fails here, not later.
        assert result.state.termination == TerminationReason.CEILING
    return result


async def test_stuck_running_ends_ceiling(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn that never ends gives up at its cap as CEILING (no STALLED reason, root
    decision), never COMPLETED and never an unexpected crash."""
    result = await stuck(backend, monkeypatch, ceiling=False)
    assert result.state.termination == TerminationReason.CEILING, (
        result.state.termination,
        result.state.error,
    )
    assert len(result.lifecycle.terminal_failures) == 1
    assert not result.lifecycle.terminal_success


STALL_AFTER_S = 1.0
"""The stall cap the planned setting is given; the ceiling is four times longer."""


async def test_stuck_running_stalls_out_before_the_ceiling(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stall cap, not the ceiling, ends a turn that never moves: CEILING plus a
    `turn.stalled` line naming the backend (root decision: no STALLED reason). PR 3a
    enforced owns the setting; this case assumes `DAIMON_TURN__STALL_AFTER_S`."""
    monkeypatch.setenv("DAIMON_TURN__STALL_AFTER_S", str(STALL_AFTER_S))
    script = Script(stuck=True, deadline_s=4 * STALL_AFTER_S, connections=[Conn(1, "eof")])
    started = time.monotonic()
    with capture_logs() as logs:
        result = await backend.run(monkeypatch, script)
    assert result.state.termination == TerminationReason.CEILING
    assert time.monotonic() - started < 2.5 * STALL_AFTER_S
    stalled = [line for line in logs if line.get("event") == "turn.stalled"]
    assert stalled and stalled[0].get("backend") == backend.name


async def test_stuck_running_settles_the_native_turn(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: before a give-up is finalized, the host asks the service to stop the
    turn, so it does not keep running and spending (PR 3a enforced)."""
    result = await stuck(backend, monkeypatch, ceiling=False)
    order = cast(list[str], result.facts["order"])
    assert "interrupt" in order and order.index("interrupt") < order.index("finalized"), order


async def test_stuck_running_reconnects_back_off(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2: eventless reconnects back off (1 s, 2 s, 4 s, cap 10 s), so a service that
    keeps closing a running turn is not hammered (PR 3a enforced)."""
    result = await stuck(backend, monkeypatch)
    # Within a 1 s ceiling, a 1 s then 2 s backoff allows at most two reopens.
    assert cast(int, result.facts["opens"]) <= 3


async def test_give_up_notice_asks_what_it_finished(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root decision: a give-up tells the person to ask Daimon what it finished,
    not to try again (PR 3a enforced)."""
    result = await stuck(backend, monkeypatch)
    termination = result.state.termination
    assert termination is not None
    notice = render_termination_notice(termination, state=result.state)
    assert notice is not None and "ask what it finished" in notice.next_step


async def test_status_always_failing_gives_up_as_lost(
    backend: Backend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every read after a quiet stream fails. The turn ends CONNECTION_LOST with a
    failure notice, never COMPLETED (F1) and never an unexpected crash (F4)."""
    script = Script(
        connections=[Conn(0, "hold")], status_failures=99, replay_failures=99, deadline_s=3.0
    )
    result = await backend.run(monkeypatch, script)
    assert cast(int, result.facts["status_failed"]) >= 1
    assert result.state.termination == TerminationReason.CONNECTION_LOST, (
        result.state.termination,
        result.state.error,
    )
    assert len(result.lifecycle.terminal_failures) == 1
    assert not result.lifecycle.terminal_success
