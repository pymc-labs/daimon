"""Regression: an ordinary first send and a checkpoint send decide again right before they post."""

import asyncio

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core.turn.errors import AdmissionDenied, SessionBusyError
from daimon.core.turn.run import run_prepared_turn
from daimon.testing.turn_fakes import RecordingLifecycle

from .test_action_time_races import setup
from .test_session_preparation import _agent, _register
from .turn.test_run_prepared_turn import _recovery_lifecycle


@pytest.mark.parametrize("change", ["none", "pin", "seal"])
async def test_normal_turn_policy_change_during_stream_open(db_session, db_nullpool_engine, change):
    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    prepared = await bind(admitted())
    inner = deps.anthropic._client._transport
    changed = False

    async def during_stream(request):
        nonlocal changed
        if (
            request.method == "GET"
            and request.url.path == f"/v1/sessions/{prepared.ma_session_id}/events/stream"
            and change != "none"
        ):
            await policy(change)
            changed = True
        return await inner.handle_async_request(request)

    object.__setattr__(
        deps,
        "anthropic",
        AsyncAnthropic(
            api_key="test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(during_stream)),
        ),
    )

    async def reseed():
        pytest.fail("this is a live normal session, so recovery must not run")

    async def run():
        return await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thread-1",
            external_user_id="user-1",
            user_message="member request",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=_recovery_lifecycle,
            render_interval_s=0.001,
        )

    if change == "none":
        outcome = await run()
    else:
        with pytest.raises(AdmissionDenied if change == "pin" else SessionBusyError):
            await run()
    if change == "none":
        assert outcome.state.error is None and not outcome.recovered
        assert transport.state.sent_batches
    else:
        assert changed
        assert not transport.state.sent_batches, (
            change,
            transport.state.sessions[prepared.ma_session_id].metadata,
            transport.state.sessions[prepared.ma_session_id].resources,
            transport.state.sent_batches,
        )


@pytest.mark.parametrize("change", ["none", "pin", "seal"])
async def test_checkpoint_policy_change_during_checkpoint_stream_open(
    db_session, db_nullpool_engine, monkeypatch, change
):
    from unittest.mock import AsyncMock

    from daimon.core import workspace_transfer
    from daimon.core.turn.prepare import bind_session

    from .test_workspace_transfer import _seed_conversation

    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    first = await bind(admitted())
    moved = _agent(model_id="claude-opus-4-6")
    _register(transport.state, moved)
    inner = deps.anthropic._client._transport
    changed = False
    _seed_conversation(transport.state, first.ma_session_id, with_reply=True)
    # Bundle polling follows the effect under review; keep this test bounded.
    monkeypatch.setattr(workspace_transfer, "_poll_for_bundle", AsyncMock(return_value=None))

    # After the in-lock decision and the transcript replay: the checkpoint
    # turn opens its stream on the old session, then sends.
    async def lookup(request):
        nonlocal changed
        if (
            not changed
            and request.method == "GET"
            and request.url.path == f"/v1/sessions/{first.ma_session_id}/events/stream"
            and change != "none"
        ):
            await policy(change)
            changed = True
        return await inner.handle_async_request(request)

    object.__setattr__(
        deps,
        "anthropic",
        AsyncAnthropic(
            api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(lookup))
        ),
    )

    async def rebind():
        return await bind_session(
            deps,
            admitted(moved),
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-1",
            session_account_id=account.id,
            reuse_existing=True,
        )

    if change == "none":
        await rebind()
    else:
        with pytest.raises(AdmissionDenied if change == "pin" else SessionBusyError):
            await rebind()
    if change == "none":
        assert any(sid == first.ma_session_id for sid, _ in transport.state.sent_batches), (
            transport.state.sent_batches
        )
    else:
        assert changed
        assert not any(sid == first.ma_session_id for sid, _ in transport.state.sent_batches), (
            change,
            transport.state.sent_batches,
        )
