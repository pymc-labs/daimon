"""Prepared billing uses native usage without fabricating SDK span meters."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal, NoReturn, cast

import anthropic
import httpx
import pytest
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from daimon.core.scope import ResolvedConfig
from daimon.core.turn import driver
from daimon.core.turn import run as run_module
from daimon.core.turn.admission import Admission
from daimon.core.turn.io import TurnEvent, TurnIO, TurnStream
from daimon.core.turn.posture import Billed
from daimon.core.turn.prepare import PreparedTurn
from daimon.core.turn.provider_actions import ProviderActionApproval, ProviderActionPrompt
from daimon.core.turn.run import prepared_billing, run_prepared_turn_impl
from daimon.core.usage_billing import ObservationBilled
from daimon.testing.ma import MARouter
from daimon.testing.ma_models import ma_agent, ma_environment, ma_model_usage
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef, ResourceRef, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.errors import ScopeViolation
from mux.state.memory import MemoryStateStore
from mux.state.store import binding_slot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_run_prepared_turn import _deps

TENANT = uuid.UUID(int=6604)
ACCOUNT = uuid.UUID(int=6605)
CHANNEL = ChannelRef(tenant_id=str(TENANT), platform="slack", channel_id="channel")
NATIVE = ResourceRef(
    id="native-session",
    kind="session",
    provider="openai",
    account_scope_id="project",
    tenant_id=str(TENANT),
    account_id=str(ACCOUNT),
)
OBSERVATION = UsageObservation(
    id="actual-measurement",
    revision=2,
    session=NATIVE,
    grain="turn",
    basis="cumulative",
    input_tokens=14,
    output_tokens=None,
    completeness="partial",
    observed_at=datetime.now(UTC),
    native_meter={"actual": 14},
)


def admitted() -> Admission:
    return Admission(
        account_id=ACCOUNT,
        agent=ma_agent(tenant_id=TENANT),
        environment=ma_environment(),
        config=ResolvedConfig(agent_name="test", environment_name="test"),
    )


async def test_default_anthropic_recorder_receives_the_original_sdk_event() -> None:
    calls: list[BetaManagedAgentsSpanModelRequestEndEvent] = []

    async def record(*, event: BetaManagedAgentsSpanModelRequestEndEvent) -> None:
        calls.append(event)

    prepared = PreparedTurn(
        admission=admitted(),
        ma_session_id="anthropic-session",
        mapping_id=None,
        watermark=None,
        reused=False,
        session_account_id=ACCOUNT,
        _record=record,
    )
    billing = prepared_billing(prepared, tenant_id=TENANT)
    assert isinstance(billing, Billed)
    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id="native-sdk-measurement",
        type="span.model_request_end",
        model_request_start_id="start",
        model_usage=ma_model_usage(input_tokens=10, output_tokens=5),
        processed_at=datetime.now(UTC),
        is_error=False,
    )
    await billing.record(event=event)
    assert calls == [event] and calls[0] is event
    assert billing.record is record


async def test_foreign_callback_receives_usage_only_frame_and_keeps_pending_signal() -> None:
    calls: list[UsageObservation] = []

    async def record(*, observation: UsageObservation) -> bool:
        calls.append(observation)
        return False

    revision = ConfigRevision.create(
        CHANNEL,
        1,
        resolve_default(
            BackendConfig(
                backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna"
            )
        ),
    )
    prepared = PreparedTurn(
        admission=replace(admitted(), backend_revision=revision),
        ma_session_id=NATIVE.id,
        mapping_id=None,
        watermark=None,
        reused=False,
        session_account_id=ACCOUNT,
        _record=record,
        session_ref=NATIVE,
    )
    billing = prepared_billing(prepared, tenant_id=TENANT)
    assert isinstance(billing, ObservationBilled)
    item = TurnEvent(usage=OBSERVATION)
    assert item.native is None and item.normalized is None
    assert item.usage is not None
    assert await billing.record(observation=item.usage) is False
    assert calls == [OBSERVATION] and calls[0] is OBSERVATION
    assert calls[0].output_tokens is None and calls[0].revision == 2


@pytest.mark.parametrize(
    "change", ["ref", "tenant", "account", "session", "provider", "kind", "config"]
)
def test_foreign_recorder_requires_an_admitted_authorized_native_ref(change: str) -> None:
    async def record(*, observation: UsageObservation) -> bool:
        raise AssertionError("unproved recorder must not run")

    revision = ConfigRevision.create(
        CHANNEL,
        1,
        resolve_default(
            BackendConfig(
                backend="openai", profile="openai.persistent_workspace", model="gpt-6-luna"
            )
        ),
    )
    ref: ResourceRef | None = NATIVE
    if change == "ref":
        ref = None
    elif change == "config":
        revision = revision.model_copy(
            update={"channel": CHANNEL.model_copy(update={"tenant_id": "other"})}
        )
    else:
        field = {
            "tenant": "tenant_id",
            "account": "account_id",
            "session": "id",
            "provider": "provider",
            "kind": "kind",
        }[change]
        ref = NATIVE.model_copy(update={field: "anthropic" if change == "provider" else "other"})
    prepared = PreparedTurn(
        admission=replace(admitted(), backend_revision=revision),
        ma_session_id=NATIVE.id,
        mapping_id=None,
        watermark=None,
        reused=False,
        session_account_id=ACCOUNT,
        _record=record,
        session_ref=ref,
    )
    with pytest.raises(ScopeViolation):
        prepared_billing(prepared, tenant_id=TENANT)


def test_default_profile_cannot_bill_a_foreign_native_session() -> None:
    async def record(*, observation: UsageObservation) -> bool:
        raise AssertionError("foreign recorder must not run under the default profile")

    prepared = PreparedTurn(
        admission=admitted(),
        ma_session_id=NATIVE.id,
        mapping_id=None,
        watermark=None,
        reused=False,
        session_account_id=ACCOUNT,
        _record=record,
        session_ref=NATIVE,
    )
    with pytest.raises(ScopeViolation, match="admitted profile"):
        prepared_billing(prepared, tenant_id=TENANT)


@pytest.mark.parametrize(
    "provider,profile,model",
    [
        ("openai", "openai.persistent_workspace", "gpt-6-luna"),
        ("gemini", "gemini.inline_reuse", "gemini-3.8-flash"),
    ],
)
async def test_prepared_foreign_failure_reconciles_real_usage_without_anthropic_recovery(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    provider: Literal["openai", "gemini"],
    profile: str,
    model: str,
) -> None:
    ref = NATIVE.model_copy(update={"provider": provider})
    observation = OBSERVATION.model_copy(update={"session": ref})
    revision = ConfigRevision.create(
        CHANNEL, 1, resolve_default(BackendConfig(backend=provider, profile=profile, model=model))
    )
    store = MemoryStateStore()
    binding = ProviderBinding(
        id="actual-provider-binding",
        thread=ThreadRef(channel=CHANNEL, thread_id="thread"),
        provider=provider,
        profile=profile,
        native_refs={"session": ref.id},
        generation=1,
        config_revision=revision.local,
        legacy_account_id=str(ACCOUNT),
    )
    await store.put_binding(binding, expected_generation=0)
    deps = replace(
        _deps(sessionmaker=db_session_factory, router=MARouter()),
        turn_path="mux",
        state_store=store,
    )
    recorded: list[UsageObservation] = []

    async def record(*, observation: UsageObservation) -> bool:
        recorded.append(observation)
        return False

    prepared = PreparedTurn(
        admission=replace(admitted(), backend_revision=revision),
        ma_session_id=ref.id,
        mapping_id=None,
        watermark=None,
        reused=False,
        session_account_id=ACCOUNT,
        _record=record,
        session_ref=ref,
    )

    class FailedCodec:
        async def open_stream(self, *, read_timeout_s: float) -> TurnStream:
            response = httpx.Response(
                404, request=httpx.Request("GET", "https://test.invalid/session")
            )
            raise anthropic.NotFoundError("native session gone", response=response, body=None)

        async def replay_usage(self) -> list[UsageObservation]:
            return [observation]

    codec = FailedCodec()

    async def provider_hook(prompt: ProviderActionPrompt) -> None:
        raise AssertionError("failing stream cannot ask for approval")

    approvals: list[ProviderActionApproval] = []

    def factory(*args: object, **kwargs: object) -> TurnIO:
        approval = kwargs.get("provider_actions")
        assert isinstance(approval, ProviderActionApproval)
        assert approval.hook is provider_hook
        assert approval.requester.platform == "slack"
        assert approval.requester.thread_id == "thread"
        assert approval.requester.platform_user_id == "caller"
        approvals.append(approval)
        return cast(TurnIO, codec)

    async def forbidden_recovery(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("a foreign dead session cannot use Anthropic recovery")

    async def forbidden_reseed() -> NoReturn:
        raise AssertionError("a foreign dead session cannot reseed Anthropic")

    monkeypatch.setattr(driver, "turn_io", factory)
    monkeypatch.setattr(run_module, "_replace_dead_session", forbidden_recovery)
    monkeypatch.setattr(run_module, "bind_recorder", forbidden_recovery)
    result = await run_prepared_turn_impl(
        deps,
        prepared,
        tenant_id=TENANT,
        platform="slack",
        thread_id="thread",
        external_user_id="caller",
        user_message="hi",
        lifecycle=RecordingLifecycle(),
        cancel=asyncio.Event(),
        reseed_user_message=forbidden_reseed,
        recovery_lifecycle=lambda cancel: RecordingLifecycle(),
        provider_action=provider_hook,
    )
    assert len(approvals) == 1
    assert approvals[0].expires_at > datetime.now(UTC)
    assert result.recovered is False and result.ma_session_id == ref.id
    assert result.state.error is not None and result.state.error.kind == "upstream"
    assert recorded == [observation] and recorded[0] is observation
    assert recorded[0].output_tokens is None and recorded[0].session.provider == provider
    # The real host/driver composition released its admitted binding lease.
    lease = await store.acquire_lease(
        binding_slot(binding),
        holder="after-failure",
        turn_id="next-turn",
        now=datetime.now(UTC),
        ttl=timedelta(minutes=5),
    )
    assert lease.holder == "after-failure"
