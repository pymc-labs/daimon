"""Every entry point of a data read, and the guard each one goes through.

One row per path of the data read rule (`daimon.core.data_audience`): who a call answers
for, where data tools are offered, and what guards a data-reading agent. A row names a
guard found in the code, or a test of it; a path that is missing fails here.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp import data_access, server
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import (
    agent_chat,
    agent_memory,
    data,
    repo_memory,
    sessions,
    task_continuity,
)
from daimon.core.capability_tools import CAPABILITY_TOOLS
from daimon.core.channel_audience import channel_has_outsiders
from daimon.core.data_audience import (
    UNKNOWN_READ,
    DataAudience,
    DataOrigin,
    Reader,
    audience_ref,
    audience_ref_holds,
    data_audience,
    user_subject,
)
from daimon.core.data_tools import DATA_READS, DATA_TOOLS, REDACTED_DATA_RESULT, mark_data_result
from daimon.core.features import toolset
from daimon.core.features.registry import FEATURES
from daimon.core.stores.domain import Role
from daimon.core.stores.repo_memory import MemoryEntry
from daimon.core.teams_graph import GraphMember, GraphUnavailable
from daimon.core.turn import admission
from daimon.core.turn_hold import TurnHold
from daimon.data import pool, reads
from daimon.data.identity import IdentityKey
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _calls(function: Callable[..., Any]) -> set[str]:
    """The names `function` calls, read from its source."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(function))))
    return {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    }


# (path, function, guard it must call). Each is an entry point into a data-reading agent
# or a place its results could leave the conversation they were for.
_GUARDS: list[tuple[str, Callable[..., Any], str]] = [
    ("helper asks into a data-reading agent are refused", admission._admit_gates, "reads_data"),  # pyright: ignore[reportPrivateUsage]
    ("contained and held rooms offer no data tool", admission._admit_gates, "without_data_reads"),  # pyright: ignore[reportPrivateUsage]
    ("routine, agent chat and CLI sessions card writes out", toolset.place_session, "replace"),
    ("handoff into a data-reading agent", task_continuity._hand_off_task_impl, "reads_data"),  # pyright: ignore[reportPrivateUsage]
    ("agent chat start", agent_chat._start_turn_impl, "_refuse_data_agent"),  # pyright: ignore[reportPrivateUsage]
    ("agent chat out of a conversation that read data", agent_chat._refuse_data_agent, "has_read"),  # pyright: ignore[reportPrivateUsage]
    ("handoff out of a conversation that read data", task_continuity._hand_off_task_impl, "has_read"),  # pyright: ignore[reportPrivateUsage]
    ("agent chat follow-up", agent_chat._continue_turn_impl, "_refuse_data_agent"),  # pyright: ignore[reportPrivateUsage]
    ("agent chat ask starts or follows up", agent_chat._ask_impl, "_start_turn_impl"),  # pyright: ignore[reportPrivateUsage]
    ("hub and session transcripts", sessions.SessionEventOut.from_event, "redact_data_results"),
    ("every caller is resolved from signed facts", data_access.data_audience_for, "data_origin"),
    ("chat turns by their grant, never the latest origin", data_access._turn_and_parent, "matching_turn"),  # pyright: ignore[reportPrivateUsage]
    ("rooms: outsiders close them", data_access._room, "channel_has_outsiders"),  # pyright: ignore[reportPrivateUsage]
    ("Discord rooms: stored viewers", data_access._room, "read_viewers"),  # pyright: ignore[reportPrivateUsage]
    ("Slack rooms: members read per call", data_access._room, "_slack_viewers"),  # pyright: ignore[reportPrivateUsage]
    ("Teams rooms: members read per call", data_access._room, "_teams_viewers"),  # pyright: ignore[reportPrivateUsage]
    ("helper turns: the asking agent reads data here", data_access._turn_origin, "reads_data"),  # pyright: ignore[reportPrivateUsage]
    ("the data tools register in one place", server.create_mcp_app, "register_data_tools"),  # pyright: ignore[reportUnknownMemberType]
    ("a memory saved after a data read records it", repo_memory._refs, "reads_of"),  # pyright: ignore[reportPrivateUsage]
    ("a memory that recorded data shows to its audience only", repo_memory.repo_memory_impl, "_shown"),
    ("host memory of a data-reading agent is read-only", agent_memory.open_host_memory, "reads_data"),
    ("data tools hide until the workspace has a source", admission.place_features, "has_sources"),
]  # fmt: skip


