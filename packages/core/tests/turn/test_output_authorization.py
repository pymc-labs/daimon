"""Progress-render cancellation must not suppress the final authorization check."""

import asyncio

import pytest
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.output import OutputAuthorization


@pytest.mark.parametrize("revoked", [False, True])
async def test_cancelled_progress_rechecks_access_for_final_delivery(revoked):
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def authorize():
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        if revoked:
            raise AdmissionDenied(reason="invoker_not_allowed")

    gate = OutputAuthorization(authorize)
    progress = asyncio.create_task(gate.check())
    await started.wait()
    progress.cancel()
    with pytest.raises(asyncio.CancelledError):
        await progress
    assert not gate.failed
    finish.set()
    if revoked:
        with pytest.raises(AdmissionDenied):
            await gate.check()
        assert gate.failed
        with pytest.raises(AdmissionDenied):
            await gate.check()
        assert calls == 2  # A proved denial remains latched.
    else:
        await gate.check()
        assert calls == 2
        assert not gate.failed


async def test_watch_progress_ticks_skip_access_and_final_rechecks():
    from unittest.mock import AsyncMock

    from daimon.core.turn.run import _DeferredFailureLifecycle
    from daimon.core.turn.state import TurnState

    inner = AsyncMock()
    authorize = AsyncMock()
    lifecycle = _DeferredFailureLifecycle(inner=inner, authorize_final=authorize)
    state = TurnState()
    for _ in range(10):
        await lifecycle.on_render(state)
    authorize.assert_not_awaited()
    inner.on_render.assert_not_awaited()
    await lifecycle.on_terminal_success(state)
    authorize.assert_awaited_once()
    inner.on_terminal_success.assert_awaited_once_with(state)


@pytest.mark.parametrize("ending", ["success", "failure"])
@pytest.mark.parametrize("denied", [False, True])
async def test_refused_watch_final_is_logged_and_cleaned_up(ending, denied, monkeypatch):
    from unittest.mock import AsyncMock

    from daimon.core.turn.run import _DeferredFailureLifecycle
    from daimon.core.turn.state import TurnState
    from structlog.testing import capture_logs

    inner = AsyncMock()
    error = AdmissionDenied(reason="invoker_not_allowed") if denied else OSError("connection reset")
    authorize = AsyncMock(side_effect=error)
    sleep = AsyncMock()
    monkeypatch.setattr("daimon.core.turn.run.asyncio.sleep", sleep)
    lifecycle = _DeferredFailureLifecycle(inner=inner, authorize_final=authorize)
    with capture_logs() as logs:
        if ending == "success":
            await lifecycle.on_terminal_success(TurnState())
        else:
            await lifecycle.on_terminal_failure(TurnState(), RuntimeError("model failed"))
            await lifecycle.flush_held_failure()
    inner.on_terminal_success.assert_not_awaited()
    inner.on_terminal_failure.assert_not_awaited()
    inner.on_output_refused.assert_awaited_once()
    assert authorize.await_count == (1 if denied else 3)
    assert [call.args[0] for call in sleep.await_args_list] == ([] if denied else [1, 2])
    assert logs == [
        {
            "event": "turn.final_output_refused",
            "error_type": type(error).__name__,
            "log_level": "warning",
        }
    ]


@pytest.mark.parametrize("ending", ["success", "failure"])
async def test_transient_final_check_retries_and_delivers(ending, monkeypatch):
    from unittest.mock import AsyncMock

    from daimon.core.turn.run import _DeferredFailureLifecycle
    from daimon.core.turn.state import TurnState

    inner = AsyncMock()
    authorize = AsyncMock(side_effect=[OSError("connection reset"), None])
    sleep = AsyncMock()
    monkeypatch.setattr("daimon.core.turn.run.asyncio.sleep", sleep)
    lifecycle = _DeferredFailureLifecycle(inner=inner, authorize_final=authorize)
    state = TurnState()
    if ending == "success":
        await lifecycle.on_terminal_success(state)
        inner.on_terminal_success.assert_awaited_once_with(state)
    else:
        error = RuntimeError("model failed")
        await lifecycle.on_terminal_failure(state, error)
        await lifecycle.flush_held_failure()
        inner.on_terminal_failure.assert_awaited_once_with(state, error)
    assert authorize.await_count == 2
    sleep.assert_awaited_once_with(1)
    inner.on_output_refused.assert_not_awaited()


@pytest.mark.parametrize("ending", ["success", "failure"])
async def test_inner_terminal_transient_error_is_logged_and_cleaned_up(ending):
    from unittest.mock import AsyncMock

    from daimon.core.turn.run import _DeferredFailureLifecycle
    from daimon.core.turn.state import TurnState
    from structlog.testing import capture_logs

    inner = AsyncMock()
    hook = inner.on_terminal_success if ending == "success" else inner.on_terminal_failure
    hook.side_effect = OSError("connection reset between chunks")
    authorize = AsyncMock()
    lifecycle = _DeferredFailureLifecycle(inner=inner, authorize_final=authorize)
    with capture_logs() as logs:
        if ending == "success":
            await lifecycle.on_terminal_success(TurnState())
        else:
            await lifecycle.on_terminal_failure(TurnState(), RuntimeError("model failed"))
            await lifecycle.flush_held_failure()
    authorize.assert_awaited_once()
    hook.assert_awaited_once()
    inner.on_output_refused.assert_awaited_once()
    assert logs == [
        {"event": "turn.final_output_refused", "error_type": "OSError", "log_level": "warning"}
    ]
