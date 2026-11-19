"""Retention of idle Gemini conversations: the decision table and one real sweep."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from daimon.core._models import GeminiRecord, ProviderBinding, ProviderBindingSlot, UsageObservation
from daimon.core.turn import gemini_retention
from daimon.core.turn.gemini_retention import Facts, prune_idle_records, retention, stubbed
from daimon.core.turn.gemini_state import PostgresGeminiStorage
from mux.errors import ContinuityLost
from mux.testing.drivers.gemini.fake import MemoryStorage
from sqlalchemy import delete, func, select, update

from .test_gemini_postgres_state import (  # noqa: F401  # fixture
    _gemini_session,  # pyright: ignore[reportPrivateUsage]  # shared driver setup
    postgres_state,
)

NOW = datetime(2026, 11, 19, tzinfo=UTC)
WINDOW = timedelta(days=30)
IDLE = NOW - timedelta(days=31)


@pytest.mark.parametrize(
    ("change", "named", "settled", "held", "expected"),
    [
        ({}, True, True, False, "stub"),
        ({}, False, True, False, "delete"),
        ({"pruned": True}, True, True, False, "keep"),
        ({"touched": NOW - timedelta(days=29)}, False, True, False, "keep"),
        ({"state": "running"}, False, True, False, "keep"),
        ({"state": "requires_action"}, False, True, False, "keep"),
        ({"unknown_delivery": True}, False, True, False, "keep"),
        ({"uploading": ("skill",)}, False, True, False, "keep"),
        ({}, False, False, False, "keep"),
        ({"final": False}, False, True, False, "keep"),
        ({}, False, True, True, "keep"),
    ],
)
async def test_retention_keeps_anything_open_and_stubs_only_what_a_binding_names(
    change, named, settled, held, expected
):
    storage = MemoryStorage()
    _, _, session = await _gemini_session(storage, _scope())
    async with storage.read() as records:
        record = records.sessions[session.ref.id]
    observation = next(iter(record.observations.values()))
    observation = observation.model_copy(update={"final": change.get("final", True)})
    record = replace(
        record,
        session=record.session.model_copy(update={"state": change.get("state", "idle")}),
        observations={next(iter(record.interactions)): observation},
        **{k: v for k, v in change.items() if k in ("pruned", "unknown_delivery", "uploading")},
    )
    facts = Facts(
        touched_at=change.get("touched", IDLE),
        named=named,
        settled={observation.id: observation.revision} if settled else {},
        held=frozenset(record.interactions) if held else frozenset(),
    )
    assert retention(record, facts, NOW, WINDOW) == expected


def _scope():
    from mux.contracts.ids import Scope

    return Scope(
        tenant_id="00000000-0000-0000-0000-000000000001",
        account_id="00000000-0000-0000-0000-000000000002",
        principal_id="caller",
        authorization_id="admitted",
    )


async def test_sweep_deletes_an_unbound_session_and_stubs_a_bound_one(
    postgres_state,  # noqa: F811
    monkeypatch,
):
    """The unbound session goes with its account records; the agent and environment
    stay while another session uses them; the bound one reads as lost after."""
    sessions, scope, _, _ = postgres_state
    storage = PostgresGeminiStorage(sessions, scope=scope, project="project")
    _, _, first = await _gemini_session(storage, scope, key="first")
    driver, _, second = await _gemini_session(storage, scope, key="second")
    tenant = UUID(scope.tenant_id)
    async with storage.read() as records:
        kept = [records.sessions[first.ref.id], records.sessions[second.ref.id]]
        agent, environment = kept[0].refs[:2]
    async with sessions.begin() as db:
        # Both sessions ran the fake's i1 on binding "b": one settled observation.
        for id_, revision in {(o.id, o.revision) for r in kept for o in r.observations.values()}:
            db.add(
                UsageObservation(
                    binding_id="b",
                    observation_id=id_,
                    revision=revision,
                    tenant_id=tenant,
                    applied={},
                )
            )
        await db.execute(
            update(GeminiRecord)
            .where(GeminiRecord.kind != "account", GeminiRecord.id != second.ref.id)
            .values(touched_at=func.now() - timedelta(days=40))
        )

    now = datetime.now(UTC)
    assert await prune_idle_records(sessions, now=now, window=WINDOW) == {"deleted": 1}
    async with sessions() as db:
        left = set((await db.execute(select(GeminiRecord.kind, GeminiRecord.id))).tuples())
    assert ("session", first.ref.id) not in left and ("session", second.ref.id) in left
    assert {agent, environment} <= left, "the other session still uses them"
    async with storage.read() as records:
        named = {creation[2] for creation in records.creations.values()}
    assert first.ref.id not in named and second.ref.id in named

    binding = kept[1].session.binding
    async with sessions.begin() as db:
        db.add(
            ProviderBindingSlot(
                binding_id="b",
                tenant_id=tenant,
                platform="discord",
                channel_id="c",
                thread_id="th",
                generation=binding.generation,
            )
        )
        await db.flush()
        db.add(
            ProviderBinding(
                binding_id="b",
                generation=binding.generation,
                tenant_id=tenant,
                provider="gemini",
                profile=binding.profile,
                binding=binding.model_dump(mode="json"),
            )
        )
        await db.execute(update(GeminiRecord).values(touched_at=func.now() - timedelta(days=40)))
    assert await prune_idle_records(sessions, now=now, window=WINDOW) == {"stubbed": 1}
    async with storage.read() as records:
        assert records.sessions[second.ref.id] == stubbed(kept[1])
    with pytest.raises(ContinuityLost):
        await driver.sessions.retrieve(scope, second.ref)

    async def idle() -> None:
        async with sessions.begin() as db:
            await db.execute(
                update(GeminiRecord).values(touched_at=func.now() - timedelta(days=40))
            )

    await idle()
    assert await prune_idle_records(sessions, now=now, window=WINDOW) == {"kept": 1}

    def undecodable(*_: object) -> None:
        raise AssertionError("an unchanged kept row is decoded again")

    with monkeypatch.context() as patch:
        patch.setattr(gemini_retention, "load_row", undecodable)
        assert await prune_idle_records(sessions, now=now, window=WINDOW) == {"kept": 1}
    async with sessions.begin() as db:
        await db.execute(delete(ProviderBinding))
        await db.execute(delete(ProviderBindingSlot))
    assert await prune_idle_records(sessions, now=now, window=WINDOW) == {"deleted": 1}, (
        "a kept row whose binding went is decided again"
    )
