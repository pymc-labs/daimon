"""`/api/admin/audit`: the workspace's change log."""

from __future__ import annotations

from typing import Annotated, Any

from daimon.adapters.web.admin_common import AuditViewer, names_by_account
from daimon.adapters.web.paging import PageRequest, cut, page_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.stores import web_audit, web_invites
from daimon.core.stores import web_people as people_store
from fastapi import APIRouter, Depends, Response


def audit_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/admin")

    @router.get("/audit")
    async def audit_log(  # pyright: ignore[reportUnusedFunction]
        caller: AuditViewer,
        response: Response,
        page: Annotated[PageRequest, Depends(page_of(100))],
    ) -> list[dict[str, Any]]:
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            found = await web_audit.list_recent(
                db, tenant_id=tenant_id, limit=page.limit + 1, before=page.cursor
            )
            rows = cut(found, page, response, lambda r: (r.at, r.id))
            names = await names_by_account(db, tenant_id)
            # People and invites are shown as they are now; an erased one falls back to
            # the row's generic label.
            people = await people_store.get_people(
                db, person_ids=[r.target_person_id for r in rows if r.target_person_id]
            )
            emails = await web_invites.emails_by_id(
                db,
                tenant_id=tenant_id,
                invite_ids=[r.target_invite_id for r in rows if r.target_invite_id],
            )
        person_names = {p.id: p.display_name for p in people if p.tenant_id == tenant_id}
        out: list[dict[str, Any]] = []
        for row in rows:
            who = names.get(row.actor_account_id) if row.actor_account_id else None
            shown = (person_names.get(row.target_person_id) if row.target_person_id else None) or (
                emails.get(row.target_invite_id) if row.target_invite_id else None
            )
            entry: dict[str, Any] = {
                "id": str(row.id),
                "at": row.at.isoformat(),
                "who": who or ("Daimon" if row.actor_account_id is None else "Someone"),
                "action": row.action,
                "target": shown or row.target,
            }
            if row.detail is not None:
                entry["detail"] = row.detail
            out.append(entry)
        return out

    return router
