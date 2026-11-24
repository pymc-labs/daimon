"""`/api/artifacts/*`: the pages a person can see, their sharing, versions, data and events.

Every rule is `daimon.core.artifact_pages`'; this module reads and shapes. A public page
is listed only to those who manage it; a page nobody may see here is not found.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Annotated, Any
from urllib.parse import quote

from daimon.adapters.web.admin_common import listed_agents
from daimon.adapters.web.audit import audit, person_target
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.cookies import token_sha256
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.invites import SIGN_IN_LINK_TTL, issue_link, join_invite, link_url
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
    role_for,
)
from daimon.core.artifact_pages.documents import (
    EVENTS_COLLECTION,
    SERVER_PREFIX,
    DocumentWrite,
    build_list_query,
    check_page_collection,
)
from daimon.core.artifact_pages.events import describe_where
from daimon.core.artifact_pages.model import ArtifactError, ErrorCode, page_url
from daimon.core.artifact_pages.standing import outsider_accounts
from daimon.core.permissions import WEB_PLACE_PREFIX
from daimon.core.stores import artifact_pages as artifact_store
from daimon.core.stores import watches as watches_store
from daimon.core.stores import web_invites
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
    "not_allowed": 403,
    "unavailable": 503,
}
_NOT_FOUND = "This page doesn't exist or isn't shared with you."
_DOCS_PAGE = 200
_EVENTS_SHOWN = 200
_WATCH_NAME = 60
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _refused(error: ArtifactError) -> HTTPException:
    return HTTPException(status_code=_STATUS[error.code], detail=str(error))


def _seen_as(artifact: ArtifactRow, viewer: Viewer, now: datetime) -> Role:
    """The role a person has here; `none` hides the page. Managers see their pages even
    expired; anyone else as the page rules admit them, never a public page."""
    role = role_for(artifact, viewer)
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
    limits = runtime.settings.artifact_pages
    docs_page, events_shown = (min(n, limits.list_documents) for n in (_DOCS_PAGE, _EVENTS_SHOWN))

    async def agents_of(tenant_id: uuid.UUID) -> dict[str, dict[str, Any]]:
        listed = await listed_agents(runtime, tenant_id)
        avatars = await avatars_of(runtime, tenant_id, listed)
        return {a.id: {"id": a.id, "name": a.name, "avatar": avatars.get(a.id)} for a in listed}

    async def as_pages_see(
        db: AsyncSession, tenant_id: uuid.UUID, found: Collection[WebPersonRow]
    ) -> list[WebPersonRow]:
        """The people as pages count them: an outsider is a guest, not a member."""
        outsiders = await outsider_accounts(
            db, tenant_id=tenant_id, account_ids={p.account_id for p in found if p.kind == "member"}
        )
        return [
            p.model_copy(update={"kind": "visitor"}) if p.account_id in outsiders else p
            for p in found
        ]

    async def people_of(
        db: AsyncSession, tenant_id: uuid.UUID, account_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, WebPersonRow]:
        found = await people_store.get_people_for_accounts(
            db, tenant_id=tenant_id, account_ids=set(account_ids)
        )
        return {p.account_id: p for p in await as_pages_see(db, tenant_id, list(found.values()))}

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
        role = _seen_as(artifact, await viewer_of(db, caller), runtime.now())
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
            "collection_rules": {k: v.model_dump() for k, v in artifact.collection_rules.items()},
            "link": link,
            "usage": {
                "documents": usage.documents,
                "max_documents": limits.documents * artifact.data_scale,
                "bytes": usage.bytes,
                "max_bytes": limits.data_bytes * artifact.data_scale,
            },
            "watches": [
                {
                    "id": str(w.watch.id),
                    "name": w.watch.instructions.strip().splitlines()[0][:_WATCH_NAME]
                    if w.watch.instructions.strip()
                    else "Watch",
                    "owner": _person(w.watch.owner_account_id, people)["name"],
                    "mine": w.watch.owner_account_id == caller.person.account_id,
                    "agent": w.watch.agent_name,
                    "status": "paused"
                    if w.watch.status == "paused" or w.watch.paused_at
                    else "active",
                    "turns_today": w.fires_today,
                    "max_turns_per_day": w.watch.max_turns_per_day,
                    "where": describe_where(w.watch.source_state.get("where")),
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
        now, tenant_id = runtime.now(), caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            viewer = await viewer_of(db, caller)
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
            found = await as_pages_see(db, caller.person.tenant_id, found)
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
                    db,
                    artifact,
                    wanted,
                    role=role,
                    account_id=caller.person.account_id,
                    now=runtime.now(),
                    is_admin=(await viewer_of(db, caller)).is_admin,
                )
            except ArtifactError as exc:
                raise _refused(exc) from exc
            return await detail(db, artifact, role, caller)

    @router.post("/{artifact_id}/people")
    async def share_by_email(  # pyright: ignore[reportUnusedFunction]
        artifact_id: uuid.UUID,
        caller: Caller,
        email: Annotated[str, Body(embed=True, max_length=254)],
    ) -> dict[str, Any]:
        """Add the workspace's person with this email, or a new visitor who can open only
        the pages shared with them. A new visitor's sign-in link is returned once; an
        existing person gets none, since it would sign in as them. With Google sign-in on,
        a new visitor gets none either: they sign in with the Google account of that email."""
        address, now = email.strip().lower(), runtime.now()
        if not _EMAIL.fullmatch(address):
            raise HTTPException(status_code=422, detail="Enter an email address.")
        async with runtime.sessionmaker.begin() as db:
            artifact, role = await load(db, caller, artifact_id)
            require(can_share(artifact, role))
            if artifact.access != "people":
                raise HTTPException(status_code=422, detail="Share with specific people first.")
            tenant_id, sharer = artifact.tenant_id, caller.person.account_id
            await web_invites.lock_visitor_invites_by(db, tenant_id=tenant_id, account_id=sharer)
            found = [
                p
                for p in await people_store.list_people(db, tenant_id=tenant_id, search=address)
                if (p.email or "").lower() == address
            ]
            link = None
            if found:
                person = found[0]
                if person.status != "active":
                    raise HTTPException(status_code=422, detail="This person can't be added.")
            else:
                made = await web_invites.count_visitor_invites_by(
                    db, tenant_id=tenant_id, account_id=sharer, since=now - timedelta(days=1)
                )
                if made >= limits.shares_per_day:
                    raise HTTPException(
                        status_code=429, detail="You've invited many people today. Try tomorrow."
                    )
                invite, token = await issue_link(
                    db,
                    tenant_id=tenant_id,
                    kind="invite",
                    expires_at=now + SIGN_IN_LINK_TTL,
                    email=address,
                    created_by_account_id=sharer,
                )
                claimed = await web_invites.claim_invite(
                    db, token_sha256=token_sha256(token), now=now
                )
                assert claimed is not None, "a link just made is open"
                person = await join_invite(db, claimed, now)
                await web_invites.set_accepted_by(db, invite_id=invite.id, person_id=person.id)
                if not runtime.web.google_configured:
                    _, token = await issue_link(
                        db,
                        tenant_id=tenant_id,
                        kind="sign_in",
                        expires_at=now + SIGN_IN_LINK_TTL,
                        email=address,
                        person_id=person.id,
                        created_by_account_id=sharer,
                    )
                    page = page_url("", artifact.public_id, artifact.slug)
                    link = f"{link_url(runtime.web, token)}?next={quote(page, safe='')}"
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=sharer,
                    action="Invited by email to a page",
                    target=person_target(person.id),
                    at=now,
                )
            if person.account_id not in artifact.people:
                try:
                    artifact = await artifact_store.update_settings(
                        db,
                        artifact,
                        PageChange(people=(*artifact.people, person.account_id)),
                        role=role,
                        account_id=sharer,
                        now=now,
                        is_admin=(await viewer_of(db, caller)).is_admin,
                    )
                except ArtifactError as exc:
                    raise _refused(exc) from exc
            return {**await detail(db, artifact, role, caller), "invite_link": link}

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
                    limits=limits,
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
                    check_page_collection(collection), limit=docs_page, after=after, limits=limits
                )
            except ArtifactError as exc:
                raise _refused(exc) from exc
            found = await artifact_store.list_docs(db, artifact.id, query, limits=limits)
        headers = {"X-Next-Cursor": found[-1].doc_id} if len(found) == docs_page else {}
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
                    rule=None,
                    now=runtime.now(),
                    limits=limits,
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
                    db,
                    artifact,
                    check_page_collection(collection),
                    doc_id,
                    rule=None,
                    account_id=None,
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
                EVENTS_COLLECTION, order_by="at", descending=True, limit=events_shown, limits=limits
            )
            found = await artifact_store.list_docs(db, artifact.id, query, limits=limits)
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