@pytest.mark.parametrize(("path", "function", "guard"), _GUARDS, ids=[g[0] for g in _GUARDS])
def test_every_entry_point_calls_its_guard(
    path: str, function: Callable[..., Any], guard: str
) -> None:
    assert guard in _calls(function), f"{path}: {function.__qualname__} no longer calls {guard}"


def _tool_defs() -> dict[str, ast.AsyncFunctionDef]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(data.register_data_tools)))
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}


def _names(node: ast.AST) -> set[str]:
    return {
        c.func.id if isinstance(c.func, ast.Name) else c.func.attr
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name | ast.Attribute)
    }


def test_every_data_tool_reads_as_its_audience_and_marks_its_result() -> None:
    defs = _tool_defs()
    reading = DATA_TOOLS | {"data_my_drive"}
    assert set(defs) >= reading, "every data tool registers in register_data_tools"
    for name in reading:
        assert {"reading", "_text"} <= _names(defs[name]), f"{name} skips the audience or mark"
        if name != "data_sources":
            assert "noted" in _names(defs[name]), f"{name} leaves its reads out of memory"
    assert {"data_audience_for", "open_read"} <= _names(defs["reading"])
    assert "ledgers" in _names(defs["noted"]), "a helper's reads must land on its asker too"


def _attrs(node: ast.AST) -> set[str]:
    return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def test_what_redaction_cannot_cover_is_refused_on_every_surface() -> None:
    """While names hide, caller SQL and file bytes are refused and a query naming a hidden
    entity finds nothing (`daimon.data.reading`). The web's twin is in its data route tests."""
    mcp = _tool_defs()
    for name, guard in (
        ("data_sql", "redacted"),
        ("data_file", "redacted"),
        ("data_search", "names_hidden"),
    ):
        assert guard in _attrs(mcp[name]), f"{name} skips {guard}"
    shows = [
        c for c in ast.walk(mcp["data_sql"]) if isinstance(c, ast.Call) and "show" in _names(c)
    ]
    assert any(k.arg == "ids" for c in shows for k in c.keywords), "SQL rows choose no rules"


def test_every_model_spend_bills_the_asker() -> None:
    """Search embeds and reranks; with no meter or payer it would bill nobody."""
    calls = [
        c
        for c in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(data.register_data_tools))))
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "search"
    ]
    assert calls
    for call in calls:
        given = {k.arg: k.value for k in call.keywords}
        for key in ("meter", "payer"):
            assert key in given and not (
                isinstance(given[key], ast.Constant) and given[key].value is None
            ), f"search without a {key}"


def test_a_data_reading_agent_is_unsealed_and_guarded() -> None:
    """A data-reading agent is no relay agent: it keeps its session and memory. Its provider
    memory mounts read-only, and writes after a data read are carded."""
    assert "or data_reading" not in inspect.getsource(admission), "no seal on data agents"
    assert admission.Admission.stores_read_only.fget is not None
    assert "data_reading" in inspect.getsource(admission.Admission.stores_read_only.fget)
    assert "tool_safety" in FEATURES[DATA_READS].depends_on, "writes after a data read are carded"
    assert CAPABILITY_TOOLS[DATA_READS] == DATA_TOOLS, "they hide with data_reads"


def _auth(**fields: Any) -> AuthIdentity:
    fields.setdefault("tenant_id", uuid.uuid4())
    return AuthIdentity(account_id=uuid.uuid4(), role=Role.USER, **fields)


_NO_RUNTIME = cast(McpRuntime, None)
_PRIVATE = {"platform": "discord", "platform_user_id": "7", "external_id": "1"}
_ORIGINS: list[tuple[str, dict[str, Any], str, bool]] = [
    ("outsider's held turn", {**_PRIVATE, "hold": TurnHold("discord", "1", client=True)}, "private", True),
    ("another organisation's person", {**_PRIVATE, "foreign_home_id": "org"}, "private", True),
    ("operator token, no person", {"platform": "discord"}, "none", False),
    ("personal key, hub, CLI", _PRIVATE, "private", False),
]  # fmt: skip


