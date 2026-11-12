"""`turn.run`: a replacement's framing waits in the database until its first send."""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types.beta import BetaManagedAgentsSystemContentBlockParam
from daimon.core.pending_carry_over_sweep import sweep_stale_pending_carry_overs
from daimon.core.stores.pending_carry_overs import (
    get_pending_carry_over,
    record_pending_carry_over,
)
from daimon.core.turn import run as run_module
from daimon.core.turn.driver import SEND_ATTEMPTED, SEND_CLAIM
from daimon.core.turn.prepare import CONTINUED, ContinuityOutcome
from daimon.core.turn.state import TurnState
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SYSTEM: tuple[BetaManagedAgentsSystemContentBlockParam, ...] = (
    {"type": "text", "text": "You took over this conversation."},
)
REPLACED = ContinuityOutcome(
    state="replaced", user_prefix="EARLIER CONVERSATION\n\n", system_blocks=SYSTEM
)


@dataclass(frozen=True)
class _Prepared:
    """The fields `run_prepared_turn` reads, as a dataclass so the framing can be put back."""

    admission: Any
    continuity: ContinuityOutcome
    ma_session_id: str = "sess_new"
    watermark: str | None = None
    reused: bool = True
    mapping_id: uuid.UUID | None = None
    snapshot: None = None
    _finalize: None = None


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    sessions: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID | None,
    continuity: ContinuityOutcome,
    origin: str,
    sends: bool,
    session_id: str = "sess_new",
    fails_after_send: bool = False,
) -> ContinuityOutcome:
    """Run one turn; return the continuity the driver was handed."""
    handed: list[ContinuityOutcome] = []

    async def unchanged(*_a: Any, user_message: str, **_k: Any) -> tuple[str, None]:
        return user_message, None

    async def no_notices(
        *_a: Any, user_message: str, reseed_user_message: Any, **_k: Any
    ) -> tuple[str, Any, list[str]]:
        return user_message, reseed_user_message, []

    async def no_queue(*_a: Any, **_k: Any) -> None:
        return None

    async def driver(_deps: Any, prepared: Any, **_k: Any) -> Any:
        handed.append(prepared.continuity)
        if not sends:
            raise RuntimeError("provider unreachable")
        if (claim := SEND_CLAIM.get()) is not None:
            await claim()
        mark = SEND_ATTEMPTED.get()
        assert mark is not None, "the turn runs with a send mark"
        mark.append(True)
        if fails_after_send:
            raise RuntimeError("stream lost")
        return run_module.RunOutcome(
            state=TurnState(content=[]),
            ma_session_id=prepared.ma_session_id,
            mapping_id=None,
            recovered=False,
            continuity=prepared.continuity,
        )

    def _finish(**_k: Any) -> None:
        return None

    monkeypatch.setattr(run_module, "_with_handoff_summary", unchanged)
    monkeypatch.setattr(run_module, "_with_agent_notices", no_notices)
    monkeypatch.setattr(run_module, "_queue_agent_notices", no_queue)
    monkeypatch.setattr(run_module, "run_prepared_turn_impl", driver)
    observation = SimpleNamespace(
        activate=contextlib.nullcontext,
        finish=_finish,
        session_id=None,
        model_by_session={},
    )
    prepared = _Prepared(
        admission=SimpleNamespace(
            observation=observation,
            account_id=account_id,
            agent=SimpleNamespace(id="agt", name="daimon"),
            backend_footer_profile=None,
            features=None,
            backend_revision=None,
        ),
        continuity=continuity,
        ma_session_id=session_id,
    )
    deps = SimpleNamespace(
        sessionmaker=sessions,
        running_turns=SimpleNamespace(track=contextlib.nullcontext),
        backend_selection_enabled=False,
    )
    with contextlib.suppress(RuntimeError):
        await run_module.run_prepared_turn(
            deps,  # pyright: ignore[reportArgumentType]
            prepared,  # pyright: ignore[reportArgumentType]
            tenant_id=tenant_id,
            platform="discord",
            thread_id="T",
            external_user_id="42",
            user_message="hi",
            lifecycle=None,  # pyright: ignore[reportArgumentType]
            cancel=None,  # pyright: ignore[reportArgumentType]
            reseed_user_message=None,  # pyright: ignore[reportArgumentType]
            recovery_lifecycle=None,  # pyright: ignore[reportArgumentType]
            origin=origin,  # pyright: ignore[reportArgumentType]
        )
    (continuity_handed,) = handed
    return continuity_handed


@pytest.mark.parametrize("origin", ["chat", "routine"])
async def test_a_retry_after_an_unsent_first_message_gets_the_original_framing(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
    origin: str,
) -> None:
    """The retry reuses the session and prepares no framing; the kept one is sent as it was."""
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant) if origin == "chat" else None
    common: dict[str, Any] = {
        "tenant_id": tenant.id,
        "account_id": account.id if account is not None else None,
        "origin": origin,
    }

    first = await _turn(monkeypatch, db_session_factory, continuity=REPLACED, sends=False, **common)
    assert first.user_prefix == REPLACED.user_prefix, "the first try carries the framing"
    async with db_session_factory() as db:
        kept = await get_pending_carry_over(db, tenant_id=tenant.id, session_id="sess_new")
    assert kept is not None, "an unsent framing is kept"

    retry = await _turn(monkeypatch, db_session_factory, continuity=CONTINUED, sends=True, **common)
    assert retry.user_prefix == REPLACED.user_prefix, "the retry opens with the original framing"
    assert retry.system_blocks == SYSTEM, "and keeps its system blocks"
    async with db_session_factory() as db:
        gone = await get_pending_carry_over(db, tenant_id=tenant.id, session_id="sess_new")
    assert gone is None, "a sent framing is dropped"

    later = await _turn(monkeypatch, db_session_factory, continuity=CONTINUED, sends=True, **common)
    assert later == CONTINUED, "the turn after that is an ordinary one"


