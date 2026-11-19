"""Row contract: records split into rows, strict views, and the changes they write."""

import pytest
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import ChannelRef, ModelRef, Scope, ThreadRef
from mux.contracts.resources import AgentSpec, SessionSpec
from mux.drivers.gemini import GeminiManagedAgents
from mux.drivers.gemini.storage import (
    ACCOUNT,
    Keys,
    Records,
    RecordsScopeError,
    changes,
    records_of,
    rows_of,
    scoped,
    session_key,
    view,
)
from mux.state.memory import MemoryStateStore
from mux.testing.drivers.gemini.fake import FakeTransport, MemoryStorage

SCOPE = Scope(tenant_id="t", account_id="a", principal_id="p", authorization_id="allowed")
THREAD = ThreadRef(
    channel=ChannelRef(tenant_id="t", platform="discord", channel_id="c"), thread_id="th"
)


async def _two_sessions() -> tuple[MemoryStorage, list[str]]:
    storage = MemoryStorage()
    ma = GeminiManagedAgents(
        FakeTransport(), storage=storage, state_store=MemoryStateStore(), account_scope_id="p"
    )
    agent = await ma.agents.create(
        SCOPE,
        AgentSpec(name="a", model=ModelRef(provider="gemini", id="gemini-3.8-flash")),
        key="agent",
    )
    ids: list[str] = []
    for index in range(2):
        spec = SessionSpec(
            agent=agent.ref,
            agent_revision=agent.revision,
            config_revision=1,
            extensions={
                "gemini.session": ExtensionConfig(
                    namespace="gemini.session",
                    version=1,
                    value={"thread": THREAD.model_dump(mode="json"), "binding_id": f"b{index}"},
                )
            },
        )
        ids.append((await ma.sessions.create(SCOPE, spec, key=f"s{index}")).ref.id)
    return storage, ids


async def test_rows_of_round_trips_through_records_of() -> None:
    """Splitting a blob into rows and back loses nothing."""
    storage, ids = await _two_sessions()
    async with storage.read() as records:
        rows = rows_of(records)
        assert records_of(rows) == records, "rows must rebuild the same records"
    assert {session_key(i) for i in ids} <= set(rows), "each session is its own row"
    assert rows[session_key(ids[0])].record.refs, "a session names the rows it reads"  # type: ignore[union-attr]


async def test_view_raises_on_rows_outside_its_keys() -> None:
    """A view loads only the named rows; anything else is a scope error, not absence."""
    storage, (first, second) = await _two_sessions()
    async with storage.read() as records:
        rows = rows_of(records)
    loaded, keys = scoped(rows, Keys(lock=(session_key(first),), refs=True))
    seen = view(loaded, keys)
    assert seen.sessions[first].session.ref.id == first, "the locked session is loaded"
    assert seen.agents.get(seen.sessions[first].spec.agent.id), "refs load its agent"
    with pytest.raises(RecordsScopeError):
        seen.sessions.get(second)
    with pytest.raises(RecordsScopeError):
        list(seen.sessions.values())
    with pytest.raises(RecordsScopeError):
        seen.creations.get(("t", "a", "k"))


async def test_changes_write_only_locked_rows() -> None:
    """A changed locked row is upserted; an unchanged one is not; a loaded one may not change."""
    storage, (first, _) = await _two_sessions()
    async with storage.read() as records:
        rows = rows_of(records)
    loaded, keys = scoped(rows, Keys(lock=(session_key(first),), refs=True))
    untouched = view(loaded, keys)
    assert changes(untouched, loaded, keys).upserts == {}, "a no-op writes nothing"
    edited = view(loaded, keys)
    edited.sessions[first].unknown_delivery = True
    assert set(changes(edited, loaded, keys).upserts) == {session_key(first)}, (
        "only the edited session row is written"
    )
    agent = edited.sessions[first].spec.agent.id
    edited.agents[agent] = edited.agents[agent].model_copy(update={"created_at": None})
    with pytest.raises(RecordsScopeError):
        changes(edited, loaded, keys)
    assert ACCOUNT not in keys.lock, "the account row stays unlocked"
    assert records_of(rows) != Records(), "rows hold the setup"
