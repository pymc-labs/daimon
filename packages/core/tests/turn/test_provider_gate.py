"""A provider key's gate on a turn: cooldowns, caps and what every provider runs through."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from daimon.core.pricing import ProviderPrice
from daimon.core.provider_key_gate import ProviderKey
from daimon.core.turn.daily_model_cap import CappedFlashAccounting
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.provider_gate import CooldownTransport, gated_turn
from mux.contracts.errors import ProviderError
from mux.contracts.ids import ModelRef
from mux.drivers.gemini import FlashAttempt

TENANT = uuid.uuid4()
KEY = ProviderKey("openai", "digest")


class FakeGate:
    def __init__(self, *, cooling: bool = False, full: bool = False) -> None:
        self.cooling, self.full = cooling, full
        self.leases: set[uuid.UUID] = set()
        self.renewed: list[uuid.UUID] = []
        self.cooldowns: list[tuple[ProviderKey, str, float]] = []

    async def cooling_until(self, key: ProviderKey, model: str) -> datetime | None:
        return datetime.now(UTC) + timedelta(seconds=30) if self.cooling else None

    async def acquire(self, key: ProviderKey, tenant_id: uuid.UUID) -> uuid.UUID | None:
        if self.full:
            return None
        lease = uuid.uuid4()
        self.leases.add(lease)
        return lease

    async def renew(self, lease: uuid.UUID) -> None:
        self.renewed.append(lease)

    async def release(self, lease: uuid.UUID) -> None:
        self.leases.discard(lease)

    async def record_cooldown(self, key: ProviderKey, model: str, wait_s: float) -> None:
        self.cooldowns.append((key, model, wait_s))


@dataclass(frozen=True)
class Prepared:
    _finalize: object = None


@dataclass(frozen=True)
class Request:
    deps: SimpleNamespace
    admission: SimpleNamespace
    tenant_id: uuid.UUID = TENANT
    gate: object = None
    deadline: datetime = datetime.now(UTC) + timedelta(minutes=45)

    def now(self) -> datetime:
        return datetime.now(UTC)


def request_for(gate: FakeGate | None, *, profile: str = "p", model: str | None = "m"):
    revision = SimpleNamespace(profile=profile, model=model, backend="openai")
    return Request(
        deps=SimpleNamespace(
            provider_gate=gate, provider_ceiling=None, turn_runtimes={}, sessionmaker=None
        ),
        admission=SimpleNamespace(backend_revision=revision, resolved=lambda name: None),
    )


async def run(request, source, work):
    """Prepare as a provider hook does: enter the gate after its checks, then work."""

    async def prepare(gated):
        await gated.gate.enter(source)
        return await work()

    return await gated_turn(request, prepare)


async def test_a_cooling_key_refuses_before_any_preparation_or_provider_call():
    gate, called = FakeGate(cooling=True), []

    async def prepare():
        called.append("prepared")

    with pytest.raises(AdmissionDenied) as refused:
        await run(request_for(gate), lambda: KEY, prepare)
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
        await run(request_for(FakeGate(full=True)), lambda: KEY, prepare)
    assert refused.value.cause == "provider_busy" and not called


async def test_the_lease_is_held_until_the_turn_ends_and_freed_when_preparation_fails():
    gate, ended = FakeGate(), []

    async def end() -> None:
        ended.append("ended")

    async def prepare():
        return Prepared(end)

    prepared = await run(request_for(gate), lambda: KEY, prepare)
    assert len(gate.leases) == 1, "held while the turn runs"
    await prepared._finalize()
    assert ended == ["ended"] and not gate.leases, (
        "the turn's own cleanup runs, then the lease frees"
    )

    async def fail():
        raise RuntimeError("preparation failed")

    with pytest.raises(RuntimeError):
        await run(request_for(gate), lambda: KEY, fail)
    assert not gate.leases


async def test_a_lease_is_renewed_and_freed_when_its_task_ends_without_running(monkeypatch):
    """An adapter that fails between preparation and the run never calls the finalizer."""
    from daimon.core.turn import provider_gate

    monkeypatch.setattr(provider_gate, "RENEW_EVERY_S", 0.01)
    gate = FakeGate()

    async def prepare():
        return Prepared()

    async def adapter_turn():
        prepared = await run(request_for(gate), lambda: KEY, prepare)
        assert prepared._finalize is not None and len(gate.leases) == 1
        while not gate.renewed:
            await asyncio.sleep(0.01)
        raise RuntimeError("card post failed")

    with pytest.raises(RuntimeError):
        await asyncio.create_task(adapter_turn())
    async with asyncio.timeout(1):
        while gate.leases:
            await asyncio.sleep(0.01)
    assert gate.renewed and not gate.leases, "renewed while held, freed once its task ended"


@pytest.mark.parametrize("gated", [False, True])
async def test_a_turn_with_no_gate_or_no_key_runs_ungated(gated: bool):
    async def prepare():
        return Prepared()

    gate = FakeGate(full=True) if gated else None
    source = (lambda: None) if gated else (lambda: KEY)
    assert isinstance(await run(request_for(gate), source, prepare), Prepared)


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
    model = ModelRef(provider="gemini", id="flash")
    price = ProviderPrice("gemini", "flash", datetime.now(UTC).date(), *[Decimal(1)] * 3)
    accounting = CappedFlashAccounting(inner, request, {"flash": price}, None, key=lambda: KEY)  # type: ignore[arg-type]
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
