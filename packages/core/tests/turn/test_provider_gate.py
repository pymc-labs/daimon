"""A provider key's gate on a turn: cooldowns, caps and what every provider runs through."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from daimon.core.mux_backend import TurnRuntime
from daimon.core.provider_key_gate import ProviderKey
from daimon.core.provider_keys import DEFAULT_KEY_ENV
from daimon.core.turn import openai_host
from daimon.core.turn.daily_model_cap import CappedFlashAccounting
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini_prepare import prepare_gemini_turn
from daimon.core.turn.provider_gate import CooldownTransport, gated_turn
from mux.contracts.errors import ProviderError
from mux.contracts.ids import ModelRef
from mux.drivers.gemini import FlashAttempt
from mux.profiles import INLINE_REUSE, PERSISTENT_WORKSPACE

TENANT = uuid.uuid4()
KEY = ProviderKey("openai", "digest")


class FakeGate:
    def __init__(self, *, cooling: bool = False, full: bool = False) -> None:
        self.cooling, self.full = cooling, full
        self.leases: set[uuid.UUID] = set()
        self.cooldowns: list[tuple[ProviderKey, str, float]] = []

    async def cooling_until(self, key: ProviderKey, model: str) -> datetime | None:
        return datetime.now(UTC) + timedelta(seconds=30) if self.cooling else None

    async def acquire(self, key: ProviderKey, tenant_id: uuid.UUID) -> uuid.UUID | None:
        if self.full:
            return None
        lease = uuid.uuid4()
        self.leases.add(lease)
        return lease

    async def release(self, lease: uuid.UUID) -> None:
        self.leases.discard(lease)

    async def record_cooldown(self, key: ProviderKey, model: str, wait_s: float) -> None:
        self.cooldowns.append((key, model, wait_s))


@dataclass(frozen=True)
class Prepared:
    _finalize: object = None


def request_for(gate: FakeGate | None, *, profile: str = "p", model: str | None = "m"):
    revision = SimpleNamespace(profile=profile, model=model, backend="openai")
    return SimpleNamespace(
        deps=SimpleNamespace(provider_gate=gate, turn_runtimes={}, sessionmaker=None),
        admission=SimpleNamespace(backend_revision=revision, resolved=lambda name: None),
        tenant_id=TENANT,
        now=lambda: datetime.now(UTC),
    )


async def test_a_cooling_key_refuses_before_any_preparation_or_provider_call():
    gate, called = FakeGate(cooling=True), []

    async def prepare():
        called.append("prepared")

    with pytest.raises(AdmissionDenied) as refused:
        await gated_turn(request_for(gate), KEY, prepare)
    assert (refused.value.reason, refused.value.cause) == (
        "backend_unsupported",
        "provider_cooling",
    )
    assert refused.value.provider == "openai" and not called and not gate.leases


async def test_a_full_key_refuses_as_busy_without_preparing():
    called = []

    async def prepare():
        called.append("prepared")

    with pytest.raises(AdmissionDenied) as refused:
        await gated_turn(request_for(FakeGate(full=True)), KEY, prepare)
    assert refused.value.cause == "provider_busy" and not called


async def test_the_lease_is_held_until_the_turn_ends_and_freed_when_preparation_fails():
    gate, ended = FakeGate(), []

    async def end() -> None:
        ended.append("ended")

    async def prepare():
        return Prepared(end)

    prepared = await gated_turn(request_for(gate), KEY, prepare)
    assert len(gate.leases) == 1, "held while the turn runs"
    await prepared._finalize()
    assert ended == ["ended"] and not gate.leases, (
        "the turn's own cleanup runs, then the lease frees"
    )

    async def fail():
        raise RuntimeError("preparation failed")

    with pytest.raises(RuntimeError):
        await gated_turn(request_for(gate), KEY, fail)
    assert not gate.leases


@pytest.mark.parametrize("gated", [False, True])
async def test_a_turn_with_no_gate_or_no_key_runs_ungated(gated: bool):
    async def prepare():
        return "ran"

    gate = FakeGate(full=True) if gated else None
    assert await gated_turn(request_for(gate), None if gated else KEY, prepare) == "ran"


async def test_a_provider_rate_limit_puts_the_key_and_model_on_cooldown():
    gate = FakeGate()

    class Limited:
        async def request(self, *args: object, **kwargs: object) -> object:
            raise ProviderError("rate_limited", retryable=True, retry_after_s=7.5)

    transport = CooldownTransport(Limited(), gate, KEY, "gpt")  # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        await transport.request("GET", "/agents/sessions/s")
    assert gate.cooldowns == [(KEY, "gpt", 7.5)]


async def test_gemini_records_a_rate_limit_and_refuses_a_cooling_fallback():
    gate, request = FakeGate(), request_for(FakeGate())
    request.deps.provider_gate = gate
    inner = SimpleNamespace(before=[], after=[])

    async def before(**kwargs: object) -> None:
        inner.before.append(kwargs)

    async def after(**kwargs: object) -> None:
        inner.after.append(kwargs)

    inner.before_attempt, inner.after_attempt = before, after
    accounting = CappedFlashAccounting(inner, request, {}, None, key=KEY)  # type: ignore[arg-type]
    model = ModelRef(provider="gemini", id="flash")
    limited = FlashAttempt(
        model=model, attempt=1, outcome="unknown", http_status=None, retry_after_s=12.0
    )
    await accounting.after_attempt(receipt=limited)
    assert gate.cooldowns == [(KEY, "flash", 12.0)] and len(inner.after) == 1
    gate.cooling = True
    await accounting.before_attempt(model=model, attempt=1)  # the primary was checked at admission
    with pytest.raises(ProviderError) as refused:
        await accounting.before_attempt(model=model, attempt=2)
    assert refused.value.native_code == "503" and len(inner.before) == 1


async def test_every_provider_with_a_deployment_key_runs_its_turns_through_the_gate(monkeypatch):
    seen: dict[str, ProviderKey | None] = {}

    def keyed(provider: str) -> TurnRuntime:
        key = ProviderKey(provider, "digest")
        return TurnRuntime(
            transport_factory=lambda *_: None, journal=None, usage_store=None, provider_key=key
        )

    async def record(request, key, prepare):
        seen[key.provider if key else ""] = key
        raise AdmissionDenied(reason="backend_unsupported", cause="provider_busy")

    monkeypatch.setattr("daimon.core.turn.provider_gate.gated_turn", record)
    gemini = request_for(FakeGate(), profile=INLINE_REUSE.profile_id)
    gemini.deps.turn_runtimes = {INLINE_REUSE.profile_id: keyed(INLINE_REUSE.provider)}
    openai = request_for(FakeGate(), profile=PERSISTENT_WORKSPACE.profile_id)
    monkeypatch.setattr(openai_host, "runtime_for", lambda *_: keyed(PERSISTENT_WORKSPACE.provider))
    hooks = {
        INLINE_REUSE.provider: (prepare_gemini_turn, gemini),
        PERSISTENT_WORKSPACE.provider: (openai_host.prepare_openai, openai),
    }
    assert set(hooks) == set(DEFAULT_KEY_ENV), "a new keyed provider needs a gated preparation"
    for provider, (prepare, request) in hooks.items():
        with pytest.raises(AdmissionDenied):
            await prepare(request)
        assert provider in seen, f"{provider} turns skipped the gate"


@pytest.mark.parametrize(
    ("cause", "words"), [("provider_busy", "busy"), ("provider_cooling", "slow down")]
)
def test_a_gate_refusal_names_the_provider_and_what_to_do(cause: str, words: str):
    from daimon.core.turn.notices import RefusalNouns, admission_refusal_text

    text = admission_refusal_text(
        "backend_unsupported",
        RefusalNouns(scope="server", admin="a server admin", billing="/billing"),
        cause=cause,  # type: ignore[arg-type]
        provider="gemini",
    )
    assert "Gemini" in text and words in text and "Try again" in text
