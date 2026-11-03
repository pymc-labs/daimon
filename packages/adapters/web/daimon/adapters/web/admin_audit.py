"""`/api/admin/audit`: what changed in the workspace, from every audit source."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from daimon.adapters.web.admin_common import AuditViewer, names_by_account
from daimon.adapters.web.paging import PageRequest, cut, page_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import audit_trail
from daimon.core.audit_trail import AuditEntry, AuditFilter, AuditSource
from daimon.core.stores import web_invites
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import WebAuditRow
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
        ("Channel", event.target_channel_id or ""),
        ("Message", event.target_message_id or ""),
        ("Turn", event.turn_ref or ""),
    ]
    return [[label, value] for label, value in stored if value]


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
            names = await names_by_account(db, tenant_id)
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
        return [_shown(e, names, person_names, emails) for e in entries]

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


def _shown(
    entry: AuditEntry,
    names: dict[uuid.UUID, str],
    person_names: dict[uuid.UUID, str],
    emails: dict[uuid.UUID, str],
) -> dict[str, Any]:
    row = entry.row
    who = names.get(entry.actor_account_id) if entry.actor_account_id else None
    if isinstance(row, WebAuditRow):
        shown = (person_names.get(row.target_person_id) if row.target_person_id else None) or (
            emails.get(row.target_invite_id) if row.target_invite_id else None
        )
        out: dict[str, Any] = {
            "id": str(row.id),
            "source": entry.source,
            "at": row.at.isoformat(),
            "who": who or ("Daimon" if row.actor_account_id is None else "Someone"),
            "action": row.action,
            "target": shown or row.target,
        }
        if row.detail is not None:
            out["detail"] = row.detail
        return out
    return {
        "id": str(row.id),
        "source": entry.source,
        "at": row.occurred_at.isoformat(),
        "who": who or row.agent_name or "Someone",
        "action": _event_label(row),
        "target": row.agent_name if who and row.agent_name else "",
        "detail": _OUTCOMES[row.outcome],
        "facts": _event_facts(row),
    }
