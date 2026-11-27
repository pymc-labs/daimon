"""A routine as an agent-step workflow and back, as plain row writes.

`translate` and `untranslate` are pure and stdlib only: the phase 4 migration vendors this
module. Ids are `uuid5` of the routine id and `now` is an argument, so a move is deterministic.
`apply_move` writes one with SQLAlchemy Core, online and in that migration alike.

Every routine becomes one of four shapes. Its `run` agent step works where it posts (`at`),
else at home, else privately on its owner's personal memory; a Post then posts the result
where the routine did, unless the agent already posted there itself; then `files` posts
only the files it made, if any, as the routine sent them on their own:

- A. a channel or thread: `run` at it, `seen` branch, `post` (or `files`) there, `done`;
- B. a DM: `run` private, `seen`, `post` (or `files`) to the person it runs as, `done`;
- C. no destination, a channel: `run` at home, `done`;
- D. neither: `run` private, `done`.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from typing import Any, Final, Literal

import sqlalchemy as sa

NAMESPACE: Final = uuid.UUID("5d2f7c1e-6a0b-4f3e-9c8d-7b1a2e4f6c90")
SCHEMA: Final = "daimon.workflow/v1"
STEP: Final = "run"
SEEN: Final = "seen"
POST: Final = "post"
FILES: Final = "files"
DONE: Final = "done"
TRIGGER: Final = "schedule"
NOTE: Final = "Moved from a routine"
MAX_TITLE: Final = 120
#: `routine_files.PREFIX` and `OUTBOX_FILES`: a routine's pending result with file refs.
FILES_PREFIX: Final = "\x1eroutine-files-v1\n"
OUTBOX_FILES: Final = "generated_files"
#: `delivery._SLACK_THREAD` and `_TEAMS_THREAD`, as `delivery.target_for` splits a thread id.
_SLACK_THREAD: Final = re.compile(r"([CG][A-Z0-9]{2,}):([0-9]+\.[0-9]+)")
_TEAMS_THREAD: Final = ";messageid="
#: A schedule trigger's keys a routine holds.
_SCHEDULE_KEYS: Final = frozenset({"id", "kind", "cron", "timezone", "catch_up"})

Reason = Literal[
    "handback_paused",
    "invalid_schedule",
    "owner_unlinked",
    "busy",
    "not_a_routine",
    "changed",
]


@dataclass(frozen=True)
class Write:
    """One statement: insert `values`, or update or delete the rows matching `where`."""

    table: str
    op: Literal["insert", "update", "delete"]
    values: Mapping[str, Any] = field(default_factory=dict[str, Any])
    where: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class Move:
    """A translation either way: `id` is the row it makes, `spec` the workflow's (or the one
    undone), `outcome` exact or paused, `notes` what else moves."""

    id: uuid.UUID
    outcome: Literal["exact", "paused"]
    notes: tuple[str, ...]
    spec: dict[str, Any]
    spec_hash: str
    writes: tuple[Write, ...]


@dataclass(frozen=True)
class Refusal:
    reason: Reason


@dataclass(frozen=True)
class RoutineSnapshot:
    """A routine row's columns, plus what the caller reads beside it: the owner's account,
    whether it sets its own result header (dropped), whether a pinned sandbox On decides it
    and who set that, the intake config bound to it and its schedule's next slot (None when
    the schedule is invalid)."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    created_by_user_id: str | None
    agent_id: str
    agent_name: str
    name: str | None
    cron_expr: str
    timezone: str
    trigger_message: str
    enabled: bool
    catch_up_policy: str
    next_fire_at: datetime | None
    last_fired_at: datetime | None
    last_fired_slot: datetime | None
    last_finished_at: datetime | None
    seeded_after: datetime | None
    source_routine_id: uuid.UUID | None
    handback_paused_at: datetime | None
    last_error: str | None
    last_result_tail: str | None
    destination_kind: str | None
    destination_id: str | None
    delivery_status: str | None
    delivery_payload: str | None
    delivery_note: str | None
    delivery_hold: dict[str, Any] | None
    channel_id: str | None
    made_in_channel_id: str | None
    source_provenance_known: bool
    source_channel_id: str | None
    source_thread_id: str | None
    created_at: datetime
    owner_account_id: uuid.UUID | None = None
    owner_notice: dict[str, Any] | None = None
    header: bool = False
    sandbox: bool = False
    sandbox_set_by: uuid.UUID | None = None
    intake: dict[str, Any] | None = None
    next_slot: datetime | None = None

    @classmethod
    def of(cls, row: Mapping[str, object], extra: Mapping[str, object]) -> RoutineSnapshot:
        """From a routine row's fields (others ignored) and the `extra` read beside it."""
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in row.items() if k in names}, **extra)  # pyright: ignore[reportArgumentType]


