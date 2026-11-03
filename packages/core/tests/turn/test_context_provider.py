"""Actual MA turn assembly with fake I/O, including timeout and accepted-send edge."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from daimon.core.context_provider import (
    ContextHook,
    ContextProviders,
    ContextRequest,
    ContextResponse,
    LoadedProvider,
    ProviderConfig,
)
from daimon.core.turn.posture import BillingExempt
from daimon.testing.turn_fakes import FakeAnthropic, RecordingLifecycle, YieldEvent
from daimon.testing.turn_fakes import run_turn_on_leg as run_turn

from ..context_card_stub import FixtureProvider
from ..test_context_provider import CONFIG, FIXTURE, REQUEST, TENANT, loaded
from .conftest import make_end_turn, make_status_idle


async def drive(owner: ContextProviders, *, session_id: str = "s1", load=loaded):
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_status_idle(event_id="idle", stop_reason=make_end_turn())),
        ]
    ]
    lifecycle = RecordingLifecycle()
    state = await run_turn(
        anthropic=cast(AsyncAnthropic, fake),
        session_id=session_id,
        user_message="hello",
        lifecycle=lifecycle,
        cancel=asyncio.Event(),
        render_interval_s=0.001,
        billing=BillingExempt(reason="cli-operator-run"),
        context_hook=ContextHook(owner, REQUEST, load),
        system_blocks=({"type": "text", "text": "existing continuity framing"},),
    )
    assert state.error is None
    assert len(lifecycle.terminal_success) == 1
    return fake.beta.sessions.events.sent_events[0][1]


async def test_driver_attaches_before_inference_and_dedupes_after_send() -> None:
    provider = FixtureProvider(FIXTURE, tenant_id=str(TENANT))
    owner = ContextProviders(provider=provider)
    batch = await drive(owner)
    assert [e["type"] for e in batch] == ["user.message", "system.message"]
    assert "person" in batch[0]["content"][0]["text"]
    assert batch[0]["content"][0]["text"].endswith("hello")
    assert [b["text"] for b in batch[1]["content"]] == [
        "existing continuity framing",
        provider.snapshot.system_fragment,
    ]
    second = await drive(owner)
    assert second[0]["content"][0]["text"] == "hello"
    assert len(second[1]["content"]) == 1
    reset = await drive(owner, session_id="replacement")
    assert "context_cards" in reset[0]["content"][0]["text"]
    assert len(reset[1]["content"]) == 2


@pytest.mark.parametrize("fault", ["timeout", "oversize", "failure", "disabled", "no_binding"])
async def test_failed_context_never_fails_or_changes_the_turn(fault: str) -> None:
    async def provider(config: ProviderConfig, request: ContextRequest) -> ContextResponse:
        if fault == "timeout":
            await asyncio.Event().wait()
        if fault == "failure":
            raise RuntimeError("provider unavailable")
        return ContextResponse(status="ok", turn_fragment="x" * 9000)

    async def load(request: ContextRequest) -> LoadedProvider | None:
        if fault == "no_binding":
            return None
        return LoadedProvider(
            CONFIG.model_copy(
                update={
                    "timeout_ms": 10,
                    "first_call_timeout_ms": 10,
                    "enabled": fault != "disabled",
                }
            ),
            "fixture-server",
        )

    batch = await drive(ContextProviders(provider=provider), load=load)
    assert batch == [
        {"type": "user.message", "content": [{"type": "text", "text": "hello"}]},
        {
            "type": "system.message",
            "content": [{"type": "text", "text": "existing continuity framing"}],
        },
    ]


async def test_denied_before_send_does_not_acknowledge_context() -> None:
    provider = FixtureProvider(FIXTURE, tenant_id=str(TENANT))
    owner = ContextProviders(provider=provider)
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [
        [
            YieldEvent(make_status_idle(event_id="idle", stop_reason=make_end_turn())),
        ]
    ]

    async def refuse() -> None:
        raise PermissionError("send is denied")

    with pytest.raises(PermissionError):
        await run_turn(
            anthropic=cast(AsyncAnthropic, fake),
            session_id="s1",
            user_message="hello",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            billing=BillingExempt(reason="cli-operator-run"),
            context_hook=ContextHook(owner, REQUEST, loaded),
            before_send=refuse,
        )
    assert not fake.beta.sessions.events.sent_events
    assert not next(iter(owner.sessions.values())).hashes
    batch = await drive(owner)
    assert "context_cards" in batch[0]["content"][0]["text"]


@pytest.mark.parametrize("place_kind", ["channel", "thread", "dm", "unknown"])
@pytest.mark.parametrize("older_session", [False, True])
async def test_prepared_path_loads_tenant_binding_and_trusted_request_fields(
    db_session,
    db_nullpool_engine,
    monkeypatch,
    place_kind: str,
    older_session: bool,
) -> None:
    from dataclasses import replace
    from types import SimpleNamespace

    from daimon.core import context_provider as context_module
    from daimon.core.authz import AgentRef, Place, Subject, Surface, build_turn_place
    from daimon.core.stores.context_providers import set_provider
    from daimon.core.turn.admission import AdmissionGrant
    from daimon.core.turn.run import run_prepared_turn
    from daimon.testing.factories import make_account, make_tenant, make_thread_session
    from daimon.testing.ma_models import ma_agent, ma_environment
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from .test_run_prepared_turn import (
        _admission,
        _deps,
        _prepared_turn,
        _recovery_lifecycle,
        _reseed,
        _router,
    )

    # This test verifies DB-backed assembly, not wall-clock performance. Keep
    # host scheduling out of its budget; dedicated fake tests exercise deadlines.
    async def complete_fixture_io(call, seconds, detached):
        return await call

    monkeypatch.setattr(context_module, "_bounded", complete_fixture_io)
    monkeypatch.setattr(context_module, "time", SimpleNamespace(monotonic=lambda: 0.0))
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    tenant = await make_tenant(db_session, workspace_id="fixture-server")
    account = await make_account(db_session, tenant=tenant)
    row = await make_thread_session(
        db_session,
        tenant=tenant,
        account=account,
        platform="discord",
        thread_id="thread-1",
        ma_session_id="sess_1",
    )
    await set_provider(
        db_session,
        tenant_id=tenant.id,
        config=CONFIG,
        actor=Subject(is_admin=True),
        set_by="test",
    )
    await db_session.commit()
    sent = []
    router = _router(session_bodies=[], dead_session_ids=set(), sent_batches=sent)
    provider = FixtureProvider(FIXTURE, tenant_id=str(tenant.id))
    deps = replace(
        _deps(sessionmaker=factory, router=router),
        context_providers=ContextProviders(provider=provider),
    )
    admission = _admission(
        account_id=account.id,
        agent=ma_agent(id="ag_1", tenant_id=tenant.id, model="claude-sonnet-5"),
        env=ma_environment(id="env_1", tenant_id=tenant.id),
    )
    dm = place_kind == "dm"
    thread = place_kind == "thread"
    place = (
        Place()
        if dm
        else Place(parent_unresolved=True)
        if place_kind == "unknown"
        else build_turn_place(channel_id="staff", thread_id="thread-1" if thread else None)
    )
    admission = replace(
        admission,
        origin_channel_id="dm-alice" if dm else "staff",
        origin_thread_id="thread-1" if thread else None,
        private_dm_id="dm-alice" if dm else None,
        grant=AdmissionGrant(
            tenant_id=tenant.id,
            subject=Subject(platform_user_id="alice"),
            surface=Surface.DM if dm else Surface.CHANNEL,
            turn_place=place,
            run_place=place,
            agent=AgentRef.of("daimon"),
            channel_id="dm-alice" if dm else "staff",
            thread_id="thread-1" if thread else None,
            is_dm=dm,
            human_invocation=True,
        ),
    )
    prepared = _prepared_turn(
        deps=deps,
        admission=admission,
        tenant_id=tenant.id,
        external_user_id="alice",
        ma_session_id="sess_1",
        mapping_id=row.id,
        session_account_id=account.id,
    )
    prepared = replace(
        prepared,
        session_model_id="claude-sonnet-4-6" if older_session else "claude-sonnet-5",
    )
    outcome = await run_prepared_turn(
        deps,
        prepared,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thread-1",
        external_user_id="alice",
        user_message="adapter framing and message",
        context_message_text="Orbit's plan",
        context_mention_ids=("bob",),
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=_reseed,
        recovery_lifecycle=_recovery_lifecycle,
        render_interval_s=0.001,
    )
    assert outcome.state.error is None
    if place_kind == "unknown":
        assert not provider.requests
        return
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.tenant_id == tenant.id and request.server_id == "fixture-server"
    if dm:
        assert request.channel_id == "dm-alice"
        assert request.parent_channel_id is None and request.place_type == "dm"
    elif thread:
        assert request.channel_id == request.thread_id == "thread-1"
        assert request.parent_channel_id == "staff" and request.place_type == "thread"
    else:
        assert request.channel_id == "staff" and request.place_type == "channel"
        assert request.parent_channel_id is None and request.thread_id is None
    assert request.mention_ids == ("bob",) and request.message_text == "Orbit's plan"
    assert request.agent_id == "ag_1" and request.requester_platform_user_id == "alice"
    assert "context_cards" in sent[0][1][0]["content"][0]["text"]
    if older_session:
        assert sent[0][1][0]["content"][0]["text"].startswith("Dated reference data")
        assert len(sent[0][1]) == 1
    else:
        assert sent[0][1][-1]["type"] == "system.message"


async def test_unsupported_system_message_models_receive_once_only_user_fragment() -> None:
    owner = ContextProviders(provider=FixtureProvider(FIXTURE, tenant_id=str(TENANT)))
    fake = FakeAnthropic()
    hook = ContextHook(owner, REQUEST, loaded, system_message_supported=False)
    for _ in range(2):
        fake.beta.sessions.events.stream_scripts.append(
            [
                YieldEvent(make_status_idle(event_id="idle", stop_reason=make_end_turn())),
            ]
        )
        state = await run_turn(
            anthropic=cast(AsyncAnthropic, fake),
            session_id="s1",
            user_message="hello",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            render_interval_s=0.001,
            billing=BillingExempt(reason="cli-operator-run"),
            context_hook=hook,
        )
        assert state.error is None
    first, second = [batch for _, batch in fake.beta.sessions.events.sent_events]
    assert len(first) == len(second) == 1
    assert first[0]["content"][0]["text"].startswith("Dated reference data")
    assert second[0]["content"][0]["text"] == "hello"


async def test_failed_io_send_does_not_acknowledge_context(monkeypatch) -> None:
    owner = ContextProviders(provider=FixtureProvider(FIXTURE, tenant_id=str(TENANT)))
    fake = FakeAnthropic()
    fake.beta.sessions.events.stream_scripts = [
        [YieldEvent(make_status_idle(event_id="idle", stop_reason=make_end_turn()))]
    ]

    async def failed_send(*args, **kwargs):
        raise RuntimeError("send failed")

    monkeypatch.setattr(fake.beta.sessions.events, "send", failed_send)
    with pytest.raises(RuntimeError, match="send failed"):
        await run_turn(
            anthropic=cast(AsyncAnthropic, fake),
            session_id="s1",
            user_message="hello",
            lifecycle=RecordingLifecycle(),
            cancel=asyncio.Event(),
            billing=BillingExempt(reason="cli-operator-run"),
            context_hook=ContextHook(owner, REQUEST, loaded),
        )
    cached = next(iter(owner.sessions.values()))
    assert not cached.hashes and not cached.system_received
    assert "context_cards" in (await drive(owner))[0]["content"][0]["text"]


async def test_turn_fragment_is_separated_from_user_text() -> None:
    async def provider(config, request):
        return ContextResponse(status="ok", turn_fragment="reference text")

    batch = await drive(ContextProviders(provider=provider))
    assert batch[0]["content"][0]["text"] == "reference text\n\nhello"
