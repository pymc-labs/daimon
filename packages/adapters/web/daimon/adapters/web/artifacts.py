"""`/api/artifacts/*`: the pages a person can see, their sharing, versions, data and events.

Every rule is `daimon.core.artifact_pages`'; this module reads and shapes. A public page
is listed only to those who manage it; a page nobody may see here is not found.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime
from typing import Annotated, Any

from daimon.adapters.web.admin_common import listed_agents
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.adapters.web.viewer import page_on, viewer_of
from daimon.core.artifact_pages.access import (
    ASK_OWNER,
    PageChange,
    Role,
    Viewer,
    admit,
    can_delete,
    can_edit,
    can_share,
    role_of,
)
from daimon.core.artifact_pages.documents import (
    EVENTS_COLLECTION,
    SERVER_PREFIX,
    DocumentWrite,
    build_list_query,
    check_page_collection,
    max_bytes,
    max_documents,
)
from daimon.core.artifact_pages.model import ArtifactError, ErrorCode, page_url
from daimon.core.permissions import WEB_PLACE_PREFIX
from daimon.core.stores import artifact_pages as artifact_store
from daimon.core.stores import watches as watches_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import ArtifactDocumentRow, ArtifactRow, WebPersonRow
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import JsonValue, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

Caller = Annotated[WebCaller, Depends(requires("artifacts.view"))]
_STATUS: dict[ErrorCode, int] = {
    "invalid": 422,
    "too_large": 422,
    "unauthenticated": 401,
    "not_found": 404,
    "forbidden": 403,
    "rate_limited": 429,
    "unavailable": 503,
}
_NOT_FOUND = "This page doesn't exist or isn't shared with you."
_DOCS_PAGE = 200
_EVENTS_SHOWN = 200
_WATCH_NAME = 60


def _refused(error: ArtifactError) -> HTTPException:
    return HTTPException(status_code=_STATUS[error.code], detail=str(error))


def _seen_as(artifact: ArtifactRow, viewer: Viewer, now: datetime) -> Role:
    """The role a person has here; `none` hides the page. Managers see their pages even
    expired; anyone else as the page rules admit them, never a public page."""
    role = role_of(artifact, viewer.account_id, is_admin=viewer.is_admin)
    if can_edit(role):
        return role
    if artifact.access == "public" or admit(artifact, viewer, now) == "none":
        return "none"
    return "viewer"


def _person(account_id: uuid.UUID, people: dict[uuid.UUID, WebPersonRow]) -> dict[str, Any]:
    found = people.get(account_id)
    if found is None:
        return {"id": str(account_id), "name": "Someone", "kind": "member"}
    return {"id": str(account_id), "name": found.display_name, "kind": found.kind}


def _doc_json(doc: ArtifactDocumentRow) -> dict[str, Any]:
    return {
        "id": doc.doc_id,
        "data": doc.data,
        "updated_at": doc.updated_at.isoformat(),
        "updated_by": doc.updated_by or "",
        "size_bytes": doc.size_bytes,
    }


def artifacts_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/artifacts")

    async def agents_of(tenant_id: uuid.UUID) -> dict[str, dict[str, Any]]:
        listed = await listed_agents(runtime, tenant_id)
        avatars = await avatars_of(runtime, tenant_id, listed)
        return {a.id: {"id": a.id, "name": a.name, "avatar": avatars.get(a.id)} for a in listed}

    async def people_of(
        db: AsyncSession, tenant_id: uuid.UUID, account_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, WebPersonRow]:
        return await people_store.get_people_for_accounts(
            db, tenant_id=tenant_id, account_ids=set(account_ids)
        )

    def summary(
        artifact: ArtifactRow,
        role: Role,
        caller: WebCaller,
        agents: dict[str, dict[str, Any]],
        people: dict[uuid.UUID, WebPersonRow],
    ) -> dict[str, Any]:
        removed = {"id": artifact.agent_id, "name": "Removed agent", "avatar": None}
        return {
            "id": str(artifact.id),
            "slug": artifact.slug,
            "title": artifact.title,
            "url": page_url("", artifact.public_id, artifact.slug),
            "agent": agents.get(artifact.agent_id, removed),
            "owner": _person(artifact.owner_account_id, people),
            "mine": artifact.owner_account_id == caller.person.account_id,
            "role": role,
            "access": artifact.access,
            "people_count": len(artifact.people),
            "data": artifact.data,
            "events": artifact.events,
            "ask": artifact.ask,
            "version": artifact.current_version,
            "size_bytes": artifact.size_bytes,
            "updated_at": artifact.updated_at.isoformat(),
            "created_at": artifact.created_at.isoformat(),
            "expires_at": None if artifact.expires_at is None else artifact.expires_at.isoformat(),
        }

    async def load(
        db: AsyncSession, caller: WebCaller, artifact_id: uuid.UUID
    ) -> tuple[ArtifactRow, Role]:
        artifact = await artifact_store.get(db, artifact_id)
        if artifact is None or artifact.tenant_id != caller.person.tenant_id:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        role = _seen_as(artifact, viewer_of(caller), runtime.now())
        if role == "none" or not await page_on(runtime, db, artifact):
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return artifact, role

    def require(allowed: bool) -> None:
        if not allowed:
            raise HTTPException(status_code=403, detail=ASK_OWNER)

    async def detail(
        db: AsyncSession, artifact: ArtifactRow, role: Role, caller: WebCaller
    ) -> dict[str, Any]:
        now, tenant_id = runtime.now(), artifact.tenant_id
        watches = [
            w
            for w in await watches_store.list_tenant_watches(db, tenant_id=tenant_id, now=now)
            if can_edit(role)  # viewers see the page's Details only
            and w.watch.page_id == artifact.id
            and w.watch.source == "page_event"
            and w.watch.status in ("active", "paused")
        ]
        people = await people_of(
            db,
            tenant_id,
            {artifact.owner_account_id, *artifact.people, *artifact.editors}
            | {w.watch.owner_account_id for w in watches},
        )
        usage = await artifact_store.usage(db, artifact.id)
        asks = await artifact_store.ask_stats(db, artifact.id, now=now)
        link = None
        if artifact.access == "public" and artifact.link_secret and can_edit(role):
            link = page_url(
                str(runtime.web.public_url), artifact.public_id, artifact.slug, artifact.link_secret
            )
        return {
            **summary(artifact, role, caller, await agents_of(tenant_id), people),
            "people": [_person(i, people) for i in artifact.people],
            "editors": [_person(i, people) for i in artifact.editors],
            "editors_can_share": artifact.editors_can_share,
            "data_scale": artifact.data_scale,
            "link": link,
            "usage": {
                "documents": usage.documents,
                "max_documents": max_documents(artifact.data_scale),
                "bytes": usage.bytes,
                "max_bytes": max_bytes(artifact.data_scale),
            },
            "watches": [
                {
                    "id": str(w.watch.id),
                    "name": w.watch.instructions.strip().splitlines()[0][:_WATCH_NAME]
                    if w.watch.instructions.strip()
                    else "Watch",
                    "owner": _person(w.watch.owner_account_id, people)["name"],
                    "agent": w.watch.agent_name,
                    "status": "paused"
                    if w.watch.status == "paused" or w.watch.paused_at
                    else "active",
                    "turns_today": w.fires_today,
                    "max_turns_per_day": w.watch.max_turns_per_day,
                    "thread_url": None,
                }
                for w in watches
            ],
            "asks": {"threads": asks.threads, "turns_7d": asks.turns_7d},
            "can": {
                "edit": can_edit(role),
                "share": can_share(artifact, role),
                "delete": can_delete(role),
                "transfer": can_delete(role),
            },
        }

    @router.get("")
    async def list_artifacts(caller: Caller) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        viewer, now, tenant_id = viewer_of(caller), runtime.now(), caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            found = await artifact_store.list_visible(
                db,
                tenant_id=tenant_id,
                account_id=caller.person.account_id,
                is_admin=viewer.is_admin,
            )
            on: dict[str, bool] = {}
            shown: list[tuple[ArtifactRow, Role]] = []
            for artifact in found:
                if artifact.agent_id not in on:
                    on[artifact.agent_id] = await page_on(runtime, db, artifact)
                role = _seen_as(artifact, viewer, now)
                if role != "none" and on[artifact.agent_id]:
                    shown.append((artifact, role))
            people = await people_of(db, tenant_id, {a.owner_account_id for a, _ in shown})
        agents = await agents_of(tenant_id)
        listed = [summary(a, role, caller, agents, people) for a, role in shown]
        return sorted(listed, key=lambda s: s["updated_at"], reverse=True)

    @router.get("/people")
    async def people(caller: Caller, q: str = Query("", max_length=100)) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        """The workspace's people the sharing pickers offer, by account."""
        async with runtime.sessionmaker() as db:
            found = await people_store.list_people(
                db, tenant_id=caller.person.tenant_id, limit=20, search=q.strip() or None
            )
        return [
            {"id": str(p.account_id), "name": p.display_name, "kind": p.kind}
            for p in found
            if p.status == "active"
        ]

    @router.get("/{artifact_id}")
    async def get_artifact(artifact_id: uuid.UUID, caller: Caller) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            artifact, role = await load(db, caller, artifact_id)
            return await detail(db, artifact, role, caller)

    @router.patch("/{artifact_id}")
    async def change(  # pyright: ignore[reportUnusedFunction]
        artifact_id: uuid.UUID, caller: Caller, body: Annotated[dict[str, Any], Body()]
    ) -> dict[str, Any]:
        try:
            wanted = PageChange.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="Check the page's settings.") from exc
        async with runtime.sessionmaker.begin() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_edit(role))
            added = {*(wanted.people or ()), *(wanted.editors or ())} - {
                *artifact.people,
                *artifact.editors,
            }
            known = await people_of(db, artifact.tenant_id, added)
            if any(i not in known or known[i].status != "active" for i in added):
                raise HTTPException(status_code=422, detail="Pick people from the list.")
            try:
                artifact = await artifact_store.update_settings(
                    db, artifact, wanted, role=role, now=runtime.now()
                )
            except ArtifactError as exc:
                raise _refused(exc) from exc
            return await detail(db, artifact, role, caller)

    @router.delete("/{artifact_id}", status_code=204)
    async def delete(artifact_id: uuid.UUID, caller: Caller) -> Response:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker.begin() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_delete(role))
            await artifact_store.delete_artifact(db, artifact, role=role, now=runtime.now())
        return Response(status_code=204)

    @router.post("/{artifact_id}/link")
    async def new_link(artifact_id: uuid.UUID, caller: Caller) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker.begin() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_share(artifact, role))
            try:
                artifact = await artifact_store.rotate_link(
                    db, artifact, role=role, now=runtime.now()
                )
            except ArtifactError as exc:
                raise _refused(exc) from exc
            return await detail(db, artifact, role, caller)

    @router.post("/{artifact_id}/transfer")
    async def transfer(  # pyright: ignore[reportUnusedFunction]
        artifact_id: uuid.UUID, caller: Caller, person: Annotated[uuid.UUID, Body(embed=True)]
    ) -> dict[str, Any]:
        async with runtime.sessionmaker.begin() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_delete(role))
            try:
                artifact = await artifact_store.transfer(
                    db, artifact, to_account_id=person, role=role, now=runtime.now()
                )
            except ArtifactError as exc:
                raise _refused(exc) from exc
            return await detail(db, artifact, role, caller)

    @router.get("/{artifact_id}/versions")
    async def versions(artifact_id: uuid.UUID, caller: Caller) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_edit(role))  # versions, data and events are for those who manage it
            found = await artifact_store.list_versions(db, artifact.id)
            people = await people_of(
                db,
                artifact.tenant_id,
                {v.published_by_account_id for v in found if v.published_by_account_id},
            )
        agents = await agents_of(artifact.tenant_id)

        def chat_url(platform: str | None, thread: str | None) -> str | None:
            if platform != "web" or not thread or not thread.startswith(WEB_PLACE_PREFIX):
                return None
            return f"/chat/{thread.removeprefix(WEB_PLACE_PREFIX)}"

        return [
            {
                "version": v.version,
                "created_at": v.created_at.isoformat(),
                "size_bytes": v.size_bytes,
                "by": "Someone"
                if v.published_by_account_id is None
                else _person(v.published_by_account_id, people)["name"],
                "agent": agents.get(v.agent_id, {"name": "Removed agent"})["name"],
                "current": v.version == artifact.current_version,
                "chat_url": chat_url(v.origin_platform, v.origin_thread_id),
            }
            for v in found
        ]

    @router.post("/{artifact_id}/versions/{version}/restore")
    async def restore(artifact_id: uuid.UUID, version: int, caller: Caller) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker.begin() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_edit(role))
            try:
                artifact = await artifact_store.restore(
                    db,
                    artifact,
                    version,
                    role=role,
                    account_id=caller.person.account_id,
                    now=runtime.now(),
                )
            except ArtifactError as exc:
                raise _refused(exc) from exc
            return await detail(db, artifact, role, caller)

    @router.get("/{artifact_id}/data")
    async def collections(artifact_id: uuid.UUID, caller: Caller) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_edit(role))
            found = await artifact_store.list_collections(db, artifact.id)
        return [
            {"name": c.name, "documents": c.documents, "bytes": c.bytes}
            for c in found
            if not c.name.startswith(SERVER_PREFIX)
        ]

    @router.get("/{artifact_id}/data/{collection}")
    async def documents(  # pyright: ignore[reportUnusedFunction]
        artifact_id: uuid.UUID, collection: str, caller: Caller, after: str | None = None
    ) -> JSONResponse:
        async with runtime.sessionmaker() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_edit(role))
            try:
                query = build_list_query(
                    check_page_collection(collection), limit=_DOCS_PAGE, after=after
                )
            except ArtifactError as exc:
                raise _refused(exc) from exc
            found = await artifact_store.list_docs(db, artifact.id, query)
        headers = {"X-Next-Cursor": found[-1].doc_id} if len(found) == _DOCS_PAGE else {}
        return JSONResponse([_doc_json(d) for d in found], headers=headers)

    @router.put("/{artifact_id}/data/{collection}/{doc_id}")
    async def put_document(  # pyright: ignore[reportUnusedFunction]
        artifact_id: uuid.UUID,
        collection: str,
        doc_id: str,
        caller: Caller,
        data: Annotated[dict[str, JsonValue], Body(embed=True)],
    ) -> dict[str, Any]:
        async with runtime.sessionmaker.begin() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_edit(role))
            try:
                saved, _ = await artifact_store.write_doc(
                    db,
                    artifact,
                    check_page_collection(collection),
                    doc_id,
                    DocumentWrite(data=data),
                    by=caller.person.display_name,
                    account_id=caller.person.account_id,
                    now=runtime.now(),
                )
            except ArtifactError as exc:
                raise _refused(exc) from exc
        return _doc_json(saved)

    @router.delete("/{artifact_id}/data/{collection}/{doc_id}", status_code=204)
    async def delete_document(  # pyright: ignore[reportUnusedFunction]
        artifact_id: uuid.UUID, collection: str, doc_id: str, caller: Caller
    ) -> Response:
        async with runtime.sessionmaker.begin() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_edit(role))
            try:
                await artifact_store.delete_doc(
                    db, artifact, check_page_collection(collection), doc_id
                )
            except ArtifactError as exc:
                raise _refused(exc) from exc
        return Response(status_code=204)

    @router.get("/{artifact_id}/events")
    async def events(artifact_id: uuid.UUID, caller: Caller) -> list[dict[str, Any]]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_edit(role))
            query = build_list_query(
                EVENTS_COLLECTION, order_by="at", descending=True, limit=_EVENTS_SHOWN
            )
            found = await artifact_store.list_docs(db, artifact.id, query)
        return [
            {
                "id": d.doc_id,
                "event": d.data.get("event"),
                "data": d.data.get("data"),
                "by": d.data.get("by"),
                "sender": d.data.get("sender"),
                "at": d.data.get("at"),
                "delivered": d.data.get("delivered", 0),
            }
            for d in found
        ]

    return router