@dataclass(frozen=True)
class Delivery:
    id: uuid.UUID
    kind: str
    status: str
    payload: dict[str, Any]
    note: str | None


@dataclass(frozen=True)
class WorkflowSnapshot:
    """A converted workflow: its row, live spec and publisher, schedule cursor, unsent posts
    and notices (oldest first), intake config, runs and how the last ended."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    name: str
    status: str
    owner_account_id: uuid.UUID | None
    home_channel_id: str | None
    made_in: dict[str, Any] | None
    converted_from: dict[str, Any] | None
    spec: dict[str, Any]
    published_by: uuid.UUID | None
    next_fire_at: datetime | None
    last_fired_slot: datetime | None
    seeded_after: datetime | None
    deliveries: tuple[Delivery, ...] = ()
    intake: dict[str, Any] | None = None
    active_runs: bool = False
    #: How its last ended live run ended and why it failed; None before its first.
    last_status: str | None = None
    last_error: str | None = None


def ids(routine_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """The rows a routine's move makes, by name."""
    names = ("workflow", "trigger", "post", "notice", "grant")
    return {k: uuid.uuid5(NAMESPACE, f"{routine_id}:{k}") for k in names}


def spec_hash(spec: Mapping[str, Any]) -> str:
    """The canonical spec's sha256, as `workflows.spec.spec_hash` (and `node_hash` of a node)."""
    text = json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def slug(name: str) -> str:
    """`workflows.spec.slug`: the key a spec names an agent by."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def label(prompt: str, routine_id: uuid.UUID) -> str:
    """`routines.routine_label`: the message's first 60 characters, else a hex-id fallback."""
    return prompt.strip()[:60] or f"routine {routine_id.hex[:8]}"


def routine_name(name: str, converted_from: Mapping[str, Any] | None, prompt: str) -> str | None:
    """A converted workflow's name as its routine's: None while it keeps an unnamed one's label."""
    cf = converted_from or {}
    routine_id = cf.get("routine_id")
    blank = routine_id is not None and cf.get("name") is None
    return None if blank and name == label(prompt, uuid.UUID(routine_id)) else name


