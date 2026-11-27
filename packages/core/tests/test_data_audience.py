"""The data-read rule as a table: one row per path of `daimon.core.data_audience`."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.data_audience import (
    CAP,
    UNKNOWN_READ,
    DataAudience,
    DataOrigin,
    DataRefusal,
    Reader,
    audience_ref,
    audience_ref_holds,
    data_audience,
    is_audience_ref,
    scope_ceiling,
)

OWNER, ASKER, VIEWER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
asker = Reader("discord/1:user:10", ASKER)
viewer = Reader("discord/1:user:11", VIEWER)
guest = Reader("discord/1:user:12")
many = frozenset(Reader(f"discord/1:user:{n}", uuid.uuid4()) for n in range(CAP))

# (row, origin, owner, expected: refusal reason, or (public_only, list_rows, redact))
_ROWS: list[tuple[str, DataOrigin, uuid.UUID | None, str | tuple[bool, bool, bool]]] = [
    ("room", DataOrigin("room", asker, frozenset({asker, viewer})), OWNER, (False, True, False)),
    ("room, no owner", DataOrigin("room", asker, frozenset({asker})), None, (False, False, False)),
    ("asker absent", DataOrigin("room", None, frozenset({viewer})), OWNER, (True, False, True)),
    ("asker not viewer", DataOrigin("room", asker, frozenset({viewer})), OWNER, (True, False, True)),
    ("guest viewer", DataOrigin("room", asker, frozenset({asker, guest})), OWNER, (True, True, True)),
    ("guest asker", DataOrigin("private", guest), OWNER, (True, False, True)),
    ("viewers unknown", DataOrigin("room", asker, None), OWNER, (True, False, True)),
    ("viewers empty", DataOrigin("room", asker, frozenset()), OWNER, (True, False, True)),
    ("over cap", DataOrigin("room", asker, many | {asker}), OWNER, (False, False, False)),
    ("contained", DataOrigin("room", asker, frozenset({asker}), contained=True), OWNER, "contained"),
    ("held outsider", DataOrigin("private", asker, contained=True), OWNER, "contained"),
    ("dm, web, cli, key", DataOrigin("private", asker), OWNER, (False, True, False)),
    ("routine to room", DataOrigin("room", asker, frozenset({asker, viewer})), OWNER, (False, True, False)),
    ("routine, no destination", DataOrigin("private", asker), OWNER, (False, True, False)),
    ("helper from plain agent", DataOrigin("agent_request", asker, frozenset({asker})), OWNER, "agent_request"),
    (
        "helper from data agent, in a room",
        DataOrigin("agent_request", asker, frozenset({asker, viewer}), caller_reads_data=True),
        OWNER,
        (False, True, False),
    ),
    (
        "helper from data agent, asked from a DM: the asker's own access",
        DataOrigin("agent_request", asker, frozenset({asker}), caller_reads_data=True),
        OWNER,
        (False, True, False),
    ),
    ("teams channel, visitor, key with no person", DataOrigin("none"), OWNER, "no_person"),
]  # fmt: skip


@pytest.mark.parametrize(("row", "origin", "owner", "expected"), _ROWS, ids=[r[0] for r in _ROWS])
def test_data_audience_table(
    row: str, origin: DataOrigin, owner: uuid.UUID | None, expected: str | tuple[bool, ...]
) -> None:
    """Each path answers for exactly who will see it, or runs no data tool."""
    got = data_audience(origin, agent_owner=owner)
    if isinstance(expected, str):
        assert got == DataRefusal(expected), f"{row}: should refuse with {expected}, got {got}"  # pyright: ignore[reportArgumentType]
        return
    assert isinstance(got, DataAudience), f"{row}: should answer, got {got}"
    assert (got.public_only, got.list_rows, got.redact) == expected, f"{row}: {got}"
    if owner is not None:
        assert owner in got.people, f"{row}: the agent's owner must bound the answer"


def test_scope_ceiling_is_everyones_intersection_capped_by_guests() -> None:
    """Everyone's common scopes; a guest leaves only public; a person with no scopes, none."""
    scopes = {
        OWNER: frozenset({"public", "team", "sales"}),
        ASKER: frozenset({"public", "team"}),
        VIEWER: frozenset({"public", "team", "hr"}),
    }
    room = data_audience(DataOrigin("room", asker, frozenset({asker, viewer})), agent_owner=OWNER)
    with_guest = data_audience(
        DataOrigin("room", asker, frozenset({asker, guest})), agent_owner=OWNER
    )
    assert isinstance(room, DataAudience) and isinstance(with_guest, DataAudience)
    assert scope_ceiling(room, scopes) == {"public", "team"}, "intersection of every reader"
    assert scope_ceiling(with_guest, scopes) == {"public"}, "a guest caps it at public"
    assert scope_ceiling(room, {OWNER: scopes[OWNER]}) == frozenset(), "unlisted person: none"


def test_redacted_person_turns_on_redaction() -> None:
    """A person marked redacted among the readers turns read-time redaction on."""
    got = data_audience(
        DataOrigin("room", asker, frozenset({asker, viewer})),
        agent_owner=OWNER,
        redacted=frozenset({VIEWER}),
    )
    assert isinstance(got, DataAudience) and got.redact, "a redacted viewer redacts the answer"


def test_audience_refs_recall_only_in_place_and_among_the_readers() -> None:
    """A live read recalls in its place among its readers; a saver's, only to the saver alone."""
    room = DataOrigin("room", asker, frozenset({asker, viewer}), place="slack:C1")
    read = data_audience(room, agent_owner=OWNER)
    alone = data_audience(
        DataOrigin("room", asker, frozenset({asker}), place="slack:C1"), agent_owner=OWNER
    )
    elsewhere = data_audience(DataOrigin("private", asker, place="slack:D1"), agent_owner=OWNER)
    joined = data_audience(
        DataOrigin(
            "room", asker, frozenset({asker, viewer, Reader("s", uuid.uuid4())}), place="slack:C1"
        ),
        agent_owner=OWNER,
    )
    unknown = data_audience(DataOrigin("room", asker, None, place="slack:C1"), agent_owner=OWNER)
    assert isinstance(read, DataAudience) and isinstance(alone, DataAudience)
    assert isinstance(elsewhere, DataAudience) and isinstance(joined, DataAudience)
    assert isinstance(unknown, DataAudience)
    ref = audience_ref(read)
    assert is_audience_ref(ref) and audience_ref_holds(ref, read) and audience_ref_holds(ref, alone)
    assert not audience_ref_holds(ref, elsewhere), "another place never recalls it"
    assert not audience_ref_holds(ref, joined), "a newcomer was not a reader at the read"
    assert audience_ref(unknown) == UNKNOWN_READ, "unlisted readers record a read no one recalls"