async def test_a_framing_older_than_a_week_is_swept(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session abandoned before its first send leaves a row the sweep drops."""
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        await record_pending_carry_over(
            db,
            tenant_id=tenant.id,
            session_id="sess_old",
            account_id=None,
            user_prefix="EARLIER",
            system_blocks=SYSTEM,
        )
    now = datetime.now(UTC)
    assert await sweep_stale_pending_carry_overs(db_session_factory, now=now) == 0, (
        "a fresh row stays"
    )
    swept = await sweep_stale_pending_carry_overs(db_session_factory, now=now + timedelta(days=8))
    assert swept == 1, "a week-old row goes"


async def _kept(
    sessions: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, session_id: str
) -> str | None:
    async with sessions() as db:
        row = await get_pending_carry_over(db, tenant_id=tenant_id, session_id=session_id)
    return None if row is None else row.user_prefix


async def test_a_second_replacement_before_the_first_send_keeps_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """R0 -> R1 (first send fails) -> R2: R2 opens with R0's conversation, R1's row is gone."""
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
    common: dict[str, Any] = {"tenant_id": tenant.id, "account_id": account.id, "origin": "chat"}
    to_r1 = replace(REPLACED, predecessor_session_id="sess_r0")
    await _turn(
        monkeypatch,
        db_session_factory,
        continuity=to_r1,
        sends=False,
        session_id="sess_r1",
        **common,
    )
    assert await _kept(db_session_factory, tenant.id, "sess_r1") == REPLACED.user_prefix

    to_r2 = ContinuityOutcome(
        state="replaced", user_prefix="R1 HAD NOTHING", predecessor_session_id="sess_r1"
    )
    handed = await _turn(
        monkeypatch,
        db_session_factory,
        continuity=to_r2,
        sends=False,
        session_id="sess_r2",
        **common,
    )
    merged = f"{REPLACED.user_prefix}\n\nR1 HAD NOTHING"
    assert handed.user_prefix == merged, "R0's conversation goes ahead of R2's own framing"
    assert handed.system_blocks == SYSTEM, "with R0's system blocks"
    assert await _kept(db_session_factory, tenant.id, "sess_r1") is None, "R1's row is taken over"
    assert await _kept(db_session_factory, tenant.id, "sess_r2") == merged, "and kept under R2"

    retry = await _turn(
        monkeypatch,
        db_session_factory,
        continuity=CONTINUED,
        sends=True,
        session_id="sess_r2",
        **common,
    )
    assert retry.user_prefix == merged, "R2's retry still carries R0's conversation"
    assert await _kept(db_session_factory, tenant.id, "sess_r2") is None, "sent, so dropped"


async def test_a_kept_framing_goes_ahead_of_a_retrys_own_prefix(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A retry with a notice of its own still opens with the kept framing."""
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
    common: dict[str, Any] = {"tenant_id": tenant.id, "account_id": account.id, "origin": "chat"}
    lost = replace(REPLACED, state="replaced_after_loss")
    await _turn(monkeypatch, db_session_factory, continuity=lost, sends=False, **common)
    assert await _kept(db_session_factory, tenant.id, "sess_new") == REPLACED.user_prefix, (
        "a framing after a lost workspace is kept too"
    )

    noticed = ContinuityOutcome(user_prefix="A CONNECTOR IS UNAVAILABLE")
    retry = await _turn(monkeypatch, db_session_factory, continuity=noticed, sends=True, **common)
    assert retry.user_prefix == f"{REPLACED.user_prefix}\n\nA CONNECTOR IS UNAVAILABLE"
    assert retry.system_blocks == SYSTEM, "the kept system blocks come along"
    assert await _kept(db_session_factory, tenant.id, "sess_new") is None


async def test_a_framing_is_dropped_at_the_send_even_when_the_turn_then_fails(
    monkeypatch: pytest.MonkeyPatch,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The send claims the framing: a failure after it never sends the framing twice."""
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        account = await make_account(db, tenant=tenant)
    common: dict[str, Any] = {"tenant_id": tenant.id, "account_id": account.id, "origin": "chat"}
    await _turn(
        monkeypatch,
        db_session_factory,
        continuity=REPLACED,
        sends=True,
        fails_after_send=True,
        **common,
    )
    assert await _kept(db_session_factory, tenant.id, "sess_new") is None, "claimed at the send"
    later = await _turn(monkeypatch, db_session_factory, continuity=CONTINUED, sends=True, **common)
    assert later == CONTINUED, "the next turn carries no second framing"
