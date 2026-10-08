"""Regression: an ordinary first send and a checkpoint send decide again right before they post."""

import asyncio

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.core.turn.errors import AdmissionDenied, SessionBusyError
from daimon.core.turn.run import run_prepared_turn
from daimon.testing.ma import sdk_http_client
from daimon.testing.turn_fakes import RecordingLifecycle

from .test_action_time_races import setup
from .test_session_preparation import _agent, _register
from .turn.test_run_prepared_turn import _recovery_lifecycle

# Cases: (policy change during stream open, DM turn?, already read-only?, expected error).
# A DM turn whose tenant switches `dm_memory_read_only` on must wait, like a seal;
# unchanged and already-read-only DM turns still send.
_CASES = {
    "none": (None, False, False, None),
    "pin": ("pin", False, False, AdmissionDenied),
    "seal": ("seal", False, False, SessionBusyError),
    "dm-read-only": ("dm_read_only", True, False, SessionBusyError),
    "dm-unchanged": (None, True, False, None),
    "dm-already-read-only": ("dm_read_only", True, True, None),
}


@pytest.mark.parametrize("case", list(_CASES))
async def test_normal_turn_policy_change_during_stream_open(db_session, db_nullpool_engine, case):
    change, is_dm, read_only, error = _CASES[case]
    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    prepared = await bind(admitted(is_dm=is_dm, memory_read_only=read_only))
    inner = deps.anthropic._client._transport
    changed = False

    async def during_stream(request):
        nonlocal changed
        if (
            request.method == "GET"
            and request.url.path == f"/v1/sessions/{prepared.ma_session_id}/events/stream"
            and change is not None
        ):
            await policy(change)
            changed = True
        return await inner.handle_async_request(request)

    object.__setattr__(
        deps,
        "anthropic",
        AsyncAnthropic(
            api_key="test",
            http_client=sdk_http_client(
                httpx.AsyncClient(transport=httpx.MockTransport(during_stream))
            ),
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

    if error is None:
        outcome = await run()
        assert changed == (change is not None)
        assert outcome.state.error is None and not outcome.recovered
        assert transport.state.sent_batches
    else:
        with pytest.raises(error):
            await run()
        assert changed
        assert not transport.state.sent_batches, (
            change,
            transport.state.sessions[prepared.ma_session_id].metadata,
            transport.state.sessions[prepared.ma_session_id].resources,
            transport.state.sent_batches,
        )


@pytest.mark.parametrize("case", list(_CASES))
async def test_checkpoint_policy_change_during_checkpoint_stream_open(
    db_session, db_nullpool_engine, monkeypatch, case
):
    from unittest.mock import AsyncMock

    from daimon.core import workspace_transfer
    from daimon.core.turn.prepare import bind_session

    from .test_workspace_transfer import _seed_conversation

    change, is_dm, read_only, error = _CASES[case]
    tenant, account, factory, transport, deps, admitted, bind, policy = await setup(
        db_session, db_nullpool_engine
    )
    first = await bind(admitted(is_dm=is_dm, memory_read_only=read_only))
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
            and change is not None
        ):
            await policy(change)
            changed = True
        return await inner.handle_async_request(request)

    object.__setattr__(
        deps,
        "anthropic",
        AsyncAnthropic(
            api_key="test",
            http_client=sdk_http_client(httpx.AsyncClient(transport=httpx.MockTransport(lookup))),
        ),
    )

    async def rebind():
        return await bind_session(
            deps,
            admitted(moved, is_dm=is_dm, memory_read_only=read_only),
            tenant_id=tenant.id,
            platform="discord",
            external_user_id="user-1",
            thread_id="thread-1",
            session_account_id=account.id,
            reuse_existing=True,
        )

    if error is None:
        await rebind()
        assert changed == (change is not None)
        assert any(sid == first.ma_session_id for sid, _ in transport.state.sent_batches), (
            transport.state.sent_batches
        )
    else:
        with pytest.raises(error):
            await rebind()
        assert changed
        assert not any(sid == first.ma_session_id for sid, _ in transport.state.sent_batches), (
            change,
            transport.state.sent_batches,
        )
