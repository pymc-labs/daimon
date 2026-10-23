"""Gemini cap holds are released: settled to measured cost, or expired after a TTL."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.core._models import GeminiRuntimeState
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.gemini_prepare import _settle_hold  # pyright: ignore[reportPrivateUsage]
from daimon.core.turn.gemini_state import (
    HOLD_TTL,
    PostgresFlashAccounting,
    SQLiteFlashAccounting,
    interaction_of,
    outstanding_holds,
)
from mux.contracts.ids import ModelRef, ResourceRef
from mux.contracts.usage import UsageObservation
from mux.drivers.gemini.flash import FLASH_PRIMARY, FlashAttempt
from mux.profiles.gemini import ARGON_MODEL
from sqlalchemy import select, update
from structlog.testing import capture_logs

from .test_gemini_postgres_state import postgres_state as postgres_state

START = datetime(2026, 10, 23, 1, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def holds(sessions, scope, clock=None, *, hold="0.10", cap="0.49"):
    return PostgresFlashAccounting(
        sessions,
        scope=scope,
        project="project",
        hold_usd=Decimal(hold),
        cap_usd=Decimal(cap),
        now=clock or Clock(),
    )


async def ledger(sessions) -> dict:
    async with sessions() as db:
        (body,) = (await db.scalars(select(GeminiRuntimeState.body))).all()
    return json.loads(body)


async def accepted(owner, model: ModelRef, interaction: str) -> None:
    await owner.before_attempt(model=model, attempt=1)
    await owner.after_attempt(
        receipt=FlashAttempt(model, 1, "accepted", interaction_id=interaction)
    )


@pytest.mark.parametrize("model_id", [FLASH_PRIMARY, ARGON_MODEL])
async def test_settled_attempts_never_exhaust_the_cap(postgres_state, model_id):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=model_id)
    # floor(0.49 / 0.10) = 4 concurrent holds; 12 settled attempts all run.
    for index in range(12):
        owner = holds(sessions, scope)
        await accepted(owner, model, f"i{index}")
        assert await owner.settle(
            interaction_id=f"i{index}", model=model_id, actual_usd=Decimal("0.013")
        )
    rows = (await ledger(sessions)).values()
    assert len(rows) == 12
    assert all(row["held_usd"] == "0" and row["settled_usd"] == "0.10" for row in rows)
    assert all(row["actual_usd"] == "0.013" for row in rows)
    summary = outstanding_holds(json.dumps(await ledger(sessions)))
    assert summary["held_usd"] == "0" and summary["open_holds"] == 0


async def test_unsettled_holds_still_stop_at_the_cap(postgres_state):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    for index in range(4):
        await accepted(holds(sessions, scope), model, f"i{index}")
    with pytest.raises(AdmissionDenied, match="provider_hold_cap_reached"):
        await holds(sessions, scope).before_attempt(model=model, attempt=1)


async def test_settlement_is_idempotent_and_never_double_counts(postgres_state):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=ARGON_MODEL)
    owner = holds(sessions, scope)
    await accepted(owner, model, "i1")
    # A cumulative revision of the same interaction replaces the actual cost only.
    for actual in ("0.02", "0.03", "0.03"):
        assert await owner.settle(
            interaction_id="i1", model=ARGON_MODEL, actual_usd=Decimal(actual)
        )
    (row,) = (await ledger(sessions)).values()
    assert (row["held_usd"], row["settled_usd"], row["actual_usd"]) == ("0", "0.10", "0.03")
    # No such accepted attempt in this ledger: nothing changes.
    assert not await owner.settle(interaction_id="other", model=ARGON_MODEL, actual_usd=Decimal(1))
    with pytest.raises(ValueError, match="model"):
        await owner.settle(interaction_id="i1", model=FLASH_PRIMARY, actual_usd=Decimal(1))
    with pytest.raises(ValueError, match="measured cost"):
        await owner.settle(interaction_id="i1", model=ARGON_MODEL, actual_usd=Decimal(-1))


async def test_concurrent_settlements_release_one_hold_once(postgres_state):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    await accepted(holds(sessions, scope), model, "i1")
    results = await asyncio.gather(
        *(
            holds(sessions, scope).settle(
                interaction_id="i1", model=FLASH_PRIMARY, actual_usd=Decimal("0.01")
            )
            for _ in range(4)
        )
    )
    assert all(results)
    (row,) = (await ledger(sessions)).values()
    assert (row["held_usd"], row["settled_usd"]) == ("0", "0.10")


@pytest.mark.parametrize("outcome", ["unknown", "accepted", "refused"])
async def test_an_unsettled_hold_expires_after_the_ttl_with_an_audit_log(postgres_state, outcome):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    clock = Clock()
    owner = holds(sessions, scope, clock, hold="0.49")
    await owner.before_attempt(model=model, attempt=1)
    # A lost response (unknown), an accepted turn whose usage never came, or a 503.
    status = 503 if outcome == "refused" else None
    await owner.after_attempt(receipt=FlashAttempt(model, 1, outcome, http_status=status))
    clock.now = START + HOLD_TTL - timedelta(seconds=1)
    with pytest.raises(AdmissionDenied, match="provider_hold_cap_reached"):
        await holds(sessions, scope, clock, hold="0.49").before_attempt(model=model, attempt=1)
    clock.now = START + HOLD_TTL
    with capture_logs() as logs:
        await holds(sessions, scope, clock, hold="0.49").before_attempt(model=model, attempt=1)
    expired = [row for row in (await ledger(sessions)).values() if "expired_usd" in row]
    assert len(expired) == 1 and expired[0]["held_usd"] == "0"
    assert expired[0]["expired_usd"] == "0.49" and expired[0]["outcome"] == outcome
    (audit,) = [entry for entry in logs if entry["event"] == "provider_hold_expired"]
    assert audit["expired_usd"] == "0.49" and audit["outcome"] == outcome


async def test_a_4xx_refusal_still_releases_at_once(postgres_state):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=ARGON_MODEL)
    owner = holds(sessions, scope, hold="0.49")
    await owner.before_attempt(model=model, attempt=1)
    await owner.after_attempt(receipt=FlashAttempt(model, 1, "refused", http_status=400))
    await holds(sessions, scope, hold="0.49").before_attempt(model=model, attempt=1)


async def test_a_legacy_unstamped_hold_starts_its_clock_at_the_upgrade(postgres_state):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    clock = Clock()
    await holds(sessions, scope, clock, hold="0.49").before_attempt(model=model, attempt=1)
    rows = await ledger(sessions)
    for row in rows.values():
        del row["held_at"]
    async with sessions.begin() as db:
        await db.execute(update(GeminiRuntimeState).values(body=json.dumps(rows)))
    clock.now = START + timedelta(days=3)
    # First sight stamps it now; it does not expire on the spot.
    with pytest.raises(AdmissionDenied, match="provider_hold_cap_reached"):
        await holds(sessions, scope, clock, hold="0.49").before_attempt(model=model, attempt=1)
    (row,) = (await ledger(sessions)).values()
    assert row["held_at"] == clock.now.isoformat()
    clock.now += HOLD_TTL
    await holds(sessions, scope, clock, hold="0.49").before_attempt(model=model, attempt=1)


def test_outstanding_holds_report_reads_without_writing():
    body = json.dumps(
        {
            "a": {"held_usd": "0.10", "outcome": "unknown", "held_at": "2026-10-22T01:00:00+00:00"},
            "b": {
                "held_usd": "0.10",
                "outcome": "accepted",
                "held_at": "2026-10-21T01:00:00+00:00",
            },
            "c": {"held_usd": "0", "outcome": "accepted", "held_at": "2026-10-20T01:00:00+00:00"},
        }
    )
    assert outstanding_holds(body) == {
        "held_usd": "0.20",
        "open_holds": 2,
        "open_by_outcome": {"accepted": 1, "unknown": 1},
        "oldest_open_held_at": "2026-10-21T01:00:00+00:00",
        "rows": 3,
    }
    assert outstanding_holds(None)["held_usd"] == "0"


async def test_sqlite_ledger_settles_and_expires_the_same_way(tmp_path):
    clock = Clock()
    path = tmp_path / "holds.sqlite3"

    def owner():
        return SQLiteFlashAccounting(
            path, namespace="n", hold_usd=Decimal("0.49"), cap_usd=Decimal("0.49"), now=clock
        )

    model = ModelRef(provider="gemini", id=ARGON_MODEL)
    first = owner()
    await accepted(first, model, "i1")
    assert await first.settle(interaction_id="i1", model=ARGON_MODEL, actual_usd=Decimal("0.2"))
    second = owner()
    await second.before_attempt(model=model, attempt=1)
    await second.after_attempt(receipt=FlashAttempt(model, 1, "unknown"))
    with pytest.raises(AdmissionDenied):
        await owner().before_attempt(model=model, attempt=1)
    clock.now += HOLD_TTL
    await owner().before_attempt(model=model, attempt=1)


def usage(interaction: str, model_id: str, **changes) -> UsageObservation:
    fields = dict(
        id=f"gemini:{interaction}:usage",
        revision=1,
        session=ResourceRef(
            provider="gemini", kind="session", id="s", tenant_id="t", account_scope_id="a"
        ),
        model=ModelRef(provider="gemini", id=model_id),
        grain="turn",
        basis="cumulative",
        observed_at=START,
        completeness="measured",
        native_meter={},
        input_tokens=10_000,
        input_cached_tokens=0,
        output_tokens=1_000,
        final=True,
    )
    return UsageObservation.model_validate(fields | changes)


class Settling:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, Decimal]] = []
        self.error = error

    async def settle(self, *, interaction_id: str, model: str, actual_usd: Decimal) -> bool:
        self.calls.append((interaction_id, model, actual_usd))
        if self.error is not None:
            raise self.error
        return True


async def test_measured_usage_settles_at_the_models_token_cost(monkeypatch):
    from daimon.core import pricing

    class InArgonWindow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 30, tzinfo=UTC)

    monkeypatch.setattr(pricing, "datetime", InArgonWindow)
    prices = pricing.provider_prices("gemini.inline_reuse")
    argon = prices[("gemini", ARGON_MODEL)]
    assert interaction_of(usage("abc", ARGON_MODEL)) == "abc"
    accounting = Settling()
    await _settle_hold(accounting, usage("abc", ARGON_MODEL), argon)
    # 10k input at $2/M + 1k output at $10/M.
    assert accounting.calls == [("abc", ARGON_MODEL, Decimal("0.03"))]
    # A running snapshot never frees the hold, even with a measured meter.
    await _settle_hold(accounting, usage("r", ARGON_MODEL, final=False), argon)
    # Partial meters, unpriced models and foreign ids leave the hold for the TTL.
    await _settle_hold(accounting, usage("p", ARGON_MODEL, completeness="partial"), argon)
    await _settle_hold(accounting, usage("u", ARGON_MODEL), None)
    await _settle_hold(accounting, usage("x", ARGON_MODEL, id="other:x"), argon)
    assert len(accounting.calls) == 1
    # A settlement failure never fails the billed turn.
    with capture_logs() as logs:
        await _settle_hold(Settling(RuntimeError("busy")), usage("abc", ARGON_MODEL), argon)
    assert [entry["event"] for entry in logs] == ["provider_hold_settle_failed"]


async def test_staging_report_reads_outstanding_holds_without_writing(
    postgres_state, db_committing_engine
):
    import importlib.util
    from pathlib import Path

    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    await accepted(holds(sessions, scope), model, "i1")
    before = await ledger(sessions)
    path = Path(__file__).parents[4] / "scripts" / "gemini_hold_report.py"
    spec = importlib.util.spec_from_file_location("gemini_hold_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with db_committing_engine.connect() as connection:
        (line,) = await module.read_holds(connection, hold=Decimal("0.10"), cap=Decimal("0.49"))
    assert (line["project"], line["held_usd"], line["open_holds"]) == ("project", "0.10", 1)
    assert line["attempts_left"] == 3 and line["open_by_outcome"] == {"accepted": 1}
    assert await ledger(sessions) == before
    # The report's transaction is READ ONLY at the database: a write is rejected.
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async with db_committing_engine.connect() as connection:
        with pytest.raises(DBAPIError, match="read-only transaction"):
            async with module.read_only(connection):
                await connection.execute(text("UPDATE gemini_runtime_state SET body = '{}'"))
    assert await ledger(sessions) == before


def test_report_never_echoes_an_unparseable_database_url(monkeypatch, capsys):
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).parents[4] / "scripts" / "gemini_hold_report.py"
    spec = importlib.util.spec_from_file_location("gemini_hold_report", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    secret = "not a url hunter2-secret"
    monkeypatch.setattr(sys, "argv", ["gemini_hold_report.py", "--database-url", secret])
    with pytest.raises(SystemExit):
        module.main()
    captured = capsys.readouterr()
    assert "not a valid database URL" in captured.err
    assert "hunter2" not in captured.err + captured.out


async def test_a_cancelled_unbilled_interaction_releases_its_hold_at_once(postgres_state):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    # cap 0.10 = one hold: the leaked hold of a never-started run blocks the next.
    owner = holds(sessions, scope, cap="0.10")
    await accepted(owner, model, "never-ran")
    with pytest.raises(AdmissionDenied, match="provider_hold_cap_reached"):
        await holds(sessions, scope, cap="0.10").before_attempt(model=model, attempt=1)
    assert await owner.release_unbilled(interaction_id="never-ran")
    assert not await owner.release_unbilled(interaction_id="never-ran")
    (row,) = (await ledger(sessions)).values()
    assert row["held_usd"] == "0" and row["released_usd"] == "0.10"
    await accepted(holds(sessions, scope, cap="0.10"), model, "next")


async def test_release_never_touches_a_measured_cost_and_a_late_reading_still_settles(
    postgres_state,
):
    sessions, scope, _, _ = postgres_state
    model = ModelRef(provider="gemini", id=FLASH_PRIMARY)
    owner = holds(sessions, scope)
    await accepted(owner, model, "billed")
    await owner.settle(interaction_id="billed", model=FLASH_PRIMARY, actual_usd=Decimal("0.02"))
    assert not await owner.release_unbilled(interaction_id="billed")
    await accepted(holds(sessions, scope), model, "late")
    assert await owner.release_unbilled(interaction_id="late")
    assert await owner.settle(
        interaction_id="late", model=FLASH_PRIMARY, actual_usd=Decimal("0.004")
    )
    rows = {row["interaction_id"]: row for row in (await ledger(sessions)).values()}
    assert rows["billed"]["actual_usd"] == "0.02" and "released_usd" not in rows["billed"]
    assert rows["late"]["actual_usd"] == "0.004" and rows["late"]["held_usd"] == "0"
    assert not await owner.release_unbilled(interaction_id="unknown")
