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
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from daimon.adapters.mcp import data_access, server
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware.host_call_gate import HostCallGate
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import agent_chat, data, repo_memory, sessions, task_continuity
from daimon.core.capability_tools import CAPABILITY_TOOLS
from daimon.core.data_audience import user_subject
from daimon.core.data_tools import DATA_READS, DATA_TOOLS, REDACTED_DATA_RESULT, mark_data_result
from daimon.core.features import toolset
from daimon.core.features.registry import FEATURES
from daimon.core.stores.domain import Role
from daimon.core.stores.repo_memory import MemoryEntry
from daimon.core.turn import admission
from daimon.core.turn_hold import TurnHold
from daimon.data.identity import IdentityKey
from pydantic import BaseModel


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
    ("agent chat follow-up", agent_chat._continue_turn_impl, "_refuse_data_agent"),  # pyright: ignore[reportPrivateUsage]
    ("agent chat ask starts or follows up", agent_chat._ask_impl, "_start_turn_impl"),  # pyright: ignore[reportPrivateUsage]
    ("hub and session transcripts", sessions.SessionEventOut.from_event, "redact_data_results"),
    ("every caller is resolved from signed facts", data_access.data_audience_for, "data_origin"),
    ("chat turns by their grant, never the latest origin", data_access._turn_origin, "matching_turn"),  # pyright: ignore[reportPrivateUsage]
    ("rooms: outsiders close them", data_access._room, "channel_has_outsiders"),  # pyright: ignore[reportPrivateUsage]
    ("Discord rooms: stored viewers", data_access._room, "read_viewers"),  # pyright: ignore[reportPrivateUsage]
    ("Slack rooms: members read per call", data_access._room, "_slack_viewers"),  # pyright: ignore[reportPrivateUsage]
    ("Teams rooms: members read per call", data_access._room, "_teams_viewers"),  # pyright: ignore[reportPrivateUsage]
    ("helper turns: the asking agent reads data here", data_access._turn_origin, "reads_data"),  # pyright: ignore[reportPrivateUsage]
    ("the data tools register in one place", server.create_mcp_app, "register_data_tools"),  # pyright: ignore[reportUnknownMemberType]
    ("shared connection calls after a data read", HostCallGate.ask, "_read_data"),
    ("a memory saved after a data read records it", repo_memory.repo_memory_impl, "reads_of"),
    ("a memory that recorded data shows to its audience only", repo_memory.repo_memory_impl, "_shown"),
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
    assert set(defs) >= DATA_TOOLS, "every data tool registers in register_data_tools"
    for name in DATA_TOOLS:
        assert {"reading", "_text"} <= _names(defs[name]), f"{name} skips the audience or mark"
        if name != "data_sources":
            assert "noted" in _names(defs[name]), f"{name} leaves its reads out of memory"
    assert {"data_audience_for", "open_read"} <= _names(defs["reading"])


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
    assert CAPABILITY_TOOLS[DATA_READS] == DATA_TOOLS, "the data tools hide with data_reads"


def _auth(**fields: Any) -> AuthIdentity:
    return AuthIdentity(account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER, **fields)


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

    monkeypatch.setattr(repo_memory, "readable_by_audience", readable)
    shown = await repo_memory._shown(_NO_RUNTIME, _auth(), [plain, ok, hidden])  # pyright: ignore[reportPrivateUsage]
    assert [e.name for e in shown] == ["plain", "ok"]
    assert asked == [[["record:a"], ["record:b"]]], "untagged memories are not checked"
