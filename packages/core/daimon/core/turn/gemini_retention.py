"""Retention of stored Gemini conversations: keep, stub or delete idle session rows.

The scheduler runs `prune_idle_records` hourly. A row idle past the window with
no open turn, unsettled usage or open hold is deleted when no live binding names
it, or stubbed (history dropped, `pruned` set) when one does, so that thread
reads as lost and starts a new conversation.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal, cast

from daimon.core.stores import gemini_retention as store
from daimon.core.stores.gemini_retention import Owner, RowId, StoredRow
from daimon.core.turn.gemini_state import dump_row, held_interactions, load_row
from mux.contracts.resources import Agent
from mux.drivers.gemini import CATALOG_MODE
from mux.drivers.gemini.storage import ACCOUNT, AccountRow, SessionRecord, SessionRow
from mux.state.store import SESSION_REF, is_released
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Decision = Literal["keep", "stub", "delete"]
BATCH = 200


@dataclass(frozen=True)
class Facts:
    touched_at: datetime
    named: bool
    """A live binding (current generation, not released) names the session."""
    settled: Mapping[str, int]
    """Observation id -> highest revision applied with nothing pending."""
    held: frozenset[str]
    """Interaction ids an open hold names."""


def retention(record: SessionRecord, facts: Facts, now: datetime, window: timedelta) -> Decision:
    session = record.session
    if (
        now - facts.touched_at < window
        or session.state in ("running", "requires_action")
        or session.active_root_turn is not None
        or record.unknown_delivery
        or record.uploading
        or any(
            not o.final or facts.settled.get(o.id, 0) < o.revision
            for o in record.observations.values()
        )
        or not facts.held.isdisjoint(record.interactions)
    ):
        return "keep"
    if not facts.named:
        return "delete"
    return "keep" if record.pruned else "stub"


def stubbed(record: SessionRecord) -> SessionRecord:
    """The record with its conversation dropped; `_record` reads it as lost."""
    return replace(
        record,
        request={},
        current=None,
        interactions={},
        events={},
        observations={},
        interaction_models={},
        native_updated={},
        observation_history={},
        mount_echo_digests={},
        sends={},
        pruned=True,
    )


def _forget(account: AccountRow, ids: set[str]) -> AccountRow:
    """The account's operation records without those naming `ids`."""
    return AccountRow(
        creations={k: v for k, v in account.creations.items() if v[2] not in ids},
        deletions={
            k: v
            for k, v in account.deletions.items()
            if ids.isdisjoint(
                ref.id for ref in (*v[2].deleted, *v[2].retained, *v[2].pending, *v[2].failed)
            )
        },
    )


def _catalog(row: StoredRow) -> bool:
    if row.kind != "agent":
        return False
    native = cast(Agent, load_row("agent", row.body)).native
    return isinstance(native, dict) and native.get("resource_mode") == CATALOG_MODE


async def _unshared(
    sessions: async_sessionmaker[AsyncSession],
    owner: Owner,
    keys: list[RowId],
    *,
    besides: str | None,
    cutoff: datetime,
) -> list[StoredRow]:
    """Agent and environment rows idle since `cutoff` that no other session or catalog uses."""
    found = await store.rows(sessions, owner, keys)
    used = await store.referenced(sessions, owner, [f"{k}:{i}" for k, i in found], besides=besides)
    return [
        row
        for row in found.values()
        if f"{row.kind}:{row.id}" not in used and row.touched_at < cutoff and not _catalog(row)
    ]


async def _drop(
    sessions: async_sessionmaker[AsyncSession],
    owner: Owner,
    doomed: list[StoredRow],
    *,
    writes: dict[RowId, str],
    expect: dict[RowId, int],
) -> bool:
    ids = {row.id for row in doomed}
    expect |= {(row.kind, row.id): row.version for row in doomed}
    account = (await store.rows(sessions, owner, [ACCOUNT])).get(ACCOUNT) if ids else None
    if account is not None:
        before = cast(AccountRow, load_row("account", account.body))
        if (after := _forget(before, ids)) != before:
            writes[ACCOUNT] = dump_row("account", after)
            expect[ACCOUNT] = account.version
    return await store.prune(
        sessions, owner, expect=expect, writes=writes, deletes=[(r.kind, r.id) for r in doomed]
    )


async def _session(
    sessions: async_sessionmaker[AsyncSession],
    row: StoredRow,
    held: frozenset[str],
    *,
    now: datetime,
    window: timedelta,
) -> str:
    record = cast(SessionRow, load_row("session", row.body)).record
    facts = await store.session_facts(sessions, row.owner, record.session.binding.id)
    binding = facts.binding
    named = (
        binding is not None
        and not is_released(binding)
        and binding.native_refs.get(SESSION_REF) == record.session.ref.id
    )
    decision = retention(record, Facts(row.touched_at, named, facts.settled, held), now, window)
    if decision == "keep":
        return "kept"
    # Seam: Gemini offers no delete for stored interactions. Once the driver has
    # one, delete `record.interactions` provider-side here, before their ids go.
    doomed = await store.session_artifacts(sessions, row.owner, row.id)
    writes: dict[RowId, str] = {}
    if decision == "stub":
        writes[("session", row.id)] = dump_row("session", SessionRow(record=stubbed(record)))
    else:
        refs = [cast(RowId, tuple(r.split(":", 1))) for r in row.refs]
        doomed += [
            row,
            *await _unshared(
                sessions,
                row.owner,
                [r for r in refs if r[0] in ("agent", "environment")],
                besides=row.id,
                cutoff=now - window,
            ),
        ]
    done = await _drop(
        sessions, row.owner, doomed, writes=writes, expect={("session", row.id): row.version}
    )
    return ("stubbed" if decision == "stub" else "deleted") if done else "busy"


async def prune_idle_records(
    sessions: async_sessionmaker[AsyncSession], *, now: datetime, window: timedelta
) -> dict[str, int]:
    """One sweep over every account; counts by outcome. Another runner: nothing."""
    cutoff = now - window
    counts: Counter[str] = Counter()
    async with store.single_runner(sessions) as mine:
        if not mine:
            return {}
        holds: dict[Owner, frozenset[str]] = {}
        after = None
        while page := await store.stale_rows(
            sessions, kinds=("session",), cutoff=cutoff, after=after, limit=BATCH
        ):
            for row in page:
                if row.owner not in holds:
                    holds[row.owner] = held_interactions(await store.holds(sessions, row.owner))
                counts[await _session(sessions, row, holds[row.owner], now=now, window=window)] += 1
            after = page[-1].key
        after = None
        while page := await store.stale_rows(
            sessions, kinds=("agent", "environment"), cutoff=cutoff, after=after, limit=BATCH
        ):
            for row in page:
                orphan = await _unshared(
                    sessions, row.owner, [(row.kind, row.id)], besides=None, cutoff=cutoff
                )
                if orphan and await _drop(sessions, row.owner, orphan, writes={}, expect={}):
                    counts["orphans"] += 1
            after = page[-1].key
    return dict(counts)
