"""Authoritative foreign root failures survive display compatibility and host storage."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from anthropic.types.beta.sessions import BetaManagedAgentsSessionRetriesExhausted
from daimon.core.turn import driver
from daimon.core.turn.gemini import GeminiTurnIO
from daimon.core.turn.io import TurnEvent
from daimon.core.turn.openai_codec import display_event
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.termination import (
    ProviderTerminalFailure,
    TerminationReason,
    termination_reason,
)
from daimon.testing.factories import make_tenant
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.events import Event, NativeProvenance
from mux.contracts.ids import ResourceRef, Scope
from mux.testing.drivers.anthropic.ma_transport import ScriptedTransport
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from . import test_gemini_codec as gemini_native
from .conftest import make_agent_message, make_status_idle
from .test_provider_dispatch import ACCOUNT, TENANT

SCOPE = Scope(
    tenant_id=str(TENANT), account_id=str(ACCOUNT), principal_id="host", authorization_id="offline"
)


def session(provider: str, id_: str = "session") -> ResourceRef:
    return ResourceRef(
        provider=provider,
        kind="session",
        id=id_,
        tenant_id=SCOPE.tenant_id,
        account_id=SCOPE.account_id,
        account_scope_id="offline-project",
    )


def terminal(provider: str, outcome: str) -> Event:
    return Event.model_validate(
        {
            "id": "ended:" + outcome,
            "session_id": "session",
            "sequence": 0,
            "type": "session.turn_ended",
            "authority": "record",
            "observed_at": datetime.now(UTC),
            "turn_id": "root",
            "payload": {"root_turn_id": "root", "outcome": outcome},
            "native": NativeProvenance(provider=provider, api_revision="offline"),
        }
    )


class OfflineIO:
    """Script normalized input; use production display codecs and the host pump."""

    def __init__(self, event: Event, leading=()):
        self.event = event
        self.sent = False
        if event.native.provider == "anthropic":
            self.frame = TurnEvent(
                make_status_idle(
                    event_id=event.id,
                    stop_reason=BetaManagedAgentsSessionRetriesExhausted(type="retries_exhausted"),
                ),
                event,
            )
        elif event.native.provider == "openai":
            self.frame = TurnEvent(display_event(event, session_id=event.session_id), event)
        else:
            backend = SimpleNamespace(
                capabilities=lambda: SimpleNamespace(profile_id="gemini.inline_reuse")
            )
            self.frame = GeminiTurnIO(backend, SCOPE, session("gemini")).frame(event, {})
        self.frames = [*leading, self.frame]

    async def open_stream(self, **kwargs):
        return self

    async def send(self, events):
        self.sent = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        assert self.sent, "host must send before consuming the terminal record"
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)

    async def close(self):
        pass


@pytest.fixture
async def host(db_engine: AsyncEngine, db_clean):
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm.begin() as db:
        await make_tenant(db, id=TENANT)
    return sm


async def run_host(monkeypatch, event, leading=()):
    io = OfflineIO(event, leading)
    monkeypatch.setattr(driver, "turn_io", lambda *args, **kwargs: io)
    transport = ScriptedTransport()
    lifecycle = RecordingLifecycle()
    async with transport.client() as client:
        state = await driver.run_turn(
            anthropic=client,
            session_id=event.session_id,
            user_message="offline",
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            billing=BillingExempt(reason="headless-unrecorded"),
            path="mux",
            scope=SCOPE,
            render_interval_s=3600,
        )
    assert not transport.requests
    if state.error is not None:
        assert lifecycle.terminal_failures == [(state, state.error)]
        assert not lifecycle.terminal_success
    else:
        assert lifecycle.terminal_success == [state]
        assert not lifecycle.terminal_failures
    return state


async def outcome_row(host, native_session, run):
    from daimon.core.stores.turn_outcomes import list_for_tenant
    from daimon.core.turn.outcomes import TurnObservation, drain_outcomes

    observation = TurnObservation(
        host, TENANT, "slack", account_id=ACCOUNT, session_id=native_session.id
    )
    with observation.activate():
        state = await run()
        observation.finish(state=state)
    await drain_outcomes()
    async with host() as db:
        rows = await list_for_tenant(db, TENANT)
    matches = [row for row in rows if row.id == observation.id]
    assert len(matches) == 1
    row = matches[0]
    assert row.account_id == ACCOUNT and row.session_id == native_session.id
    return state, row


@pytest.fixture
async def native_gemini(monkeypatch):
    # Use the actual driver setup with the same authorized DB tenant/account.
    monkeypatch.setattr(gemini_native, "SCOPE", SCOPE)
    channel = gemini_native.CHANNEL.model_copy(update={"tenant_id": SCOPE.tenant_id})
    monkeypatch.setattr(
        gemini_native, "THREAD", gemini_native.THREAD.model_copy(update={"channel": channel})
    )
    return await gemini_native.harness.__wrapped__()


@pytest.mark.parametrize(
    "native_status,expected",
    [
        ("completed", TerminationReason.COMPLETED),
        ("cancelled", TerminationReason.INTERRUPTED),
        ("failed", TerminationReason.UPSTREAM),
        ("incomplete", TerminationReason.SESSION_TERMINATED),
    ],
)
async def test_actual_gemini_stream_terminal_reason_is_committed(
    host, monkeypatch, native_gemini, native_status, expected
):
    native_gemini.transport.responses.append(
        gemini_native.interaction("actual-" + native_status, status=native_status)
    )
    io = GeminiTurnIO(native_gemini.backend, SCOPE, native_gemini.session)
    monkeypatch.setattr(driver, "turn_io", lambda *args, **kwargs: io)
    lifecycle = RecordingLifecycle()
    ancillary = ScriptedTransport()

    async def run():
        async with ancillary.client() as client:
            return await driver.run_turn(
                anthropic=client,
                session_id=native_gemini.session.id,
                user_message="offline",
                lifecycle=lifecycle,
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="headless-unrecorded"),
                path="mux",
                scope=SCOPE,
                render_interval_s=3600,
            )

    state, row = await outcome_row(host, native_gemini.session, run)
    assert state.termination == row.reason == expected
    assert (state.error is not None) == expected.is_failure
    assert row.error_class == ("TurnError" if expected.is_failure else None)
    assert lifecycle.terminal_failures == ([(state, state.error)] if expected.is_failure else [])
    assert lifecycle.terminal_success == ([] if expected.is_failure else [state])
    assert len(native_gemini.transport.requests) == 1
    assert not ancillary.requests


@pytest.mark.parametrize(
    "native_status,expected",
    [("failed", TerminationReason.UPSTREAM), ("incomplete", TerminationReason.SESSION_TERMINATED)],
)
async def test_actual_gemini_restart_replay_remains_a_typed_failure(
    native_gemini, native_status, expected
):
    native_gemini.transport.responses.append(
        gemini_native.interaction("saved-failure", status=native_status)
    )
    original = GeminiTurnIO(native_gemini.backend, SCOPE, native_gemini.session)
    await original.send(
        [{"type": "user.message", "content": [{"type": "text", "text": "offline"}]}]
    )
    restarted = GeminiTurnIO(native_gemini.backend, SCOPE, native_gemini.session)
    with pytest.raises(ProviderTerminalFailure) as caught:
        await restarted.replay()
    assert caught.value.reason == termination_reason(caught.value) == expected
    assert caught.value.category == "upstream" and caught.value.retryable is False
    assert caught.value.native_code == "host_root_failed"
    assert len(native_gemini.transport.requests) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"authority": "preview"},
        {"authority": "gap"},
        {"thread_id": "child"},
        {"turn_id": "foreign"},
        {"session_id": "foreign"},
        {"native": NativeProvenance(provider="openai", api_revision="offline")},
    ],
)
async def test_gemini_failure_boundary_requires_accepted_own_root(native_gemini, fields):
    io = GeminiTurnIO(native_gemini.backend, SCOPE, native_gemini.session)
    io.root = "root"
    event = terminal("gemini", "terminated").model_copy(
        update={"session_id": native_gemini.session.id, **fields}
    )
    io.check_terminal(event)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize(
    "outcome,expected",
    [
        ("completed", TerminationReason.COMPLETED),
        ("interrupted", TerminationReason.INTERRUPTED),
        ("errored", TerminationReason.UPSTREAM),
        ("terminated", TerminationReason.SESSION_TERMINATED),
    ],
)
async def test_provider_root_outcome_reaches_terminal_hook_and_committed_row(
    host, monkeypatch, provider, outcome, expected
):
    async def run():
        return await run_host(monkeypatch, terminal(provider, outcome))

    state, row = await outcome_row(host, session(provider), run)
    assert state.termination == row.reason == expected
    assert (state.error is not None) == expected.is_failure
    assert row.error_class == ("TurnError" if expected.is_failure else None)


async def test_anthropic_retries_exhausted_keeps_legacy_completed_row(host, monkeypatch):
    async def run():
        return await run_host(monkeypatch, terminal("anthropic", "errored"))

    state, row = await outcome_row(host, session("anthropic"), run)
    assert state.termination == row.reason == TerminationReason.COMPLETED
    assert state.error is None and row.error_class is None


@pytest.mark.parametrize("provider", ["openai", "gemini"])
@pytest.mark.parametrize(
    "fields", [{"authority": "preview"}, {"authority": "gap"}, {"thread_id": "child"}]
)
async def test_non_authoritative_or_child_failure_cannot_replace_root_completion(
    host, monkeypatch, provider, fields
):
    # Inject a nonterminal display frame with untrusted normalized metadata.
    # The host must not promote it to a root failure before the real root ends.
    noise = TurnEvent(
        make_agent_message(event_id="partial", text="partial"),
        terminal(provider, "errored").model_copy(update=fields),
    )

    async def run():
        return await run_host(monkeypatch, terminal(provider, "completed"), [noise])

    state, row = await outcome_row(host, session(provider), run)
    assert state.termination == row.reason == TerminationReason.COMPLETED
    assert state.error is None