def _iso(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat()


def _to(kind: str | None, target: str | None) -> dict[str, Any] | None:
    if kind is None:
        return None
    return {"kind": "dm"} if kind == "dm" else {"kind": kind, "id": target}


def _spec(
    name: str,
    *,
    agent: str,
    prompt: str,
    trigger: dict[str, Any],
    home: str | None,
    to: Mapping[str, Any] | None,
    sandbox: bool,
) -> dict[str, Any]:
    """The workflow of a routine posting `to` (`_to`), in canonical key order."""
    key = slug(agent)
    place = None if to is None or to["kind"] == "dm" else "home" if to["id"] == home else to["id"]
    post = None if to is None else "run_as" if to["kind"] == "dm" else place
    run: dict[str, Any] = {"title": name, "next": DONE if post is None else SEEN, "kind": "agent"}
    run |= {"agent": key, "instructions": prompt, "writes": "policy"}
    if sandbox:
        run["builtins"] = "all"
    run["can_finish_early"] = True
    if place is not None or (to is None and home is not None):
        run["at"] = place or "home"
    else:
        run["memory"] = "personal"
    nodes: dict[str, Any] = {STEP: run}
    if post is not None:
        # `files` posts nothing when the step made none (`delivery.queue_deliver`).
        cases = [{"when": f"${{{{ '{post}' in steps.{STEP}.posted }}}}", "to": FILES}]
        nodes[SEEN] = {"title": "Posted already?", "kind": "branch", "cases": cases, "else": POST}
        files = f"${{{{ steps.{STEP}.files }}}}"
        nodes[POST] = {
            "to": post,
            "message": f"${{{{ steps.{STEP}.summary }}}}",
            "files": files,
            "title": "Post the result",
            "next": DONE,
            "kind": "post",
            "as_agent": key,
        }
        nodes[FILES] = {
            "to": post,
            "files": files,
            "title": "Post the files",
            "next": DONE,
            "kind": "post",
            "as_agent": key,
        }
    nodes[DONE] = {"title": "Done", "kind": "finish"}
    spec: dict[str, Any] = {"schema": SCHEMA, "name": name}
    if home is not None:
        spec["home"] = {"channel": home}
    return spec | {
        "visibility": "private",
        "triggers": [trigger],
        "concurrency": {"limit": 1, "on_conflict": "skip"},
        "start": STEP,
        "nodes": nodes,
    }


def _trigger(cron: str, timezone: str | None, catch_up: str | None) -> dict[str, Any]:
    found = {"cron": cron, "timezone": timezone, "catch_up": catch_up}
    return {"id": TRIGGER, "kind": "schedule"} | {k: v for k, v in found.items() if v is not None}


def _place(to: str, home: str | None) -> dict[str, Any]:
    """`delivery.target_for` of a channel or thread id."""
    channel, thread = to, None
    if _TEAMS_THREAD in to:
        channel, _, thread = to.partition(_TEAMS_THREAD)
    elif match := _SLACK_THREAD.fullmatch(to):
        channel, thread = match.group(1), match.group(2)
    kind = "channel" if thread is None else "thread"
    return {"kind": kind, "channel_id": channel, "thread_id": thread, "home": channel == home}


def _pending(snap: RoutineSnapshot) -> tuple[str, list[Any]]:
    """The pending result's text and file refs, as `routine_files.payload_for` reads it."""
    text = snap.delivery_payload or ""
    if snap.delivery_note != OUTBOX_FILES or not text.startswith(FILES_PREFIX):
        return text, []
    body = json.loads(text[len(FILES_PREFIX) :])
    return str(body.get("text") or ""), list(body.get("files") or [])


def _last(snap: RoutineSnapshot) -> dict[str, Any] | None:
    """The "Before migration" strip, whose text the page shows as it shows the runs'."""
    if snap.last_fired_at is None:
        return None
    return {
        "at": _iso(snap.last_finished_at or snap.last_fired_at),
        "status": "failed" if snap.last_error else "succeeded",
        "text": snap.last_error or snap.last_result_tail,
    }


def translate(snap: RoutineSnapshot, *, now: datetime, stuck_after: timedelta) -> Move | Refusal:
    """The workflow a routine becomes, with its cursor, posts, sandbox grant and intake
    binding; the routine row goes. Active when it was enabled with an owner, else paused. A
    fire claimed `stuck_after` ago (the scheduler's dispatch timeout) is dead: it counts as
    finished, and the fence drops a late result."""
    if snap.handback_paused_at is not None:
        return Refusal("handback_paused")
    if snap.next_slot is None:
        return Refusal("invalid_schedule")
    if snap.created_by_user_id is not None and snap.owner_account_id is None:
        return Refusal("owner_unlinked")
    fired = snap.last_fired_at
    if snap.delivery_status == "claimed" or (
        fired is not None and snap.last_finished_at is None and now - fired < stuck_after
    ):
        return Refusal("busy")
    made = ids(snap.id)
    flow, owner = made["workflow"], snap.owner_account_id
    to = _to(snap.destination_kind, snap.destination_id)
    spec = _spec(
        (snap.name or label(snap.trigger_message, snap.id))[:MAX_TITLE],
        agent=snap.agent_name,
        prompt=snap.trigger_message,
        trigger=_trigger(snap.cron_expr, snap.timezone, snap.catch_up_policy),
        home=snap.channel_id,
        to=to,
        sandbox=snap.sandbox,
    )
    active = snap.enabled and owner is not None
    notes: list[str] = [] if active else ["disabled" if owner else "no owner"]
    converted = {
        "routine_id": str(snap.id),
        "source_routine_id": None
        if snap.source_routine_id is None
        else str(snap.source_routine_id),
        "made_in_channel_id": snap.made_in_channel_id,
        "created_at": _iso(snap.created_at),
        "agent_id": snap.agent_id,
        "agent_name": snap.agent_name,
        "name": snap.name,
        "owner": snap.created_by_user_id,
        "owner_account_id": None if owner is None else str(owner),
        "to": to,
        "channel_id": snap.channel_id,
        "fired_at": _iso(snap.last_fired_at),
        "finished_at": _iso(snap.last_finished_at),
        "error": snap.last_error,
        "tail": snap.last_result_tail,
        "migrated_at": _iso(now),
        "last": _last(snap),
    }
    made_in = {"known": snap.source_provenance_known, "channel_id": snap.source_channel_id}
    made_in["thread_id"] = snap.source_thread_id
    writes = [
        Write(
            "workflows",
            "insert",
            {
                "id": flow,
                "tenant_id": snap.tenant_id,
                "name": spec["name"],
                "home_channel_id": snap.channel_id,
                "visibility": "private",
                "status": "active" if active else "paused",
                "owner_account_id": owner,
                "live_version": 1,
                "draft_spec": spec,
                "draft_revision": 1,
                "draft_base_version": 1,
                "draft_updated_by_account_id": owner,
                "draft_updated_at": now,
                "made_in": made_in,
                "converted_from": converted,
                "created_at": now,
                "updated_at": now,
            },
        ),
        Write(
            "workflow_versions",
            "insert",
            {
                "workflow_id": flow,
                "number": 1,
                "tenant_id": snap.tenant_id,
                "spec": spec,
                "spec_hash": spec_hash(spec),
                "note": NOTE,
                "source": "routine",
                "published_by_account_id": owner,
                "published_at": now,
            },
        ),
        Write(
            "workflow_triggers",
            "insert",
            {
                "id": made["trigger"],
                "tenant_id": snap.tenant_id,
                "workflow_id": flow,
                "trigger_key": TRIGGER,
                "kind": "schedule",
                "config": {"spec": spec["triggers"][0]},
                "next_fire_at": snap.next_fire_at or snap.next_slot,
                "last_fired_slot": snap.last_fired_slot,
                "seeded_after": snap.seeded_after if snap.next_fire_at else now,
                "created_at": now,
                "updated_at": now,
            },
        ),
    ]
    if snap.sandbox:
        notes.append("built-ins grant")
        pins = {"published_by": None if owner is None else str(owner)}
        grant = {"id": made["grant"], "tenant_id": snap.tenant_id, "workflow_id": flow}
        grant |= {"version": 1, "kind": "builtins", "node_id": STEP, "tool": None, "params": {}}
        grant |= {"pins": pins | {"node_hash": spec_hash(spec["nodes"][STEP])}}
        grant |= {"approved_by_account_id": snap.sandbox_set_by, "approved_at": now}
        writes.append(Write("workflow_grants", "insert", grant))
    if snap.header:
        notes.append("header dropped")
    if snap.delivery_status == "pending":
        notes.append("1 pending post")
        text, files = _pending(snap)
        payload: dict[str, Any] = {
            "text": text,
            "files": files,
            "workflow": spec["name"],
            "run_number": None,
            "speaker": None if owner is None else str(owner),
            "mention": [],
            # Only for moving back: a workflow post's hold comes from where it goes.
            "hold": snap.delivery_hold,
            "note": snap.delivery_note,
        }
        target = (
            {"kind": "dm", "account_id": payload["speaker"], "via": snap.channel_id}
            if to is None or to["kind"] == "dm"
            else _place(to["id"], snap.channel_id)
        )
        writes.append(_delivery(made["post"], snap, "post", target, payload, now))
    if snap.owner_notice is not None:
        notes.append("owner notice")
        notice = snap.owner_notice
        text = notice.get("text") or f"{notice.get('title', '')}: {notice.get('body', '')}"
        payload = {"topic": "routine_failed", "text": text, "routine_notice": dict(notice)}
        writes.append(_delivery(made["notice"], snap, "notice", {"to": "owner"}, payload, now))
    if snap.intake is not None:
        notes.insert(0, "intake binding")
        config = {**snap.intake, "routine_id": None, "workflow_id": str(flow)}
        writes.append(
            Write(
                "lead_intake_configs", "update", {"config": config}, {"tenant_id": snap.tenant_id}
            )
        )
    writes.append(Write("routines", "delete", where={"id": snap.id}))
    return Move(
        flow, "exact" if active else "paused", tuple(notes), spec, spec_hash(spec), tuple(writes)
    )


def _delivery(
    id_: uuid.UUID,
    snap: RoutineSnapshot,
    kind: str,
    target: dict[str, Any],
    payload: dict[str, Any],
    now: datetime,
) -> Write:
    values = {"id": id_, "tenant_id": snap.tenant_id, "workflow_id": ids(snap.id)["workflow"]}
    values |= {"kind": kind, "target": target, "payload": payload, "note": None, "created_at": now}
    return Write("workflow_deliveries", "insert", values)


def _untitled(spec: Mapping[str, Any]) -> dict[str, Any]:
    nodes: dict[str, Any] = dict(spec.get("nodes") or {})
    plain = {k: {f: v for f, v in n.items() if f != "title"} for k, n in nodes.items()}
    return {**spec, "nodes": plain}


def _schedules(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Its schedule triggers when the rest are manual, else none."""
    triggers: list[dict[str, Any]] = list(spec.get("triggers") or [])
    if any(t.get("kind") not in ("schedule", "manual") for t in triggers):
        return []
    return [t for t in triggers if t.get("kind") == "schedule"]


def _same_shape(snap: WorkflowSnapshot, cf: Mapping[str, Any], prompt: str) -> bool:
    """Exactly its routine's shape (A-D) for the routine's agent and destination, titles
    aside. Built-ins are not: a routine's sandbox opt-in cannot come back."""
    spec, (trigger,) = snap.spec, _schedules(snap.spec)
    expected = _spec(
        str(spec.get("name")),
        agent=str(cf.get("agent_name")),
        prompt=prompt,
        trigger=_trigger(trigger["cron"], trigger.get("timezone"), trigger.get("catch_up")),
        home=snap.home_channel_id,
        to=cf.get("to"),
        sandbox=False,
    )
    return _untitled({**spec, "triggers": [trigger]}) == _untitled(expected)


def untranslate(snap: WorkflowSnapshot, *, now: datetime) -> Move | Refusal:
    """The routine a converted workflow was, from the workflow as it is now; the workflow goes.
    Only one plain cron schedule (`not_a_routine` else), its routine's shape, agent,
    destination and owner, and no run or post under way."""
    cf = snap.converted_from
    schedules = _schedules(snap.spec)
    trigger = schedules[0] if len(schedules) == 1 else {}
    if cf is None or not trigger.get("cron") or set(trigger) - _SCHEDULE_KEYS:
        return Refusal("not_a_routine")
    nodes: dict[str, Any] = dict(snap.spec.get("nodes") or {})
    run: dict[str, Any] = nodes.get(STEP) or {}
    prompt = run.get("instructions")
    owner = cf.get("owner_account_id")
    if (
        not isinstance(prompt, str)
        or not _same_shape(snap, cf, prompt)
        or str(snap.owner_account_id) != str(owner)
        or str(snap.published_by) != str(owner)
    ):
        return Refusal("changed")
    if snap.active_runs or any(d.status == "claimed" for d in snap.deliveries):
        return Refusal("busy")
    routine_id = uuid.UUID(cf["routine_id"])
    enabled = snap.status == "active"
    to = cf.get("to")
    made_in = snap.made_in or {}
    source = cf.get("source_routine_id")
    error = cf.get("error")  # the failure streak: its last run's since the move, if any
    if snap.last_status is not None:
        error = None if snap.last_status == "succeeded" else snap.last_error or "The run failed."
    values: dict[str, Any] = {
        "id": routine_id,
        "tenant_id": snap.tenant_id,
        "created_by_user_id": cf.get("owner"),
        "agent_id": cf["agent_id"],
        "agent_name": cf["agent_name"],
        "name": routine_name(snap.name, cf, prompt),
        "cron_expr": trigger["cron"],
        "timezone": trigger.get("timezone") or "UTC",
        "trigger_message": prompt,
        "enabled": enabled,
        "catch_up_policy": trigger.get("catch_up") or "skip",
        "next_fire_at": snap.next_fire_at if enabled else None,
        "source_routine_id": None if source is None else uuid.UUID(source),
        "seeded_after": snap.seeded_after,
        "last_fired_at": _at(cf.get("fired_at")),
        "last_fired_slot": snap.last_fired_slot,
        "last_finished_at": _at(cf.get("finished_at")),
        "last_error": error,
        "last_result_tail": cf.get("tail"),
        "destination_kind": None if to is None else to["kind"],
        "destination_id": None if to is None else to.get("id", cf.get("owner")),
        "channel_id": snap.home_channel_id,
        "made_in_channel_id": cf.get("made_in_channel_id"),
        "source_provenance_known": bool(made_in.get("known")),
        "source_channel_id": made_in.get("channel_id"),
        "source_thread_id": made_in.get("thread_id"),
        "created_at": _at(cf.get("created_at")) or now,
        "updated_at": now,
    }
    notes: list[str] = [] if enabled else ["disabled"]
    posts = [d for d in snap.deliveries if d.kind == "post" and d.status == "pending"]
    if posts:
        notes.append("1 pending post")
        payload = posts[-1].payload
        text, files = str(payload.get("text") or ""), list(payload.get("files") or [])
        note = payload.get("note", OUTBOX_FILES if files else None)
        if note == OUTBOX_FILES:
            body = json.dumps(
                {"text": text, "files": files}, separators=(",", ":"), ensure_ascii=False
            )
            text = FILES_PREFIX + body
        values |= {"delivery_status": "pending", "delivery_payload": text}
        values |= {"delivery_note": note, "delivery_hold": payload.get("hold")}
    notices = [d for d in snap.deliveries if d.kind == "notice" and d.status == "pending"]
    if notices:
        notes.append("owner notice")
        payload = notices[-1].payload
        text = payload.get("text")
        values["owner_notice"] = payload.get("routine_notice") or {
            "title": "",
            "body": text,
            "text": text,
        }
    writes = [Write("routines", "insert", values)]
    if snap.intake is not None:
        notes.insert(0, "intake binding")
        config = {**snap.intake, "workflow_id": None, "routine_id": str(routine_id)}
        writes.append(
            Write(
                "lead_intake_configs", "update", {"config": config}, {"tenant_id": snap.tenant_id}
            )
        )
    writes.append(Write("workflows", "delete", where={"id": snap.id}))
    outcome: Literal["exact", "paused"] = "exact" if enabled else "paused"
    return Move(
        routine_id, outcome, tuple(notes), dict(snap.spec), spec_hash(snap.spec), tuple(writes)
    )


def _at(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def apply_move(conn: sa.Connection, move: Move) -> None:
    """Run a move's writes in order on `conn`, inside the caller's transaction. Values bind as
    `json`, which keeps a spec's key order in its `json` columns."""
    for write in move.writes:
        cols = {**write.values, **write.where}
        table = sa.table(
            write.table,
            *(
                sa.column(k, sa.JSON() if isinstance(v, dict | list) else None)
                for k, v in cols.items()
            ),
        )
        match = [table.c[k] == v for k, v in write.where.items()]
        if write.op == "insert":
            stmt: sa.Executable = sa.insert(table).values(**write.values)
        elif write.op == "update":
            stmt = sa.update(table).where(*match).values(**write.values)
        else:
            stmt = sa.delete(table).where(*match)
        conn.execute(stmt)
