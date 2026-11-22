"""Who opens an artifact page: the web sign-in's person in the page's own workspace.

A sign-in can reach people in several workspaces; a page is opened as the one in its
workspace, with no switch of the workspace the app is on. Anyone else is anonymous, and
only a public page's link lets them in.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from daimon.adapters.web.caller import WebCaller, build_caller, signin_allowed_in, ttl_of
from daimon.adapters.web.cookies import session_token, token_sha256
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.artifact_pages.access import Viewer
from daimon.core.features.snapshot import FeaturePlace, feature_in
from daimon.core.stores import web_people as people_store
from daimon.core.stores import web_sessions as sessions_store
from daimon.core.stores.domain import ArtifactRow, WebPersonRow, WebSessionRow
from daimon.core.web_roles_rules import chat_standings, has_left, is_admin, roles_of
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class PageVisitor:
    """The viewer as the page rules see them; their caller when they are a person of the
    page's workspace; whether they are signed in at all."""

    viewer: Viewer
    caller: WebCaller | None = None
    signed_in: bool = False


def _viewer(person: WebPersonRow, *, admin: bool, link: str | None) -> Viewer:
    """Admins manage every page of their workspace."""
    return Viewer(
        account_id=person.account_id,
        person_id=person.id,
        name=person.display_name,
        kind=person.kind,
        is_admin=admin,
        link=link,
    )


def viewer_of(caller: WebCaller, link: str | None = None) -> Viewer:
    return _viewer(caller.person, admin=is_admin(caller.role), link=link)


async def person_viewer(
    db: AsyncSession, artifact: ArtifactRow, person_id: uuid.UUID | None, *, link: str | None
) -> Viewer:
    """A content ticket's viewer as they stand now: anonymous once the person is gone,
    suspended or out of the workspace."""
    person = (
        None
        if person_id is None
        else await people_store.get_person(db, tenant_id=artifact.tenant_id, person_id=person_id)
    )
    if (
        person is None
        or person.status != "active"
        or not await signin_allowed_in(db, tenant_id=artifact.tenant_id)
    ):
        return Viewer(link=link)
    standings = await chat_standings(db, [person])
    if has_left(standings, person):
        return Viewer(link=link)
    role = (await roles_of(db, [person], standings))[person.id]
    return _viewer(person, admin=is_admin(role), link=link)


async def _live_session(
    runtime: WebRuntime, db: AsyncSession, request: Request
) -> WebSessionRow | None:
    token = session_token(request, runtime.web)
    if not token:
        return None
    now = runtime.now()
    live = await sessions_store.get_live_session(db, token_sha256=token_sha256(token), now=now)
    return None if live is None or now >= live.created_at + ttl_of(runtime) else live


async def _caller_in(
    runtime: WebRuntime, db: AsyncSession, live: WebSessionRow, artifact: ArtifactRow
) -> WebCaller | None:
    """The sign-in's active person in the page's workspace, or None."""
    person_ids = await sessions_store.list_session_people(db, session_id=live.id)
    found = [
        p
        for p in await people_store.get_people(db, person_ids=person_ids)
        if p.tenant_id == artifact.tenant_id and p.status == "active"
    ]
    if not found or not await signin_allowed_in(db, tenant_id=artifact.tenant_id):
        return None
    caller = await build_caller(runtime, db, live, found[0])
    return None if caller.left else caller


async def resolve_viewer(
    runtime: WebRuntime,
    db: AsyncSession,
    request: Request,
    artifact: ArtifactRow | None,
    *,
    link: str | None = None,
) -> PageVisitor:
    live = await _live_session(runtime, db, request)
    caller = (
        None if live is None or artifact is None else await _caller_in(runtime, db, live, artifact)
    )
    if caller is None:
        return PageVisitor(Viewer(link=link), signed_in=live is not None)
    return PageVisitor(viewer_of(caller, link), caller, signed_in=True)


async def page_on(runtime: WebRuntime, db: AsyncSession, artifact: ArtifactRow) -> bool:
    """Whether artifacts are on for the page's agent; off, its page is not found."""
    place = FeaturePlace(tenant_id=artifact.tenant_id, platform="web", agent_id=artifact.agent_id)
    return (await feature_in(db, runtime.settings, place, "artifacts")).on
