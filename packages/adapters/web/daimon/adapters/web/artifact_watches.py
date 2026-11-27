"""Manage the watches on a page from its Activity tab: whoever manages the page may pause
or delete any of them, an owner their own; only a watch's owner may resume it."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from daimon.adapters.web.caller import WebCaller, artifacts_here, requires
from daimon.adapters.web.runtime import WebRuntime
from daimon.adapters.web.viewer import page_on, viewer_of
from daimon.core.artifact_pages.access import admit, can_edit, role_for
from daimon.core.stores import artifact_pages as artifact_store
from daimon.core.stores import watches as watches_store
from daimon.core.stores.domain import WatchRow
from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

Caller = Annotated[WebCaller, Depends(requires("artifacts.view"))]
_GONE = "This watch doesn't exist or isn't yours."
_OWNER_ONLY = "Only the person who made this watch can resume it."


class WatchAction(BaseModel):
    action: Literal["pause", "resume"]


def artifact_watches_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/artifacts", dependencies=[Depends(artifacts_here)])

    async def managed(
        db: AsyncSession, caller: WebCaller, artifact_id: uuid.UUID, watch_id: uuid.UUID
    ) -> tuple[WatchRow, bool]:
        """The page's watch and whether the caller owns it; a refusal unless they own it
        or manage the page, with the page still theirs to see."""
        artifact = await artifact_store.get(db, artifact_id)
        watch = await watches_store.get_watch(db, watch_id)
        if (
            artifact is None
            or watch is None
            or artifact.tenant_id != caller.person.tenant_id
            or watch.page_id != artifact.id
            or not await page_on(runtime, db, artifact)
        ):
            raise HTTPException(status_code=404, detail=_GONE)
        viewer = await viewer_of(db, caller)
        mine = watch.owner_account_id == caller.person.account_id
        manages = can_edit(role_for(artifact, viewer))
        if not manages and not (mine and admit(artifact, viewer, runtime.now()) != "none"):
            raise HTTPException(status_code=404, detail=_GONE)
        return watch, mine

    @router.post("/{artifact_id}/watches/{watch_id}", status_code=204)
    async def change(  # pyright: ignore[reportUnusedFunction]
        artifact_id: uuid.UUID,
        watch_id: uuid.UUID,
        caller: Caller,
        body: Annotated[WatchAction, Body()],
    ) -> Response:
        now = runtime.now()
        async with runtime.sessionmaker.begin() as db:
            watch, mine = await managed(db, caller, artifact_id, watch_id)
            if body.action == "pause":
                await watches_store.pause_watch(db, watch.id)
                return Response(status_code=204)
            if not mine:
                raise HTTPException(status_code=403, detail=_OWNER_ONLY)
            keys = tuple(k.get_secret_value() for k in runtime.settings.crypto.keys)
            try:
                await watches_store.ensure_page_key(db, watch, keys, now=now)
                await watches_store.resume_watch(db, watch.id, now=now)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail="This watch can't resume.") from exc
        return Response(status_code=204)

    @router.delete("/{artifact_id}/watches/{watch_id}", status_code=204)
    async def remove(artifact_id: uuid.UUID, watch_id: uuid.UUID, caller: Caller) -> Response:  # pyright: ignore[reportUnusedFunction]
        async with runtime.sessionmaker.begin() as db:
            watch, _ = await managed(db, caller, artifact_id, watch_id)
            await watches_store.delete_watch(db, watch.id, now=runtime.now())
        return Response(status_code=204)

    return router