@pytest.mark.parametrize(
    ("row", "fields", "kind", "contained"), _ORIGINS, ids=[o[0] for o in _ORIGINS]
)
async def test_callers_without_a_turn_resolve_from_their_credential(
    row: str, fields: dict[str, Any], kind: str, contained: bool
) -> None:
    got = await data_access.data_origin(_NO_RUNTIME, _auth(**fields), now=datetime.now(UTC))
    assert (got.kind, got.contained) == (kind, contained), row
    if not contained and kind == "private":
        assert got.asker is not None and got.asker.subject == user_subject("discord", "1", "7"), (
            "the key's maker asks"
        )


_HOME = GraphMember(user_id="A", tenant_id="T")
_DOWN = GraphUnavailable("down", status=503)
_NO_CONSENT = GraphUnavailable("forbidden", status=403)
_ROOMS: list[tuple[str, str, object, object]] = [
    ("home members", "19:c@thread.v2", _HOME, frozenset({"a"})),
    ("a guest", "19:c@thread.v2", GraphMember(user_id="b", tenant_id="T", roles=["guest"]), "outsiders"),
    ("another tenant", "19:c@thread.v2", GraphMember(user_id="b", tenant_id="X"), "outsiders"),
    ("standard channel", "19:c@thread.tacv2", "standard", frozenset({"a"})),
    ("shared channel", "19:c@thread.tacv2", "shared", "outsiders"),
    ("channel not found", "19:c@thread.tacv2", ToolError("no such channel"), "unknown"),
    ("channel lookup fails", "19:c@thread.tacv2", _DOWN, "unknown"),
    ("members unreadable", "19:c@thread.v2", _DOWN, "unknown"),
    ("no consent", "19:c@thread.v2", _NO_CONSENT, "no_consent"),
]  # fmt: skip


def _teams_graph(monkeypatch: pytest.MonkeyPatch, conversation: str, case: object) -> None:
    channel = conversation.endswith("tacv2")
    members = [_HOME] + ([case] if isinstance(case, GraphMember) and case is not _HOME else [])
    read = AsyncMock(side_effect=case) if isinstance(case, Exception) and not channel else None
    graph = SimpleNamespace(
        list_chat_members=read or AsyncMock(return_value=members),
        list_channel_members=AsyncMock(return_value=members),
    )

    async def _locate(*_: object) -> object:
        if isinstance(case, Exception):
            raise case
        return SimpleNamespace(channel_type=case, group_id="g", channel_id="c")

    monkeypatch.setattr(data_access, "graph_for", lambda _client: graph)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(data_access, "locate_channel", _locate)


@pytest.mark.parametrize(
    ("row", "conversation", "case", "want"), _ROOMS, ids=[r[0] for r in _ROOMS]
)
async def test_a_teams_room_reads_its_members_outsiders_or_why_not(
    monkeypatch: pytest.MonkeyPatch, row: str, conversation: str, case: object, want: object
) -> None:
    _teams_graph(monkeypatch, conversation, case)
    runtime = cast(McpRuntime, SimpleNamespace(teams_client=object()))
    auth = _auth(platform="teams", external_id="t")
    got = await data_access._teams_viewers(runtime, auth, conversation)  # pyright: ignore[reportPrivateUsage]
    assert got == want, row


