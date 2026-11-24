"""`/api/artifacts/:id/consent` and `.../versions/:v/thumbnail`: a viewer's own consent
to a page's asks, and the version thumbnails the list and chat cards show.

A thumbnail is captured in a manager's browser while they preview the version (the shell
asks the page's runtime for it, only for a page with no data and no asks, which the
server checks again), so only managers may set one. Anyone who can open the page sees
the current version's; older ones are for managers.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from daimon.adapters.web.caller import WebCaller, current_caller, requires
from daimon.adapters.web.runtime import WebRuntime
from daimon.adapters.web.viewer import page_on, viewer_of
from daimon.core.artifact_pages.access import admit, can_edit, role_for
from daimon.core.artifact_pages.content import MAX_THUMBNAIL_BYTES, check_thumbnail
from daimon.core.artifact_pages.model import ArtifactError
from daimon.core.stores import artifact_consents as consents_store
from daimon.core.stores import artifact_pages as artifact_store
from daimon.core.stores import artifact_thumbnails as thumbnails_store
from daimon.core.stores.domain import ArtifactRow
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

Viewer = Annotated[WebCaller, Depends(requires("artifacts.view"))]
Person = Annotated[WebCaller, Depends(current_caller)]
_NOT_FOUND = "This page doesn't exist or isn't shared with you."


def artifact_viewing_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/artifacts/{artifact_id}")

    async def load(db: AsyncSession, caller: WebCaller, artifact_id: uuid.UUID) -> ArtifactRow:
        artifact = await artifact_store.get(db, artifact_id)
        if artifact is None or artifact.tenant_id != caller.person.tenant_id:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return artifact

    async def manages(db: AsyncSession, caller: WebCaller, artifact: ArtifactRow) -> bool:
        """Whether they manage it; 404 unless they may at least open it."""
        viewer = await viewer_of(db, caller)
        manager = can_edit(role_for(artifact, viewer))
        if not manager and admit(artifact, viewer, runtime.now()) == "none":
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        if not await page_on(runtime, db, artifact):
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        return manager

    @router.get("/consent")
    async def get_consent(artifact_id: uuid.UUID, caller: Person) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        async with runtime.pages_db() as db:
            artifact = await load(db, caller, artifact_id)
            allowed = await consents_store.has_consent(db, artifact, caller.person.id)
        return {"allowed": allowed}

    @router.delete("/consent", status_code=204)
    async def revoke_consent(artifact_id: uuid.UUID, caller: Person) -> Response:  # pyright: ignore[reportUnusedFunction]
        """The page asks again before its next question."""
        async with runtime.pages_db.begin() as db:
            artifact = await load(db, caller, artifact_id)
            await consents_store.delete_consent(db, artifact.id, caller.person.id)
        return Response(status_code=204)

    @router.get("/versions/{version}/thumbnail")
    async def get_thumbnail(artifact_id: uuid.UUID, version: int, caller: Viewer) -> Response:  # pyright: ignore[reportUnusedFunction]
        async with runtime.pages_db() as db:
            artifact = await load(db, caller, artifact_id)
            manager = await manages(db, caller, artifact)
            if not manager and version != artifact.current_version:
                raise HTTPException(status_code=404, detail="No thumbnail yet.")
            png = await thumbnails_store.get_thumbnail(db, artifact.id, version)
        if png is None:
            raise HTTPException(status_code=404, detail="No thumbnail yet.")
        return Response(
            png,
            media_type="image/png",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"},
        )

    @router.put("/versions/{version}/thumbnail", status_code=204)
    async def put_thumbnail(  # pyright: ignore[reportUnusedFunction]
        artifact_id: uuid.UUID, version: int, request: Request, caller: Viewer
    ) -> Response:
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_THUMBNAIL_BYTES:
                break
        try:
            check_thumbnail(bytes(body))
        except ArtifactError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        async with runtime.pages_db.begin() as db:
            artifact = await load(db, caller, artifact_id)
            if not await manages(db, caller, artifact):
                raise HTTPException(status_code=403, detail="Only those who manage a page set it.")
            if artifact.data != "off" or artifact.ask:
                raise HTTPException(
                    status_code=409, detail="A page that shows data or asks has no thumbnail."
                )
            if await artifact_store.get_version(db, artifact.id, version) is None:
                raise HTTPException(status_code=404, detail=f"No version {version}.")
            await thumbnails_store.upsert_thumbnail(db, artifact, version, bytes(body))
        return Response(status_code=204)

    return router
