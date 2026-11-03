"""`/api/admin/audit`: what changed in the workspace, from every audit source."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from daimon.adapters.web.admin_common import AuditViewer
from daimon.adapters.web.channel_names import channel_names
from daimon.adapters.web.paging import PageRequest, cut, page_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import audit_trail
from daimon.core.audit_trail import AuditEntry, AuditFilter, AuditSource
from daimon.core.stores import web_invites
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import WebAuditRow, WebPersonRow
from daimon.core.stores.security_audit import SecurityAuditRow
from fastapi import APIRouter, Depends, Query, Response

_OUTCOMES = {"allowed": "Allowed", "denied": "Refused", "error": "Failed"}


def _words(code: str) -> str:
    return code.replace("_", " ").capitalize() if code else code


def _kind_label(kind: str) -> str:
    """A tool name as a person reads it; panel writes are `panel:<operation>`."""
    head, _, op = kind.partition(":")
    return f"Panel: {_words(op).lower()}" if head == "panel" and op else _words(kind)


def _event_label(event: SecurityAuditRow) -> str:
    base = _kind_label(event.tool_name)
    if event.operation and not event.tool_name.startswith("panel:"):
        return f"{base}: {event.operation.replace('_', ' ')}"
    return base


def _event_facts(event: SecurityAuditRow) -> list[list[str]]:
    """The fields the event stores that a person can read; no ids of people, no hashes."""
    stored = [
        ("Where", "Panel" if event.tool_name.startswith("panel:") else "Chat tool"),
        ("Tool", _kind_label(event.tool_name)),
        ("Operation", _words(event.operation or "")),
        ("Outcome", _OUTCOMES[event.outcome]),
        ("Reason", event.reason),
        ("Platform", event.platform or ""),
        ("Agent", event.agent_name or ""),
        ("Token", event.token_kind or ""),
        ("Scope", event.scope or ""),
    ]
    return [[label, value] for label, value in stored if value]


def _reference(event: SecurityAuditRow) -> str | None:
    """The message and turn ids, for support to look the event up; not shown, only copied."""
    lines = [
        f"{label} {value}"
        for label, value in (("Message", event.target_message_id), ("Turn", event.turn_ref))
        if value
    ]
    return "\n".join(lines) or None


def audit_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    @router.get("/audit")
    async def audit_log(  # pyright: ignore[reportUnusedFunction]
        caller: AuditViewer,
        response: Response,
        page: Annotated[PageRequest, Depends(page_of(100))],
        source: AuditSource | None = None,
        actor: Annotated[uuid.UUID | None, Query(description="A person's id")] = None,
        kind: Annotated[str | None, Query(max_length=200)] = None,
        first: Annotated[date | None, Query(alias="from")] = None,
        last: Annotated[date | None, Query(alias="to")] = None,
    ) -> list[dict[str, Any]]:
        tenant_id = caller.person.tenant_id
        since, until = audit_trail.day_bounds(first, last)
        async with runtime.sessionmaker() as db:
            account: uuid.UUID | None = None
            if actor is not None:
                chosen = next(
                    (
                        p
                        for p in await people_store.get_people(db, person_ids=[actor])
                        if p.tenant_id == tenant_id
                    ),
                    None,
                )
                if chosen is None:
                    return []
                account = chosen.account_id
            query = AuditFilter(
                source=source, actor_account_id=account, kind=kind or None, since=since, until=until
            )
            found = await audit_trail.list_entries(
                db, tenant_id=tenant_id, query=query, before=page.cursor, limit=page.limit + 1
            )
            entries = cut(found, page, response, lambda e: e.cursor)
            workspace = await people_store.list_people(db, tenant_id=tenant_id)
            web_rows = [e.row for e in entries if isinstance(e.row, WebAuditRow)]
            # People and invites are shown as they are now; an erased one falls back to
            # the row's generic label.
            people = await people_store.get_people(
                db, person_ids=[r.target_person_id for r in web_rows if r.target_person_id]
            )
            emails = await web_invites.emails_by_id(
                db,
                tenant_id=tenant_id,
                invite_ids=[r.target_invite_id for r in web_rows if r.target_invite_id],
            )
        person_names = {p.id: p.display_name for p in people if p.tenant_id == tenant_id}
        actors = {p.account_id: p for p in workspace}
        channel_ids = {
            e.row.target_channel_id
            for e in entries
            if isinstance(e.row, SecurityAuditRow) and e.row.target_channel_id
        }
        labels: dict[str, str] = {}
        if channel_ids:
            _, found = await channel_names(runtime, tenant_id, channel_ids)
            labels = found.labels()
        return [_shown(e, actors, person_names, emails, labels) for e in entries]

    @router.get("/audit/kinds")
    async def audit_kinds(  # pyright: ignore[reportUnusedFunction]
        caller: AuditViewer,
    ) -> list[dict[str, str]]:
        async with runtime.sessionmaker() as db:
            kinds = await audit_trail.list_kinds(db, tenant_id=caller.person.tenant_id)
        return [
            {
                "source": k.source,
                "value": k.kind,
                "label": _kind_label(k.kind) if k.source == "security" else k.kind,
            }
            for k in kinds
        ]

    return router


def _target(
    row: WebAuditRow, person_names: dict[uuid.UUID, str], emails: dict[uuid.UUID, str]
) -> dict[str, str | None]:
    """What the change was about, as the app links it: a person by id; an invite by address;
    anything else, or someone erased, by its generic label."""
    if row.target_person_id and (name := person_names.get(row.target_person_id)):
        return {"kind": "person", "id": str(row.target_person_id), "name": name}
    if row.target_invite_id and (email := emails.get(row.target_invite_id)):
        return {"kind": "invite", "id": str(row.target_invite_id), "name": email}
    return {"kind": "other", "id": None, "name": row.target}


def _shown(
    entry: AuditEntry,
    actors: dict[uuid.UUID, WebPersonRow],
    person_names: dict[uuid.UUID, str],
    emails: dict[uuid.UUID, str],
    channels: dict[str, str],
) -> dict[str, Any]:
    row = entry.row
    actor = actors.get(entry.actor_account_id) if entry.actor_account_id else None
    who = actor.display_name if actor else None
    if isinstance(row, WebAuditRow):
        out: dict[str, Any] = {
            "id": str(row.id),
            "source": entry.source,
            "at": row.at.isoformat(),
            "who": who or ("Daimon" if row.actor_account_id is None else "Someone"),
            "actor_id": str(actor.id) if actor else None,
            "action": row.action,
            "target": _target(row, person_names, emails),
        }
        if row.detail is not None:
            out["detail"] = row.detail
        return out
    return {
        "id": str(row.id),
        "source": entry.source,
        "at": row.occurred_at.isoformat(),
        "who": who or row.agent_name or "Someone",
        "actor_id": str(actor.id) if actor else None,
        "action": _event_label(row),
        "target": {
            "kind": "other",
            "id": None,
            "name": row.agent_name if who and row.agent_name else "",
        },
        "detail": _OUTCOMES[row.outcome],
        "facts": _event_facts(row),
        "channel": {"id": row.target_channel_id, "name": channels.get(row.target_channel_id)}
        if row.target_channel_id
        else None,
        "reference": _reference(row),
    }