async def test_a_teams_room_with_a_guest_holds_its_next_turns_and_unread_ones_get_public(
    monkeypatch: pytest.MonkeyPatch, committing_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with committing_sessionmaker.begin() as session:
        tenant = await make_tenant(session)
    guest = GraphMember(user_id="b", tenant_id="T", roles=["guest"])
    _teams_graph(monkeypatch, "19:c@thread.v2", guest)
    runtime = cast(
        McpRuntime, SimpleNamespace(teams_client=object(), session_factory=committing_sessionmaker)
    )
    auth = _auth(platform="teams", external_id="t", tenant_id=tenant.id)
    asker = Reader("teams:t:u", None)
    now = datetime.now(UTC)
    got = await data_access._room(runtime, auth, asker, "19:c@thread.v2", None, now=now)  # pyright: ignore[reportPrivateUsage]
    assert got.contained
    async with committing_sessionmaker() as session:
        audience = await channel_has_outsiders(
            session, tenant_id=tenant.id, platform="teams", channel_id="19:c@thread.v2"
        )
    assert audience.contained, "admission holds the room, so a data-reading session starts fresh"
    for case, notice in ((_NO_CONSENT, True), (_DOWN, False)):
        _teams_graph(monkeypatch, "19:c@thread.v2", case)
        got = await data_access._room(runtime, auth, asker, "19:d@thread.v2", None, now=now)  # pyright: ignore[reportPrivateUsage]
        assert not got.contained and got.viewers is None, "unread members cap at public data"
        assert (got.notice is not None and "consent" in got.notice) == notice, (
            "only a missing consent is named"
        )


@pytest.mark.parametrize("own_read", [False, True])
async def test_own_drive_reads_in_a_contained_room_for_the_asker_public_only(
    monkeypatch: pytest.MonkeyPatch, own_read: bool
) -> None:
    async def contained(*_: object, **__: object) -> DataOrigin:
        return DataOrigin("room", Reader("discord:u", uuid.uuid4()), None, contained=True)

    monkeypatch.setattr(data_access, "data_origin", contained)
    runtime = cast(McpRuntime, SimpleNamespace(session_factory=_Sessions()))
    auth = _auth(platform="discord", external_id="u")
    if not own_read:
        with pytest.raises(ToolError, match="outside the team"):
            await data_access.data_audience_for(runtime, auth)
        return
    got = await data_access.data_audience_for(runtime, auth, own_read=True)
    assert got.public_only and got.redact, "the room's outsiders see only what anyone may"


async def test_own_drive_read_is_remembered_by_its_audience_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The note an own-Drive read leaves is recalled in the same place while everyone present
    was a reader; a room outsiders may read leaves an unknown read, never recalled."""
    me = uuid.uuid4()
    assert "audience_ref" in _names(_tool_defs()["data_my_drive"])
    runtime = cast(McpRuntime, SimpleNamespace(session_factory=_Sessions()))
    auth = _auth(platform="discord", external_id="u")

    async def private(*_: object, **__: object) -> DataOrigin:
        return DataOrigin("private", Reader("discord:u", me), place="discord:D")

    monkeypatch.setattr(data_access, "data_origin", private)
    here = await data_access.data_audience_for(runtime, auth, own_read=True)
    assert audience_ref(here) == f"audience:{me}@discord:D"
    assert audience_ref_holds(audience_ref(here), here)
    elsewhere = replace(here, place="discord:E")
    assert not audience_ref_holds(audience_ref(here), elsewhere), "only in the same place"
    assert not audience_ref_holds(
        audience_ref(here), replace(here, people=frozenset({me, uuid.uuid4()}))
    )

    async def contained(*_: object, **__: object) -> DataOrigin:
        return DataOrigin("room", Reader("discord:u", me), None, contained=True, place="discord:R")

    monkeypatch.setattr(data_access, "data_origin", contained)
    got = await data_access.data_audience_for(runtime, auth, own_read=True)
    assert audience_ref(got) == UNKNOWN_READ


async def test_own_drive_is_refused_on_another_agents_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def asked(*_: object, **__: object) -> DataOrigin:
        return DataOrigin("agent_request", caller_reads_data=True)

    monkeypatch.setattr(data_access, "data_origin", asked)
    runtime = cast(McpRuntime, SimpleNamespace(session_factory=_Sessions()))
    with pytest.raises(ToolError):
        await data_access.data_audience_for(
            runtime, _auth(platform="discord", external_id="u"), own_read=True
        )


class _Sessions:
    def __call__(self) -> _Sessions:
        return self

    async def __aenter__(self) -> _Sessions:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


def test_a_transcript_read_elsewhere_hides_data_results() -> None:
    class _Event(BaseModel):
        id: str
        type: str
        content: list[dict[str, Any]]

    event = _Event(id="e", type="tool_result", content=[{"text": mark_data_result("rows")}])
    out = sessions.SessionEventOut.from_event(event)
    assert out.content == [{"text": REDACTED_DATA_RESULT}]


def test_readers_take_the_form_of_mirrored_platform_identities() -> None:
    mirrored = IdentityKey("discord", "1", "7").ref()
    assert user_subject("discord", "1", "7") == mirrored == "discord/1:user:7"


async def test_a_memory_that_recorded_data_shows_only_where_all_of_it_is_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Untagged memories show as before; a tagged one only where its items are readable."""
    now = datetime.now(UTC)
    tenant = uuid.uuid4()
    plain, ok, hidden = (
        MemoryEntry(tenant_id=tenant, repo_id=1, name=n, title=n, body="b", kind="reference",
                    scope="repo", created_by_account_id=None, created_at=now, updated_at=now,
                    data_refs=refs)
        for n, refs in (("plain", None), ("ok", ["record:a"]), ("hidden", ["record:b"]))
    )  # fmt: skip
    asked: list[list[list[str]]] = []

    async def readable(_runtime: object, _auth: object, ref_sets: list[list[str]]) -> list[bool]:
        asked.append(ref_sets)
        return [refs == ["record:a"] for refs in ref_sets]

    noted: list[tuple[str, list[str]]] = []

    async def note(_s: object, *, chat_session_id: str, refs: list[str], **_: object) -> None:
        noted.append((chat_session_id, refs))

    monkeypatch.setattr(repo_memory, "readable_by_audience", readable)
    monkeypatch.setattr(repo_memory, "ledgers", AsyncMock(return_value=["s", "asker"]))
    monkeypatch.setattr(repo_memory, "note_reads", note)
    runtime = cast(McpRuntime, SimpleNamespace(session_factory=SimpleNamespace(begin=_tx)))
    shown = await repo_memory._shown(runtime, _auth(), [plain, ok, hidden])  # pyright: ignore[reportPrivateUsage]
    assert [e.name for e in shown] == ["plain", "ok"]
    assert asked == [[["record:a"], ["record:b"]]], "untagged memories are not checked"
    assert noted == [("s", ["record:a"]), ("asker", ["record:a"])], "a recall joins the reads"
    noted.clear()
    await repo_memory._shown(runtime, _auth(), [ok], recall=False)  # pyright: ignore[reportPrivateUsage]
    assert not noted, "a listing joins nothing"


@asynccontextmanager
async def _tx() -> AsyncIterator[object]:
    yield object()


async def test_a_memory_recalls_each_ref_by_its_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Records are re-checked by the store; audience refs by where and by whom they were read;
    one unknown read hides the memory."""
    me = uuid.uuid4()
    here = data_audience(
        DataOrigin("private", Reader("discord/1:user:7", me), place="discord:D"), agent_owner=None
    )
    assert isinstance(here, DataAudience)
    checked: list[list[str]] = []

    async def readable(_c: object, _s: object, _ctx: object, refs: list[str], _env: object) -> bool:
        checked.append(refs)
        return refs == ["record:a"]

    @asynccontextmanager
    async def connection(_settings: object) -> AsyncIterator[object]:
        yield object()

    def anything(_: object) -> object:
        return True

    monkeypatch.setattr(data_access, "data_sources_on", anything)
    monkeypatch.setattr(data_access, "data_audience_for", AsyncMock(return_value=here))
    monkeypatch.setattr(pool, "connection", connection)
    monkeypatch.setattr(
        data_access, "open_read", AsyncMock(return_value=SimpleNamespace(entities=None, ctx=None))
    )
    monkeypatch.setattr(data_access, "Schemas", SimpleNamespace(of=anything))
    monkeypatch.setattr(data_access, "Env", SimpleNamespace(of=anything))
    monkeypatch.setattr(reads, "readable", readable)
    runtime = cast(McpRuntime, SimpleNamespace(settings=SimpleNamespace(data=None)))
    got = await data_access.readable_by_audience(runtime, _auth(), [
        ["record:a", audience_ref(here)], ["record:b"], ["record:a", UNKNOWN_READ],
    ])  # fmt: skip
    assert got == [True, False, False]
    assert ["record:a"] in checked and [] not in checked, "audience refs never reach the store"
