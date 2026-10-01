"""Stateful Managed Agents SESSIONS fake for daimon test suites.

Covers the session lifecycle (create/retrieve/update/archive/delete/list),
session resources (file/github_repository/memory_store add/list/delete/
update), the session events stream (send/list/stream), and the Files API
routes sessions depend on (list/upload/download/delete). All response
payloads are built with real SDK models and serialized with
`.model_dump(mode="json")` -- see `guideline:testing`'s validated-
construction rule.

Usage pattern (compose with the agent and memory-store fakes -- the agent
fake MUST be last, since `make_fake_ma_handler` never raises `NotHandled`;
unmatched requests fall through to its own 404, which would otherwise
swallow requests meant for this handler):

    from daimon.testing.ma import (
        FakeMAState,
        build_fake_anthropic,
        combine_handlers,
        make_fake_ma_handler,
        make_fake_memory_store_handler,
    )
    from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler

    ma_state = FakeMAState()
    state = FakeSessionsState(ma=ma_state)
    client = build_fake_anthropic(
        combine_handlers(
            make_fake_sessions_handler(state),
            make_fake_memory_store_handler(),
            make_fake_ma_handler(ma_state),
        )
    )

Live SDK facts this module encodes (verified against the installed
anthropic 0.117.0 SDK and its `httpx.MockTransport` wire format -- see the
fake-sessions design notes for exact file:line citations):
  - List-valued query params are sent as repeated `name[]=...` pairs, not
    bare `name=...` (`sessions.list(statuses=[...])` -> `statuses[]=`,
    `events.list(types=[...])` -> `types[]=`).
  - `events.list`'s `created_at_gte` etc. kwargs serialize to the query
    params `created_at[gte]` etc. (bracket-suffixed, not underscored).
  - `files.upload` sends a standard RFC 2046 multipart/form-data body: one
    part named "file", `Content-Disposition: form-data; name="file";
    filename="..."`, a `Content-Type` header, CRLF-delimited.
  - `sessions.list` / `sessions.resources.list` / `sessions.events.list`
    all use the `{data, next_page}` cursor-page envelope; `files.list` uses
    the differently-shaped `{data, has_more, first_id, last_id}` envelope.
  - `sessions.resources.add`'s `type` kwarg is typed `Literal["file"]` in
    the SDK itself -- a non-file resource can only be exercised here via a
    typed bypass (`cast(Any, ...)`), never through ordinary typed SDK usage.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

import httpx
from anthropic import BaseModel
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.beta_file_scope import BetaFileScope
from anthropic.types.beta.beta_managed_agents_deleted_session import BetaManagedAgentsDeletedSession
from anthropic.types.beta.beta_managed_agents_model_config import BetaManagedAgentsModelConfig
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.beta_managed_agents_system_message_event import (
    BetaManagedAgentsSystemMessageEvent,
)
from anthropic.types.beta.beta_managed_agents_user_tool_result_event import (
    BetaManagedAgentsUserToolResultEvent,
)
from anthropic.types.beta.deleted_file import DeletedFile
from anthropic.types.beta.file_metadata import FileMetadata
from anthropic.types.beta.sessions.beta_managed_agents_delete_session_resource import (
    BetaManagedAgentsDeleteSessionResource,
)
from anthropic.types.beta.sessions.beta_managed_agents_file_resource import (
    BetaManagedAgentsFileResource,
)
from anthropic.types.beta.sessions.beta_managed_agents_github_repository_resource import (
    BetaManagedAgentsGitHubRepositoryResource,
)
from anthropic.types.beta.sessions.beta_managed_agents_memory_store_resource import (
    BetaManagedAgentsMemoryStoreResource,
)
from anthropic.types.beta.sessions.beta_managed_agents_send_session_events import (
    BetaManagedAgentsSendSessionEvents,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_end_turn import (
    BetaManagedAgentsSessionEndTurn,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_resource import (
    BetaManagedAgentsSessionResource,
)
from anthropic.types.beta.sessions.beta_managed_agents_session_status_idle_event import (
    BetaManagedAgentsSessionStatusIdleEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_custom_tool_result_event import (
    BetaManagedAgentsUserCustomToolResultEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_define_outcome_event import (
    BetaManagedAgentsUserDefineOutcomeEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_interrupt_event import (
    BetaManagedAgentsUserInterruptEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_message_event import (
    BetaManagedAgentsUserMessageEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_user_tool_confirmation_event import (
    BetaManagedAgentsUserToolConfirmationEvent,
)
from daimon.testing.ma import (
    EMPTY_SESSION_STATS,
    EMPTY_SESSION_USAGE,
    FakeMAState,
    NotHandled,
    _ma_id,  # pyright: ignore[reportPrivateUsage]
    json_body,
    list_response,
    not_found_response,
    sse_response,
)

__all__ = ["FakeSessionsState", "make_fake_sessions_handler", "session_turn_sse"]

# Resource union member types carry heterogeneous "identity" shapes: file and
# github_repository resources have an `id`; memory_store resources don't (only
# `memory_store_id`). Any concrete resource instance built by this module.
SessionResource = BetaManagedAgentsSessionResource

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class FakeSessionsState:
    """Shared in-memory state for the stateful MA sessions fake.

    `ma` is the agent store shared with `make_fake_ma_handler` -- session
    create reads it to freeze an agent snapshot at creation time; a later
    `agents.update` against `ma` never affects an already-created session
    (matches the SDK's own snapshot semantics).

    `resources` is the single source of truth for a session's mounted
    resources; the corresponding `BetaManagedAgentsSession.resources` field
    stored in `sessions` is kept in sync via `_sync_session_resources` after
    every add/delete/update so a `GET`/`list` never disagrees with a prior
    `resources.add`/`.delete`/`.update`.

    `duplicate_mount_path`, `delete_unmounts`, and
    `events_listable_after_archive` gate behaviours the live-API exploration on 2026-09-13
    could not confirm against the live API; flip them per-test until a
    live probe settles the default.
    """

    ma: FakeMAState
    sessions: dict[str, BetaManagedAgentsSession] = field(
        default_factory=dict[str, BetaManagedAgentsSession]
    )
    pending_agent_update: dict[str, dict[str, Any]] = field(
        default_factory=dict[str, dict[str, Any]]
    )
    resources: dict[str, list[SessionResource]] = field(
        default_factory=dict[str, list[SessionResource]]
    )
    files: dict[str, tuple[FileMetadata, bytes]] = field(
        default_factory=dict[str, tuple[FileMetadata, bytes]]
    )
    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict[str, list[dict[str, Any]]])
    sandbox: dict[str, dict[str, bytes]] = field(default_factory=dict[str, dict[str, bytes]])
    sent_batches: list[tuple[str, list[dict[str, Any]]]] = field(
        default_factory=list[tuple[str, list[dict[str, Any]]]]
    )
    deleted_sessions: set[str] = field(default_factory=set[str])
    stream_scripts: dict[str, list[httpx.Response]] = field(
        default_factory=dict[str, list[httpx.Response]]
    )
    # file_id -> [(session_id, mount_path), ...] mounted from that file, so
    # files.delete can honour `delete_unmounts` without scanning every
    # session's sandbox.
    mount_index: dict[str, list[tuple[str, str]]] = field(
        default_factory=dict[str, list[tuple[str, str]]]
    )
    duplicate_mount_path: Literal["reject", "shadow"] = "reject"
    delete_unmounts: bool = False
    events_listable_after_archive: bool = True

    def write_output(self, session_id: str, filename: str, content: bytes | str) -> FileMetadata:
        """Test helper: synthesize a downloadable session OUTPUT file, as if
        the agent had written it to `/mnt/session/outputs/` during a turn.

        Registers it in `files` with `downloadable=True` and scoped to
        `session_id`, so `files.list(scope_id=session_id)` and
        `files.download` see it -- mirrors `output_delivery.py`'s sweep.
        Distinct from a mounted upload (`downloadable=False`, set by
        `resources.add` / session-create resources): rule 8 in the live-API
        brief.
        """
        data = content.encode() if isinstance(content, str) else content
        file_id = _ma_id("file")
        meta = FileMetadata(
            id=file_id,
            created_at=_now(),
            filename=filename,
            mime_type="application/octet-stream",
            size_bytes=len(data),
            type="file",
            downloadable=True,
            scope=BetaFileScope(id=session_id, type="session"),
        )
        self.files[file_id] = (meta, data)
        return meta


def session_turn_sse(*events: BaseModel) -> httpx.Response:
    """Frame already-constructed SDK session-event models as one SSE
    response, for queuing into `FakeSessionsState.stream_scripts`.

    Only serializes and frames; the caller constructs the real SDK event
    models (e.g. `BetaManagedAgentsSessionStatusIdleEvent(...)`), so this
    helper never invents payload content of its own.
    """
    return sse_response([e.model_dump(mode="json") for e in events])


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _error_response(
    status: int, message: str, error_type: str = "invalid_request_error"
) -> httpx.Response:
    return httpx.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": message}}
    )


def _paginate(items: list[dict[str, Any]], params: httpx.QueryParams) -> httpx.Response:
    """`{data, next_page}` cursor-page envelope shared by sessions.list,
    sessions.resources.list, and sessions.events.list. `page` is an opaque
    string offset into `items`; `limit` truncates."""
    start = int(params.get("page", "0"))
    limit_s = params.get("limit")
    end = start + int(limit_s) if limit_s is not None else len(items)
    page_items = items[start:end]
    next_page = str(end) if end < len(items) else None
    return (
        list_response(page_items)
        if next_page is None
        else httpx.Response(200, json={"data": page_items, "next_page": next_page})
    )


def _page_response(items: list[FileMetadata], params: httpx.QueryParams) -> httpx.Response:
    """`{data, has_more, first_id, last_id}` envelope for files.list
    (`SyncPage[FileMetadata]`) -- distinct from the cursor-page shape above,
    per the installed SDK's `pagination.SyncPage`."""
    limit = int(params.get("limit", "20"))
    after_id = params.get("after_id")
    before_id = params.get("before_id")
    if after_id is not None:
        idx = next((i for i, m in enumerate(items) if m.id == after_id), None)
        items = items[idx + 1 :] if idx is not None else []
    if before_id is not None:
        idx = next((i for i, m in enumerate(items) if m.id == before_id), None)
        items = items[:idx] if idx is not None else []
    page_items = items[:limit]
    body = {
        "data": [m.model_dump(mode="json") for m in page_items],
        "has_more": len(items) > limit,
        "first_id": page_items[0].id if page_items else None,
        "last_id": page_items[-1].id if page_items else None,
    }
    return httpx.Response(200, json=body)


def _resource_identity(resource: SessionResource) -> str | None:
    """file/github_repository resources have `.id`; memory_store does not."""
    return getattr(resource, "id", None)


def _normalize_mount_path(file_id: str, mount_path: str | None) -> str:
    """Live fact (observed 2026-09-13): both a bare segment ("x") and a
    slash-prefixed one ("/x") land at the same `/mnt/session/uploads/x` --
    any given mount_path is joined under the uploads root regardless of a
    leading slash. Omitted defaults to `/mnt/session/uploads/<file_id>`."""
    if not mount_path:
        return f"/mnt/session/uploads/{file_id}"
    return f"/mnt/session/uploads/{mount_path.lstrip('/')}"


def _sync_session_resources(state: FakeSessionsState, session_id: str) -> None:
    session = state.sessions[session_id]
    state.sessions[session_id] = session.model_copy(
        update={"resources": state.resources.get(session_id, []), "updated_at": _now()}
    )


# ---------------------------------------------------------------------------
# Agent snapshot freeze (session create)
# ---------------------------------------------------------------------------


def _model_config_from(raw: object) -> BetaManagedAgentsModelConfig:
    """`model` may be a bare model-id string or a `{id, speed}` object, both
    on the stored agent row and on an `agent_with_overrides` override."""
    if isinstance(raw, str):
        return BetaManagedAgentsModelConfig(id=raw)  # pyright: ignore[reportArgumentType]
    if isinstance(raw, dict):
        raw_dict = cast("dict[str, object]", raw)
        return BetaManagedAgentsModelConfig(
            id=cast(str, raw_dict.get("id", "claude-sonnet-4-6")),  # pyright: ignore[reportArgumentType]
            speed=raw_dict.get("speed"),  # pyright: ignore[reportArgumentType]
        )
    return BetaManagedAgentsModelConfig(id="claude-sonnet-4-6")  # pyright: ignore[reportArgumentType]


def _build_session_agent(
    agent_row: dict[str, object], *, version: int, overrides: dict[str, Any] | None
) -> BetaManagedAgentsSessionAgent:
    overrides = overrides or {}
    model_raw = overrides["model"] if "model" in overrides else agent_row.get("model")
    return BetaManagedAgentsSessionAgent(
        id=str(agent_row["id"]),
        type="agent",
        name=str(agent_row.get("name", "")),
        model=_model_config_from(model_raw),
        mcp_servers=overrides.get("mcp_servers", agent_row.get("mcp_servers", [])),  # pyright: ignore[reportArgumentType]
        skills=overrides.get("skills", agent_row.get("skills", [])),  # pyright: ignore[reportArgumentType]
        tools=overrides.get("tools", agent_row.get("tools", [])),  # pyright: ignore[reportArgumentType]
        system=overrides["system"] if "system" in overrides else agent_row.get("system"),  # pyright: ignore[reportArgumentType]
        version=version,
    )


def _resolve_agent_spec(
    state: FakeSessionsState, agent_spec: object
) -> tuple[BetaManagedAgentsSessionAgent, None] | tuple[None, httpx.Response]:
    """Resolve the `agent` union (str id | {type:"agent",...} |
    {type:"agent_with_overrides",...}) against `state.ma.agents`, apply
    overrides, and pin the requested-or-latest version -- reading the agent
    NOW so the returned snapshot is immune to a later `agents.update`."""
    overrides: dict[str, Any] | None
    req_version: object
    if isinstance(agent_spec, str):
        agent_id, overrides, req_version = agent_spec, None, None
    else:
        assert isinstance(agent_spec, dict)
        agent_dict = cast("dict[str, Any]", agent_spec)
        agent_id = cast(str, agent_dict["id"])
        overrides = agent_dict if agent_dict.get("type") == "agent_with_overrides" else None
        req_version = agent_dict.get("version")

    agent_row = state.ma.agents.get(agent_id)
    if agent_row is None:
        return None, not_found_response(f"no such agent: {agent_id}")

    if req_version is not None:
        # Pin an EXPLICIT prior version: `state.ma.agents` only ever holds
        # the latest (each agents.update overwrites it in place), so a
        # requested older version must come from the history side-table --
        # not from the current row, which may already be newer.
        version = int(cast(int, req_version))
        historical = state.ma.agent_versions.get(agent_id, {}).get(version)
        if historical is None:
            return None, not_found_response(f"no such agent version: {agent_id} v{version}")
        agent_row = historical
    else:
        version = int(cast(int, agent_row.get("version", 1)))

    return _build_session_agent(agent_row, version=version, overrides=overrides), None


# ---------------------------------------------------------------------------
# Resource materialization (session create + resources.add)
# ---------------------------------------------------------------------------


def _materialize_resource(
    state: FakeSessionsState, session_id: str, spec: dict[str, Any], *, allow_all_types: bool
) -> SessionResource | httpx.Response:
    rtype = spec.get("type")

    if rtype == "file":
        file_id_raw = spec.get("file_id")
        if not isinstance(file_id_raw, str):
            return _error_response(400, f"file_id must be a string, got {file_id_raw!r}")
        file_id = file_id_raw
        entry = state.files.get(file_id)
        if entry is None:
            return _error_response(400, f"No such file: {file_id!r}")
        meta, data = entry
        mount_path = _normalize_mount_path(file_id, spec.get("mount_path"))
        sandbox = state.sandbox.setdefault(session_id, {})
        if mount_path in sandbox and state.duplicate_mount_path == "reject":
            return _error_response(400, f"mount_path already in use: {mount_path!r}")
        sandbox[mount_path] = data
        state.mount_index.setdefault(file_id, []).append((session_id, mount_path))
        if meta.scope is None:
            state.files[file_id] = (
                meta.model_copy(update={"scope": BetaFileScope(id=session_id, type="session")}),
                data,
            )
        now = _now()
        return BetaManagedAgentsFileResource(
            id=_ma_id("res"),
            created_at=now,
            file_id=file_id,
            mount_path=mount_path,
            type="file",
            updated_at=now,
        )

    if not allow_all_types:
        return _error_response(
            400, f"Only file resources can be added to an existing session (got {rtype!r})."
        )

    if rtype == "github_repository":
        now = _now()
        url = str(spec["url"])
        default_mount = f"/workspace/{url.rstrip('/').rsplit('/', 1)[-1]}"
        return BetaManagedAgentsGitHubRepositoryResource(
            id=_ma_id("res"),
            created_at=now,
            mount_path=spec.get("mount_path") or default_mount,
            type="github_repository",
            updated_at=now,
            url=url,
            checkout=spec.get("checkout"),
        )

    if rtype == "memory_store":
        return BetaManagedAgentsMemoryStoreResource(
            memory_store_id=spec["memory_store_id"],
            type="memory_store",
            access=spec.get("access"),
            description=spec.get("description"),
            instructions=spec.get("instructions"),
            mount_path=spec.get("mount_path"),
            name=spec.get("name"),
        )

    return _error_response(400, f"Unknown resource type: {rtype!r}")


# ---------------------------------------------------------------------------
# Sessions: create / retrieve / update / archive / delete / list
# ---------------------------------------------------------------------------


def _handle_session_create(state: FakeSessionsState, request: httpx.Request) -> httpx.Response:
    body = json_body(request)
    agent, err = _resolve_agent_spec(state, body["agent"])
    if err is not None:
        return err
    assert agent is not None

    session_id = _ma_id("session")
    resources_out: list[SessionResource] = []
    raw_resources = cast("list[dict[str, Any]]", body.get("resources") or [])
    for raw_resource in raw_resources:
        result = _materialize_resource(state, session_id, raw_resource, allow_all_types=True)
        if isinstance(result, httpx.Response):
            return result
        resources_out.append(result)
    state.resources[session_id] = resources_out

    now = _now()
    session = BetaManagedAgentsSession(
        id=session_id,
        type="session",
        status="idle",
        agent=agent,
        environment_id=body["environment_id"],
        created_at=now,
        updated_at=now,
        metadata=body.get("metadata") or {},
        outcome_evaluations=[],
        resources=resources_out,
        stats=EMPTY_SESSION_STATS,
        usage=EMPTY_SESSION_USAGE,
        vault_ids=body.get("vault_ids") or [],
        title=body.get("title"),
    )
    state.sessions[session_id] = session
    state.events[session_id] = []
    state.sandbox.setdefault(session_id, {})
    return httpx.Response(200, json=session.model_dump(mode="json"))


def _handle_session_retrieve(state: FakeSessionsState, session_id: str) -> httpx.Response:
    if session_id in state.deleted_sessions or session_id not in state.sessions:
        return not_found_response(f"no such session: {session_id}")
    # ALWAYS the stored session -- never re-read state.ma.agents, which would
    # break the create-time snapshot-freeze guarantee.
    return httpx.Response(200, json=state.sessions[session_id].model_dump(mode="json"))


_ALLOWED_AGENT_UPDATE_KEYS = frozenset({"tools", "mcp_servers"})


def _validate_metadata_patch(current: dict[str, str], patch: dict[str, str | None]) -> str | None:
    merged = dict(current)
    for key, value in patch.items():
        if len(key) > 64:
            return f"metadata key too long (max 64 chars): {key!r}"
        if value is None:
            merged.pop(key, None)
            continue
        if len(value) > 512:
            return f"metadata value too long (max 512 chars) for key {key!r}"
        merged[key] = value
    if len(merged) > 16:
        return "metadata: maximum 16 pairs"
    return None


def _handle_session_update(
    state: FakeSessionsState, session_id: str, request: httpx.Request
) -> httpx.Response:
    if session_id in state.deleted_sessions or session_id not in state.sessions:
        return not_found_response(f"no such session: {session_id}")
    body = json_body(request)

    if "vault_ids" in body:
        return _error_response(
            400, "vault_ids: Not yet supported; requests setting this field are rejected."
        )

    agent_patch = body.get("agent")
    if agent_patch is not None:
        bad_keys = sorted(set(agent_patch) - _ALLOWED_AGENT_UPDATE_KEYS)
        if bad_keys:
            return _error_response(
                400,
                f"agent.{bad_keys[0]}: only 'tools' and 'mcp_servers' are updatable mid-session.",
            )

    metadata_patch = body.get("metadata")
    if metadata_patch is not None:
        err = _validate_metadata_patch(state.sessions[session_id].metadata, metadata_patch)
        if err is not None:
            return _error_response(400, err)

    pending = state.pending_agent_update.setdefault(session_id, {})
    if agent_patch is not None:
        pending["agent"] = {**pending.get("agent", {}), **agent_patch}
    if "metadata" in body:
        pending["metadata"] = {**pending.get("metadata", {}), **(metadata_patch or {})}
    if "title" in body:
        pending["title"] = body["title"]

    # "Applies from the next turn": the update response mirrors the
    # currently-stored session, not the pending patch.
    return httpx.Response(200, json=state.sessions[session_id].model_dump(mode="json"))


def _apply_pending_update(state: FakeSessionsState, session_id: str) -> None:
    pending = state.pending_agent_update.pop(session_id, None)
    if not pending:
        return
    session = state.sessions[session_id]
    updates: dict[str, Any] = {}

    if "agent" in pending:
        agent_patch = pending["agent"]
        agent_dict = session.agent.model_dump(mode="json")
        if "tools" in agent_patch:
            agent_dict["tools"] = agent_patch["tools"]
        if "mcp_servers" in agent_patch:
            agent_dict["mcp_servers"] = agent_patch["mcp_servers"]
        # Reconstruct (not model_copy) so the merged tools/mcp_servers are
        # re-validated against their discriminated unions, not stashed as
        # raw dicts.
        updates["agent"] = BetaManagedAgentsSessionAgent(**agent_dict)

    if "metadata" in pending:
        merged = dict(session.metadata)
        for key, value in pending["metadata"].items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = value
        updates["metadata"] = merged

    if "title" in pending:
        updates["title"] = pending["title"]

    if updates:
        updates["updated_at"] = _now()
        state.sessions[session_id] = session.model_copy(update=updates)


def _handle_session_archive(state: FakeSessionsState, session_id: str) -> httpx.Response:
    if session_id in state.deleted_sessions or session_id not in state.sessions:
        return not_found_response(f"no such session: {session_id}")
    archived = state.sessions[session_id].model_copy(
        update={"archived_at": _now(), "updated_at": _now()}
    )
    state.sessions[session_id] = archived
    return httpx.Response(200, json=archived.model_dump(mode="json"))


def _handle_session_delete(state: FakeSessionsState, session_id: str) -> httpx.Response:
    if session_id in state.deleted_sessions or session_id not in state.sessions:
        return not_found_response(f"no such session: {session_id}")
    state.deleted_sessions.add(session_id)
    deleted = BetaManagedAgentsDeletedSession(id=session_id, type="session_deleted")
    return httpx.Response(200, json=deleted.model_dump(mode="json"))


# Exactly the query params the installed SDK's SessionListParams sends
# (plus "beta", embedded in the URL literal by every sessions.* method) --
# anything else 400s so tests cannot rely on a filter production lacks.
_SESSION_LIST_PARAMS = frozenset(
    {
        "agent_id",
        "agent_version",
        "created_at_gt",
        "created_at_gte",
        "created_at_lt",
        "created_at_lte",
        "deployment_id",
        "include_archived",
        "limit",
        "memory_store_id",
        "order",
        "page",
        "statuses[]",
        "beta",
    }
)


def _handle_session_list(state: FakeSessionsState, request: httpx.Request) -> httpx.Response:
    params = request.url.params
    unknown = sorted(set(params.keys()) - _SESSION_LIST_PARAMS)
    if unknown:
        return _error_response(400, f"Unknown query parameter(s): {unknown}")

    agent_id = params.get("agent_id")
    include_archived = params.get("include_archived") == "true"
    statuses = set(params.get_list("statuses[]")) or None

    sessions = [
        s
        for sid, s in state.sessions.items()
        if sid not in state.deleted_sessions
        and (agent_id is None or s.agent.id == agent_id)
        and (include_archived or s.archived_at is None)
        and (statuses is None or s.status in statuses)
    ]
    sessions.sort(key=lambda s: s.created_at, reverse=params.get("order", "desc") != "asc")
    return _paginate([s.model_dump(mode="json") for s in sessions], params)


# ---------------------------------------------------------------------------
# Session resources
# ---------------------------------------------------------------------------


def _handle_resources_list(
    state: FakeSessionsState, session_id: str, request: httpx.Request
) -> httpx.Response:
    if session_id in state.deleted_sessions or session_id not in state.sessions:
        return not_found_response(f"no such session: {session_id}")
    items = [r.model_dump(mode="json") for r in state.resources.get(session_id, [])]
    return _paginate(items, request.url.params)


def _handle_resources_add(
    state: FakeSessionsState, session_id: str, request: httpx.Request
) -> httpx.Response:
    if session_id in state.deleted_sessions or session_id not in state.sessions:
        return not_found_response(f"no such session: {session_id}")
    body = json_body(request)
    result = _materialize_resource(state, session_id, body, allow_all_types=False)
    if isinstance(result, httpx.Response):
        return result
    state.resources.setdefault(session_id, []).append(result)
    _sync_session_resources(state, session_id)
    return httpx.Response(200, json=result.model_dump(mode="json"))


def _handle_resources_delete(
    state: FakeSessionsState, session_id: str, resource_id: str
) -> httpx.Response:
    if session_id in state.deleted_sessions or session_id not in state.sessions:
        return not_found_response(f"no such session: {session_id}")
    resources = state.resources.get(session_id, [])
    match = next((r for r in resources if _resource_identity(r) == resource_id), None)
    if match is None:
        return not_found_response(f"no such resource: {resource_id}")
    state.resources[session_id] = [r for r in resources if _resource_identity(r) != resource_id]
    # Unlike DELETE /v1/files/{id} (parameterized by `delete_unmounts` --
    # unresolved whether deleting the FILE object also drops its sandbox
    # copies), removing a session's RESOURCE (this endpoint) unconditionally
    # unmounts it: the resource row *is* the mount, so deleting the row must
    # always take the mount with it. the observed-behaviour notes state this
    # unconditionally ("removes sandbox entry"), unlike rule 8's file-delete
    # sandbox behaviour which it explicitly parameterizes.
    if isinstance(match, BetaManagedAgentsFileResource):
        state.sandbox.get(session_id, {}).pop(match.mount_path, None)
    _sync_session_resources(state, session_id)
    deleted = BetaManagedAgentsDeleteSessionResource(
        id=resource_id, type="session_resource_deleted"
    )
    return httpx.Response(200, json=deleted.model_dump(mode="json"))


def _handle_resources_update(
    state: FakeSessionsState, session_id: str, resource_id: str, request: httpx.Request
) -> httpx.Response:
    if session_id in state.deleted_sessions or session_id not in state.sessions:
        return not_found_response(f"no such session: {session_id}")
    resources = state.resources.get(session_id, [])
    idx = next((i for i, r in enumerate(resources) if _resource_identity(r) == resource_id), None)
    if idx is None:
        return not_found_response(f"no such resource: {resource_id}")
    match = resources[idx]
    if not isinstance(match, BetaManagedAgentsGitHubRepositoryResource):
        return _error_response(
            400,
            "authorization_token rotation is only supported on github_repository resources "
            f"(got {match.type!r}).",
        )
    # `authorization_token` itself isn't a field on the response resource --
    # only bump updated_at, matching what the real response would reveal.
    updated = match.model_copy(update={"updated_at": _now()})
    resources[idx] = updated
    _sync_session_resources(state, session_id)
    return httpx.Response(200, json=updated.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Session events: send / list / stream
# ---------------------------------------------------------------------------

_SYSTEM_MESSAGE_PRECEDING_TYPES = frozenset(
    {"user.message", "user.tool_result", "user.custom_tool_result"}
)
_SYSTEM_MESSAGE_MODEL_PREFIXES = (
    "claude-sonnet-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-fable-5",
    "claude-mythos-5",
)


def _build_response_event(ev: dict[str, Any], event_id: str) -> BaseModel:
    """Construct the validated SDK response-event model matching `ev["type"]`.

    Every field is real; `processed_at=None` on every type except
    `user.define_outcome`, whose SDK type declares `processed_at` required
    (non-Optional) -- the one exception to the brief's live-verified
    "processed_at=None on every echoed event" fact.
    """
    etype = ev["type"]
    if etype == "user.message":
        return BetaManagedAgentsUserMessageEvent(
            id=event_id, type="user.message", content=ev["content"], processed_at=None
        )
    if etype == "user.interrupt":
        return BetaManagedAgentsUserInterruptEvent(
            id=event_id,
            type="user.interrupt",
            processed_at=None,
            session_thread_id=ev.get("session_thread_id"),
        )
    if etype == "user.tool_confirmation":
        return BetaManagedAgentsUserToolConfirmationEvent(
            id=event_id,
            type="user.tool_confirmation",
            result=ev["result"],
            tool_use_id=ev["tool_use_id"],
            deny_message=ev.get("deny_message"),
            processed_at=None,
            session_thread_id=ev.get("session_thread_id"),
        )
    if etype == "user.custom_tool_result":
        return BetaManagedAgentsUserCustomToolResultEvent(
            id=event_id,
            type="user.custom_tool_result",
            custom_tool_use_id=ev["custom_tool_use_id"],
            content=ev.get("content"),
            is_error=ev.get("is_error"),
            processed_at=None,
            session_thread_id=ev.get("session_thread_id"),
        )
    if etype == "user.define_outcome":
        return BetaManagedAgentsUserDefineOutcomeEvent(
            id=event_id,
            type="user.define_outcome",
            description=ev["description"],
            max_iterations=ev.get("max_iterations"),
            outcome_id=_ma_id("outc"),
            processed_at=datetime.now(UTC),
            rubric=ev["rubric"],
        )
    if etype == "user.tool_result":
        return BetaManagedAgentsUserToolResultEvent(
            id=event_id,
            type="user.tool_result",
            tool_use_id=ev["tool_use_id"],
            content=ev.get("content"),
            is_error=ev.get("is_error"),
            processed_at=None,
            session_thread_id=ev.get("session_thread_id"),
        )
    if etype == "system.message":
        return BetaManagedAgentsSystemMessageEvent(
            id=event_id, type="system.message", content=ev["content"], processed_at=None
        )
    raise ValueError(f"unhandled event type: {etype!r}")


def _handle_events_send(
    state: FakeSessionsState, session_id: str, request: httpx.Request
) -> httpx.Response:
    if session_id in state.deleted_sessions or session_id not in state.sessions:
        return not_found_response(f"no such session: {session_id}")
    session = state.sessions[session_id]
    if session.archived_at is not None:
        return _error_response(400, "Cannot send events to archived session.")

    body = json_body(request)
    events: list[dict[str, Any]] = body["events"]

    system_indices = [i for i, e in enumerate(events) if e.get("type") == "system.message"]
    if len(system_indices) > 1:
        return _error_response(400, "At most one system.message event is allowed per request.")
    if system_indices:
        idx = system_indices[0]
        if idx != len(events) - 1:
            return _error_response(400, "system.message must be the final event in the request.")
        if idx == 0 or events[idx - 1].get("type") not in _SYSTEM_MESSAGE_PRECEDING_TYPES:
            return _error_response(
                400,
                "system.message must immediately follow a user.message, user.tool_result, "
                "or user.custom_tool_result event.",
            )
        model_id = session.agent.model.id
        if not model_id.startswith(_SYSTEM_MESSAGE_MODEL_PREFIXES):
            return _error_response(400, f"system.message is not supported on model {model_id!r}.")

    # "Applies from the next turn": apply any stashed sessions.update patch
    # now, before processing this batch.
    _apply_pending_update(state, session_id)

    response_events: list[BaseModel] = []
    stored_events: list[dict[str, Any]] = []
    for ev in events:
        built = _build_response_event(ev, _ma_id("evt"))
        response_events.append(built)
        stored_events.append(built.model_dump(mode="json"))

    state.events.setdefault(session_id, []).extend(stored_events)
    state.sent_batches.append((session_id, stored_events))

    result = BetaManagedAgentsSendSessionEvents(data=response_events)  # pyright: ignore[reportArgumentType]
    return httpx.Response(200, json=result.model_dump(mode="json"))


_EVENT_LIST_PARAMS = frozenset(
    {
        "created_at[gt]",
        "created_at[gte]",
        "created_at[lt]",
        "created_at[lte]",
        "limit",
        "order",
        "page",
        "types[]",
        "beta",
    }
)


def _handle_events_list(
    state: FakeSessionsState, session_id: str, request: httpx.Request
) -> httpx.Response:
    if session_id in state.deleted_sessions:
        return not_found_response(f"no such session: {session_id}")
    session = state.sessions.get(session_id)
    if session is None:
        return not_found_response(f"no such session: {session_id}")
    if session.archived_at is not None and not state.events_listable_after_archive:
        return not_found_response(f"no such session: {session_id}")

    params = request.url.params
    unknown = sorted(set(params.keys()) - _EVENT_LIST_PARAMS)
    if unknown:
        return _error_response(400, f"Unknown query parameter(s): {unknown}")

    types = set(params.get_list("types[]")) or None
    events = list(state.events.get(session_id, []))
    if types is not None:
        events = [e for e in events if e.get("type") in types]
    if params.get("order", "asc") == "desc":
        events = list(reversed(events))
    return _paginate(events, params)


def _handle_events_stream(state: FakeSessionsState, session_id: str) -> httpx.Response:
    scripts = state.stream_scripts.get(session_id)
    if scripts:
        return scripts.pop(0)
    # No script queued: serve a minimal idle-only terminal stream so a
    # driver-driven test doesn't hang waiting for a stream to end.
    idle = BetaManagedAgentsSessionStatusIdleEvent(
        id=_ma_id("evt"),
        processed_at=datetime.now(UTC),
        stop_reason=BetaManagedAgentsSessionEndTurn(type="end_turn"),
        type="session.status_idle",
    )
    return session_turn_sse(idle)


# ---------------------------------------------------------------------------
# Files API
# ---------------------------------------------------------------------------


def _parse_multipart_file(request: httpx.Request) -> tuple[str, str, bytes]:
    """Parse the single "file" part of a `files.upload` multipart body.

    Encoding verified live via a `MockTransport` echo of a real
    `client.beta.files.upload(...)` call (observed 2026-09-13): standard RFC 2046
    multipart/form-data, one part named "file", CRLF-delimited, with
    `Content-Disposition: form-data; name="file"; filename="..."` and a
    `Content-Type` header on that part.
    """
    content_type = request.headers.get("content-type", "")
    boundary_match = re.search(r'boundary=("?)([^";]+)\1', content_type)
    if boundary_match is None:
        raise ValueError(f"no multipart boundary in Content-Type header: {content_type!r}")
    boundary = boundary_match.group(2).encode()

    for raw in request.content.split(b"--" + boundary):
        if raw in (b"", b"--", b"--\r\n"):
            continue
        part = raw[2:] if raw.startswith(b"\r\n") else raw
        if part.endswith(b"\r\n"):
            part = part[:-2]
        header_blob, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        headers: dict[str, str] = {}
        for line in header_blob.split(b"\r\n"):
            name, colon, value = line.partition(b":")
            if colon:
                headers[name.strip().lower().decode()] = value.strip().decode()
        disposition = headers.get("content-disposition", "")
        if 'name="file"' not in disposition:
            continue
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        filename = filename_match.group(1) if filename_match else "upload"
        mime_type = headers.get("content-type", "application/octet-stream")
        return filename, mime_type, content

    raise ValueError("multipart/form-data body has no field named 'file'")


def _handle_files_list(state: FakeSessionsState, request: httpx.Request) -> httpx.Response:
    params = request.url.params
    scope_id = params.get("scope_id")
    items = [
        meta
        for meta, _ in state.files.values()
        if scope_id is None or (meta.scope is not None and meta.scope.id == scope_id)
    ]
    items.sort(key=lambda m: m.created_at)
    return _page_response(items, params)


def _handle_files_upload(state: FakeSessionsState, request: httpx.Request) -> httpx.Response:
    filename, mime_type, content = _parse_multipart_file(request)
    file_id = _ma_id("file")
    meta = FileMetadata(
        id=file_id,
        created_at=_now(),
        filename=filename,
        mime_type=mime_type,
        size_bytes=len(content),
        type="file",
        downloadable=False,
        scope=None,
    )
    state.files[file_id] = (meta, content)
    return httpx.Response(200, json=meta.model_dump(mode="json"))


def _handle_files_download(state: FakeSessionsState, file_id: str) -> httpx.Response:
    entry = state.files.get(file_id)
    if entry is None:
        return not_found_response(f"no such file: {file_id}")
    _, data = entry
    return httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=data)


def _handle_files_delete(state: FakeSessionsState, file_id: str) -> httpx.Response:
    entry = state.files.get(file_id)
    if entry is None:
        return not_found_response(f"no such file: {file_id}")
    del state.files[file_id]
    if state.delete_unmounts:
        for sid, path in state.mount_index.pop(file_id, []):
            state.sandbox.get(sid, {}).pop(path, None)
    deleted = DeletedFile(id=file_id, type="file_deleted")
    return httpx.Response(200, json=deleted.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def make_fake_sessions_handler(
    state: FakeSessionsState,
) -> Callable[[httpx.Request], httpx.Response]:
    """Stateful fake for the SESSIONS surface: `/v1/sessions...` and
    `/v1/files...`.

    Raises `NotHandled` for anything else -- compose with
    `make_fake_memory_store_handler` and `make_fake_ma_handler` via
    `combine_handlers`, with the agent fake LAST: `make_fake_ma_handler`
    never raises `NotHandled` (it ends in its own 404 catch-all for
    unmatched `/v1/agents` paths), so anything after it in the chain is
    unreachable.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "POST" and path == "/v1/sessions":
            return _handle_session_create(state, request)
        if method == "GET" and path == "/v1/sessions":
            return _handle_session_list(state, request)

        m = re.fullmatch(r"/v1/sessions/(?P<sid>[^/]+)/resources/(?P<rid>[^/]+)", path)
        if m:
            sid, rid = m.group("sid"), m.group("rid")
            if method == "DELETE":
                return _handle_resources_delete(state, sid, rid)
            if method == "POST":
                return _handle_resources_update(state, sid, rid, request)

        m = re.fullmatch(r"/v1/sessions/(?P<sid>[^/]+)/resources", path)
        if m:
            sid = m.group("sid")
            if method == "GET":
                return _handle_resources_list(state, sid, request)
            if method == "POST":
                return _handle_resources_add(state, sid, request)

        m = re.fullmatch(r"/v1/sessions/(?P<sid>[^/]+)/events/stream", path)
        if m and method == "GET":
            return _handle_events_stream(state, m.group("sid"))

        m = re.fullmatch(r"/v1/sessions/(?P<sid>[^/]+)/events", path)
        if m:
            sid = m.group("sid")
            if method == "POST":
                return _handle_events_send(state, sid, request)
            if method == "GET":
                return _handle_events_list(state, sid, request)

        m = re.fullmatch(r"/v1/sessions/(?P<sid>[^/]+)/archive", path)
        if m and method == "POST":
            return _handle_session_archive(state, m.group("sid"))

        m = re.fullmatch(r"/v1/sessions/(?P<sid>[^/]+)", path)
        if m:
            sid = m.group("sid")
            if method == "GET":
                return _handle_session_retrieve(state, sid)
            if method == "POST":
                return _handle_session_update(state, sid, request)
            if method == "DELETE":
                return _handle_session_delete(state, sid)

        if path == "/v1/files":
            if method == "GET":
                return _handle_files_list(state, request)
            if method == "POST":
                return _handle_files_upload(state, request)

        m = re.fullmatch(r"/v1/files/(?P<fid>[^/]+)/content", path)
        if m and method == "GET":
            return _handle_files_download(state, m.group("fid"))

        m = re.fullmatch(r"/v1/files/(?P<fid>[^/]+)", path)
        if m and method == "DELETE":
            return _handle_files_delete(state, m.group("fid"))

        raise NotHandled

    return handler
