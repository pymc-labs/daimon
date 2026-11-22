"""Artifact pages, their versions and shared data (`daimon.core.artifact_pages` rules).

Every write checks the caller's role through the pure rules; callers own the
transaction. Data writes take the artifact's row lock so the caps hold and every write
moves `data_revision`, which a page's `onSnapshot` polls.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

from cryptography.fernet import MultiFernet
from daimon.core._models import (
    Artifact,
    ArtifactDocument,
    ArtifactVersion,
    TaskContinuation,
    Watch,
    WatchFiring,
    WebMessage,
    WebPerson,
    WebThread,
)
from daimon.core.artifact_pages import access, documents
from daimon.core.artifact_pages.access import PageChange, PageSettings, Role
from daimon.core.artifact_pages.events import PageEvent
from daimon.core.artifact_pages.model import (
    ArtifactError,
    check_slug,
    check_title,
    new_link_secret,
    new_public_id,
    page_url,
)
from daimon.core.config import ArtifactPagesSettings
from daimon.core.continuity.poll_tools import PAGE_DATA_TOOL
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.stores import watches
from daimon.core.stores.agent_files import _cipher  # pyright: ignore[reportPrivateUsage]
from daimon.core.stores.domain import (
    ArtifactCollectionRow,
    ArtifactDocumentRow,
    ArtifactRow,
    ArtifactUsage,
    ArtifactVersionRow,
    CollectionRule,
    WebArtifactRef,
)
from daimon.core.web_roles_rules import is_admin, role_of_account
from pydantic import JsonValue
from sqlalchemy import (
    ColumnElement,
    Select,
    case,
    delete,
    func,
    literal,
    null,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

# Expired pages stay restorable this long before the rows go.
PURGE_AFTER: Final = timedelta(days=30)


@dataclass(frozen=True)
class PublishOrigin:
    """Where a version was published: the chat message whose turn made it."""

    platform: str
    thread_id: str
    message_id: str


@dataclass(frozen=True)
class AskStats:
    threads: int
    turns_7d: int


def _fernet(session: AsyncSession) -> MultiFernet:
    cipher = _cipher(session)
    if cipher is None:
        raise ArtifactError("unavailable", "This Daimon has no encryption key set up.")
    return cipher


def _row(session: AsyncSession, orm: Artifact) -> ArtifactRow:
    secret = None if orm.link_secret is None else decrypt_token(_fernet(session), orm.link_secret)
    return ArtifactRow(
        id=orm.id,
        tenant_id=orm.tenant_id,
        public_id=orm.public_id,
        slug=orm.slug,
        title=orm.title,
        agent_id=orm.agent_id,
        owner_account_id=orm.owner_account_id,
        access=orm.access,  # pyright: ignore[reportArgumentType]  # ck_artifacts_access
        people=tuple(orm.people),
        editors=tuple(orm.editors),
        editors_can_share=orm.editors_can_share,
        data=orm.data,  # pyright: ignore[reportArgumentType]  # ck_artifacts_data
        events=orm.events,
        ask=orm.ask,
        data_scale=orm.data_scale,
        link_secret=secret,
        expires_at=orm.expires_at,
        current_version=orm.current_version,
        size_bytes=orm.size_bytes,
        data_revision=orm.data_revision,
        collection_rules=orm.collection_rules,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


async def _locked(session: AsyncSession, artifact_id: uuid.UUID) -> Artifact:
    orm = await session.scalar(select(Artifact).where(Artifact.id == artifact_id).with_for_update())
    if orm is None:
        raise ArtifactError("not_found", "No such artifact.")
    return orm


async def lock(session: AsyncSession, artifact: ArtifactRow) -> None:
    """Hold the page's row until the transaction ends, so a cap counted under it holds."""
    await _locked(session, artifact.id)


async def _require_members(
    session: AsyncSession, tenant_id: uuid.UUID, account_ids: Collection[uuid.UUID]
) -> None:
    """Editors, and so owners (a page transfers only to an editor), are members: a visitor
    managing a page would read it while `visible_outside_workspace` stayed false."""
    if not account_ids:
        return
    visitor = await session.scalar(
        select(WebPerson.account_id)
        .where(
            WebPerson.tenant_id == tenant_id,
            WebPerson.kind == "visitor",
            WebPerson.account_id.in_(list(account_ids)),
        )
        .limit(1)
    )
    if visitor is not None:
        raise ArtifactError("invalid", "Only members can edit a page. Visitors can open it.")


async def _require_inviter_or_admin(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    added: Collection[uuid.UUID],
    *,
    actor: uuid.UUID,
    actor_is_admin: bool,
) -> None:
    """A visitor made by a member's page share is held by whoever got their sign-in link, so
    only that inviter or an admin adds them to more pages; one an admin invited, or whose
    inviter is still an admin, is open to every sharer."""
    if actor_is_admin or not added:
        return
    visitors = await session.scalars(
        select(WebPerson).where(
            WebPerson.tenant_id == tenant_id,
            WebPerson.kind == "visitor",
            WebPerson.account_id.in_(list(added)),
        )
    )
    for visitor in visitors:
        inviter = visitor.invited_by_account_id
        if inviter == actor:
            continue
        standing = (
            None
            if inviter is None
            else await role_of_account(
                session, tenant_id=tenant_id, account_id=inviter, chat_admin=False
            )
        )
        if standing is None or not is_admin(standing):
            raise ArtifactError(
                "forbidden",
                f"An admin needs to share with {visitor.email or visitor.display_name}.",
            )


async def tool_role(
    session: AsyncSession, artifact: ArtifactRow, account_id: uuid.UUID, *, chat_admin: bool
) -> Role:
    """The account's role when tools may use the page now, else `none`: a suspended person
    or a visitor never may (`access.can_use_with_tools` for the rest), and a workspace page's
    members are its viewers. Page watches are made and fed under this one rule."""
    standing = await role_of_account(
        session, tenant_id=artifact.tenant_id, account_id=account_id, chat_admin=chat_admin
    )
    if standing is None or standing == "visitor":
        return "none"
    role = access.role_of(artifact, account_id, is_admin=is_admin(standing))
    if not access.can_use_with_tools(artifact, role):
        return "none"
    return "viewer" if role == "none" else role


def bound_rule(artifact: ArtifactRow, collection: str, role: Role) -> CollectionRule | None:
    """The collection rule that binds a role, None for managers: tools act as their person,
    so a member's agent sees and writes no more than that member's page would."""
    return (
        None if access.can_edit(role) else documents.rule_of(artifact.collection_rules, collection)
    )


def _set_settings(
    session: AsyncSession, orm: Artifact, settings: PageSettings, *, rotate: bool
) -> None:
    """Write the settings; a public page keeps its link unless `rotate`, others have none."""
    orm.access = settings.access
    orm.people = list(settings.people)
    orm.editors = list(settings.editors)
    orm.editors_can_share = settings.editors_can_share
    orm.data = settings.data
    orm.events = settings.events
    orm.ask = settings.ask
    orm.data_scale = settings.data_scale
    orm.expires_at = settings.expires_at
    orm.collection_rules = {k: v.model_dump() for k, v in settings.collection_rules.items()}
    if settings.access != "public":
        orm.link_secret = None
    elif rotate or orm.link_secret is None:
        orm.link_secret = encrypt_token(_fernet(session), new_link_secret())


async def _add_version(
    session: AsyncSession,
    orm: Artifact,
    *,
    html: str,
    account_id: uuid.UUID | None,
    agent_id: str,
    origin: PublishOrigin | None,
    now: datetime,
    limits: ArtifactPagesSettings,
) -> None:
    size = len(html.encode())
    if size > limits.html_bytes:
        raise ArtifactError(
            "too_large", f"The page is {size:,} bytes; the limit is {limits.html_bytes:,}."
        )
    orm.current_version += 1
    orm.size_bytes = size
    orm.updated_at = now
    session.add(
        ArtifactVersion(
            tenant_id=orm.tenant_id,
            artifact_id=orm.id,
            version=orm.current_version,
            html=html,
            size_bytes=size,
            published_by_account_id=account_id,
            agent_id=agent_id,
            origin_platform=None if origin is None else origin.platform,
            origin_thread_id=None if origin is None else origin.thread_id,
            origin_message_id=None if origin is None else origin.message_id,
        )
    )
    await session.flush()
    await session.execute(
        delete(ArtifactVersion).where(
            ArtifactVersion.artifact_id == orm.id,
            ArtifactVersion.version <= orm.current_version - limits.versions_kept,
        )
    )


async def publish(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: str,
    account_id: uuid.UUID,
    is_admin: bool,
    slug: str,
    title: str,
    html: str,
    change: PageChange,
    origin: PublishOrigin | None,
    new_link: bool,
    now: datetime,
    limits: ArtifactPagesSettings,
) -> ArtifactRow:
    """Create the page (owned by `account_id`) or add a version to the one at `slug`, which
    takes an owner, editor or admin; changing who sees it takes `can_share` as well."""
    check_slug(slug)
    title = check_title(title)
    orm = await session.scalar(
        select(Artifact)
        .where(Artifact.tenant_id == tenant_id, Artifact.slug == slug)
        .with_for_update()
    )
    if orm is None:
        settings = access.apply_change(PageSettings(), change, owner_account_id=account_id)
        access.check_change(PageSettings(), settings, role="owner", now=now)
        await _require_members(session, tenant_id, settings.editors)
        await _require_inviter_or_admin(
            session, tenant_id, settings.people, actor=account_id, actor_is_admin=is_admin
        )
        orm = Artifact(
            tenant_id=tenant_id,
            public_id=new_public_id(),
            slug=slug,
            title=title,
            agent_id=agent_id,
            owner_account_id=account_id,
            current_version=0,
        )
        _set_settings(session, orm, settings, rotate=False)
        session.add(orm)
        await session.flush()
    else:
        current = _row(session, orm)
        role = access.role_of(current, account_id, is_admin=is_admin)
        settings = access.apply_change(
            access.settings_of(current), change, owner_account_id=orm.owner_account_id
        )
        access.check_change(
            access.settings_of(current),
            settings,
            role=role,
            now=now,
            actor=account_id,
            outside=await visible_outside_workspace(session, current),
        )
        await _require_members(session, tenant_id, set(settings.editors) - set(current.editors))
        await _require_inviter_or_admin(
            session,
            tenant_id,
            set(settings.people) - set(current.people),
            actor=account_id,
            actor_is_admin=is_admin,
        )
        if new_link and not access.can_share(current, role):
            raise ArtifactError("forbidden", access.ASK_OWNER)
        _set_settings(session, orm, settings, rotate=new_link)
        orm.title = title
        orm.agent_id = agent_id
    await _add_version(
        session,
        orm,
        html=html,
        account_id=account_id,
        agent_id=agent_id,
        origin=origin,
        now=now,
        limits=limits,
    )
    await session.flush()
    await session.refresh(orm)
    return _row(session, orm)


async def get(session: AsyncSession, artifact_id: uuid.UUID) -> ArtifactRow | None:
    orm = await session.get(Artifact, artifact_id)
    return None if orm is None else _row(session, orm)


async def get_by_public_id(session: AsyncSession, public_id: str) -> ArtifactRow | None:
    orm = await session.scalar(select(Artifact).where(Artifact.public_id == public_id))
    return None if orm is None else _row(session, orm)


async def get_by_slug(
    session: AsyncSession, *, tenant_id: uuid.UUID, slug: str
) -> ArtifactRow | None:
    orm = await session.scalar(
        select(Artifact).where(Artifact.tenant_id == tenant_id, Artifact.slug == slug)
    )
    return None if orm is None else _row(session, orm)


async def list_visible(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID, is_admin: bool
) -> list[ArtifactRow]:
    """The workspace's pages this member may see listed, newest first. A public page's link
    is a bearer secret, so public pages list only to their owner, editors and admins."""
    query = select(Artifact).where(Artifact.tenant_id == tenant_id)
    if not is_admin:
        mine = (Artifact.owner_account_id == account_id) | Artifact.editors.contains([account_id])
        opens = (Artifact.access != "public") & (
            (Artifact.access == "workspace") | Artifact.people.contains([account_id])
        )
        query = query.where(mine | opens)
    rows = await session.scalars(query.order_by(Artifact.updated_at.desc(), Artifact.id))
    return [_row(session, orm) for orm in rows]


async def visible_outside_workspace(session: AsyncSession, artifact: ArtifactRow) -> bool:
    visitors = set(
        await session.scalars(
            select(WebPerson.account_id).where(
                WebPerson.tenant_id == artifact.tenant_id,
                WebPerson.kind == "visitor",
                WebPerson.account_id.in_(access.readers(artifact)),
            )
        )
    )
    return access.visible_outside_workspace(artifact, visitors)


async def update_settings(
    session: AsyncSession,
    artifact: ArtifactRow,
    change: PageChange,
    *,
    role: Role,
    account_id: uuid.UUID,
    now: datetime,
    is_admin: bool = False,
) -> ArtifactRow:
    """The one path for a settings change, from the web app or a republish's settings;
    `account_id` is who changes it, `is_admin` whether they are a workspace admin."""
    orm = await _locked(session, artifact.id)
    current = _row(session, orm)
    settings = access.apply_change(
        access.settings_of(current), change, owner_account_id=orm.owner_account_id
    )
    access.check_change(
        access.settings_of(current),
        settings,
        role=role,
        now=now,
        actor=account_id,
        outside=await visible_outside_workspace(session, current),
    )
    await _require_members(session, orm.tenant_id, set(settings.editors) - set(current.editors))
    await _require_inviter_or_admin(
        session,
        orm.tenant_id,
        set(settings.people) - set(current.people),
        actor=account_id,
        actor_is_admin=is_admin,
    )
    _set_settings(session, orm, settings, rotate=False)
    if change.title is not None:
        orm.title = check_title(change.title)
    orm.updated_at = now
    await session.flush()
    return _row(session, orm)


async def rotate_link(
    session: AsyncSession, artifact: ArtifactRow, *, role: Role, now: datetime
) -> ArtifactRow:
    """Replace a public page's link: the old link and every ticket minted from it stop."""
    orm = await _locked(session, artifact.id)
    current = _row(session, orm)
    if not access.can_share(current, role):
        raise ArtifactError("forbidden", access.ASK_OWNER)
    if current.access != "public":
        raise ArtifactError("invalid", "Only a public page has a link.")
    _set_settings(session, orm, access.settings_of(current), rotate=True)
    orm.updated_at = now
    await session.flush()
    return _row(session, orm)


async def transfer(
    session: AsyncSession,
    artifact: ArtifactRow,
    *,
    to_account_id: uuid.UUID,
    role: Role,
    now: datetime,
) -> ArtifactRow:
    """Give the page to one of its editors; the old owner stays on as an editor."""
    orm = await _locked(session, artifact.id)
    if not access.can_delete(role):
        raise ArtifactError("forbidden", access.ASK_OWNER)
    if to_account_id not in orm.editors:
        raise ArtifactError("invalid", "Make them an editor first.")
    await _require_members(session, orm.tenant_id, [to_account_id])
    previous = orm.owner_account_id
    orm.owner_account_id = to_account_id
    # The owner is more than an editor or one of the people, as `apply_change` keeps it.
    orm.editors = sorted({*orm.editors, previous} - {to_account_id}, key=str)
    orm.people = sorted(set(orm.people) - {to_account_id}, key=str)
    orm.updated_at = now
    await session.flush()
    return _row(session, orm)


async def _delete_rows(session: AsyncSession, ids: list[uuid.UUID], *, now: datetime) -> int:
    """Delete pages with their page-event watches; a watch's queued turns go first."""
    rows = list(await session.scalars(select(Artifact).where(Artifact.id.in_(ids))))
    for orm in rows:
        for watch in await watches.page_watches(session, tenant_id=orm.tenant_id, page_id=orm.id):
            await watches.delete_watch(session, watch.id, now=now)
    await session.execute(delete(Artifact).where(Artifact.id.in_([orm.id for orm in rows])))
    return len(rows)


async def delete_artifact(
    session: AsyncSession, artifact: ArtifactRow, *, role: Role, now: datetime
) -> None:
    if not access.can_delete(role):
        raise ArtifactError("forbidden", access.ASK_OWNER)
    await _delete_rows(session, [artifact.id], now=now)


def _versions(artifact_id: uuid.UUID) -> Select[tuple[ArtifactVersion]]:
    return select(ArtifactVersion).where(ArtifactVersion.artifact_id == artifact_id)


async def list_versions(session: AsyncSession, artifact_id: uuid.UUID) -> list[ArtifactVersionRow]:
    """Newest first, without their HTML (`html` is empty)."""
    rows = await session.scalars(
        _versions(artifact_id)
        .options(defer(ArtifactVersion.html))
        .order_by(ArtifactVersion.version.desc())
    )
    return [
        ArtifactVersionRow.model_validate(
            {k: getattr(row, k) for k in ArtifactVersionRow.model_fields if k != "html"}
            | {"html": ""}
        )
        for row in rows
    ]


async def get_version(
    session: AsyncSession, artifact_id: uuid.UUID, version: int
) -> ArtifactVersionRow | None:
    orm = await session.scalar(_versions(artifact_id).where(ArtifactVersion.version == version))
    return None if orm is None else ArtifactVersionRow.model_validate(orm)


async def restore(
    session: AsyncSession,
    artifact: ArtifactRow,
    version: int,
    *,
    role: Role,
    account_id: uuid.UUID | None,
    now: datetime,
    limits: ArtifactPagesSettings,
) -> ArtifactRow:
    """Publish an earlier version again, as a new version."""
    if not access.can_edit(role):
        raise ArtifactError("forbidden", access.ASK_OWNER)
    orm = await _locked(session, artifact.id)
    if version == orm.current_version:
        raise ArtifactError("invalid", "That is the current version.")
    old = await get_version(session, artifact.id, version)
    if old is None:
        raise ArtifactError("not_found", f"No version {version}.")
    await _add_version(
        session,
        orm,
        html=old.html,
        account_id=account_id,
        agent_id=old.agent_id,
        origin=None,
        now=now,
        limits=limits,
    )
    await session.flush()
    return _row(session, orm)


async def published_in(
    session: AsyncSession, *, platform: str, thread_id: str, message_id: str, public_url: str
) -> list[WebArtifactRef]:
    """The pages a chat message's turn published, each at the latest version it made."""
    rows = await session.execute(
        select(Artifact, func.max(ArtifactVersion.version))
        .join(ArtifactVersion, ArtifactVersion.artifact_id == Artifact.id)
        .where(
            ArtifactVersion.origin_platform == platform,
            ArtifactVersion.origin_thread_id == thread_id,
            ArtifactVersion.origin_message_id == message_id,
        )
        .group_by(Artifact.id)
        .order_by(Artifact.title)
    )
    return [
        WebArtifactRef(
            id=orm.id,
            title=orm.title,
            url=page_url(public_url, orm.public_id, orm.slug),
            version=version,
            access=orm.access,  # pyright: ignore[reportArgumentType]  # ck_artifacts_access
        )
        for orm, version in rows.tuples()
    ]


async def purge_expired(session: AsyncSession, *, now: datetime) -> int:
    """Delete pages expired more than `PURGE_AFTER` ago, with their watches' queued turns."""
    ids = list(
        await session.scalars(select(Artifact.id).where(Artifact.expires_at < now - PURGE_AFTER))
    )
    return await _delete_rows(session, ids, now=now)


async def ask_stats(session: AsyncSession, artifact_id: uuid.UUID, *, now: datetime) -> AskStats:
    """Viewers' ask threads for the page, and the agent's replies in them this past week."""
    threads = select(WebThread.id).where(
        WebThread.artifact_id == artifact_id, WebThread.deleted_at.is_(None)
    )
    count = await session.scalar(select(func.count()).select_from(threads.subquery()))
    turns = await session.scalar(
        select(func.count()).where(
            WebMessage.thread_id.in_(threads),
            WebMessage.role == "assistant",
            WebMessage.created_at > now - timedelta(days=7),
        )
    )
    return AskStats(threads=count or 0, turns_7d=turns or 0)


# ── documents ──────────────────────────────────────────────────────────────


def _doc_row(orm: ArtifactDocument) -> ArtifactDocumentRow:
    return ArtifactDocumentRow.model_validate(orm, from_attributes=True)


async def _get_doc_orm(
    session: AsyncSession, artifact_id: uuid.UUID, collection: str, doc_id: str
) -> ArtifactDocument | None:
    return await session.get(ArtifactDocument, (artifact_id, collection, doc_id))


async def get_doc(
    session: AsyncSession, artifact_id: uuid.UUID, collection: str, doc_id: str
) -> ArtifactDocumentRow | None:
    orm = await _get_doc_orm(session, artifact_id, collection, doc_id)
    return None if orm is None else _doc_row(orm)


async def list_docs(
    session: AsyncSession,
    artifact_id: uuid.UUID,
    query: documents.ListQuery,
    *,
    limits: ArtifactPagesSettings,
) -> list[ArtifactDocumentRow]:
    """One page of a collection; refused when it would be over `limits.list_bytes`."""
    order: ColumnElement[Any] = (
        ArtifactDocument.data[query.order_by]
        if query.order_by is not None
        else (
            ArtifactDocument.created_at if query.by_creation else ArtifactDocument.doc_id
        ).expression
    )
    selected = select(ArtifactDocument).where(
        ArtifactDocument.artifact_id == artifact_id, ArtifactDocument.collection == query.collection
    )
    if query.after is not None:
        selected = selected.where(ArtifactDocument.doc_id > query.after)
    if query.created_by is not None:
        selected = selected.where(ArtifactDocument.created_by_account_id == query.created_by)
    selected = selected.order_by(
        (order.desc() if query.descending else order.asc()).nulls_last(), ArtifactDocument.doc_id
    ).limit(query.limit)
    page = selected.subquery()
    size: int = await session.scalar(select(func.coalesce(func.sum(page.c.size_bytes), 0))) or 0
    documents.check_list_size(size, limits)
    return [_doc_row(orm) for orm in await session.scalars(selected)]


async def list_collections(
    session: AsyncSession, artifact_id: uuid.UUID
) -> list[ArtifactCollectionRow]:
    rows = await session.execute(
        select(ArtifactDocument.collection, func.count(), func.sum(ArtifactDocument.size_bytes))
        .where(ArtifactDocument.artifact_id == artifact_id)
        .group_by(ArtifactDocument.collection)
        .order_by(ArtifactDocument.collection)
    )
    return [
        ArtifactCollectionRow(name=name, documents=count, bytes=size or 0)
        for name, count, size in rows.tuples()
    ]


async def usage(session: AsyncSession, artifact_id: uuid.UUID) -> ArtifactUsage:
    """The page's own data; Daimon's `_` collections have caps of their own."""
    count, size = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(ArtifactDocument.size_bytes), 0)).where(
                ArtifactDocument.artifact_id == artifact_id,
                ~ArtifactDocument.collection.startswith(documents.SERVER_PREFIX, autoescape=True),
            )
        )
    ).one()
    return ArtifactUsage(documents=count, bytes=size)


def _count(result: Result[Any]) -> int:
    return cast(CursorResult[Any], result).rowcount


async def _bump(session: AsyncSession, orm: Artifact) -> int:
    orm.data_revision += 1
    await session.flush()
    return orm.data_revision


async def write_doc(
    session: AsyncSession,
    artifact: ArtifactRow,
    collection: str,
    doc_id: str,
    write: documents.DocumentWrite,
    *,
    by: str | None,
    account_id: uuid.UUID | None,
    rule: CollectionRule | None,
    now: datetime,
    limits: ArtifactPagesSettings,
) -> tuple[ArtifactDocumentRow, int]:
    """Write one document under the page's caps; returns it and the new data revision.
    `by` is the writer's name as shown (`agent` for a tool), `account_id` theirs. `rule`
    is the collection's rule for a page viewer, None for managers and tools."""
    documents.check_writable_collection(collection)
    documents.check_doc_id(doc_id)
    orm = await _locked(session, artifact.id)
    existing = await _get_doc_orm(session, artifact.id, collection, doc_id)
    if rule is not None:
        documents.check_write(
            rule,
            exists=existing is not None,
            created_by=None if existing is None else existing.created_by_account_id,
            viewer=account_id,
        )
    data = documents.apply_write(write, None if existing is None else existing.data)
    size = documents.measure(data, limits.document_bytes)
    documents.check_capacity(
        await usage(session, artifact.id),
        new_size=size,
        replaced_size=None if existing is None else existing.size_bytes,
        scale=orm.data_scale,
        limits=limits,
    )
    revision = orm.data_revision + 1
    saved = await _save_doc(
        session,
        orm,
        collection,
        doc_id,
        data,
        size=size,
        by=by,
        account_id=account_id,
        revision=revision,
        now=now,
    )
    return saved, await _bump(session, orm)


async def _save_doc(
    session: AsyncSession,
    orm: Artifact,
    collection: str,
    doc_id: str,
    data: dict[str, JsonValue],
    *,
    size: int,
    by: str | None,
    account_id: uuid.UUID | None,
    revision: int,
    now: datetime,
) -> ArtifactDocumentRow:
    values = {
        "data": data,
        "size_bytes": size,
        "updated_by": by,
        "updated_by_account_id": account_id,
        "revision": revision,
        "updated_at": now,
    }
    upsert = (
        insert(ArtifactDocument)
        .values(
            tenant_id=orm.tenant_id,
            artifact_id=orm.id,
            collection=collection,
            doc_id=doc_id,
            created_at=now,
            created_by_account_id=account_id,
            **values,
        )
        .on_conflict_do_update(index_elements=["artifact_id", "collection", "doc_id"], set_=values)
        .returning(ArtifactDocument)
    )
    saved = await session.scalar(
        select(ArtifactDocument).from_statement(upsert).execution_options(populate_existing=True)
    )
    assert saved is not None, "an upsert returns its row"
    return _doc_row(saved)


async def delete_doc(
    session: AsyncSession,
    artifact: ArtifactRow,
    collection: str,
    doc_id: str,
    *,
    rule: CollectionRule | None,
    account_id: uuid.UUID | None,
) -> int:
    """Delete one document (a missing one too, as Firestore does); returns the revision.
    `rule` binds a page viewer (`account_id`), as in `write_doc`."""
    documents.check_collection(collection)
    orm = await _locked(session, artifact.id)
    if rule is not None:
        existing = await _get_doc_orm(session, artifact.id, collection, doc_id)
        documents.check_write(
            rule,
            exists=existing is not None,
            created_by=None if existing is None else existing.created_by_account_id,
            viewer=account_id,
        )
    deleted = await session.execute(
        delete(ArtifactDocument).where(
            ArtifactDocument.artifact_id == artifact.id,
            ArtifactDocument.collection == collection,
            ArtifactDocument.doc_id == doc_id,
        )
    )
    if not _count(deleted):
        return orm.data_revision
    return await _bump(session, orm)


async def add_event(
    session: AsyncSession,
    artifact: ArtifactRow,
    event: PageEvent,
    *,
    account_id: uuid.UUID | None,
    now: datetime,
    limits: ArtifactPagesSettings,
) -> None:
    """Save an event to `_events` before any watch sees it, so it outlives a paused or
    missing watch. The newest `limits.events` (and `events_bytes`), times the data scale,
    stay. The page cannot read `_events`, so the data revision stays put."""
    orm = await _locked(session, artifact.id)
    size = documents.measure(event.document, limits.event_bytes)
    await session.execute(
        text(
            "DELETE FROM artifact_documents d USING ("
            " SELECT doc_id, row_number() OVER w AS n, sum(size_bytes) OVER w AS total"
            " FROM artifact_documents WHERE artifact_id = :id AND collection = :events"
            " WINDOW w AS (ORDER BY created_at DESC, doc_id DESC)"
            ") old WHERE d.artifact_id = :id AND d.collection = :events"
            " AND d.doc_id = old.doc_id AND (old.n >= :count OR old.total > :bytes)"
        ),
        {
            "id": artifact.id,
            "events": documents.EVENTS_COLLECTION,
            "count": limits.events * orm.data_scale,
            "bytes": limits.events_bytes * orm.data_scale - size,
        },
    )
    by = event.document.get("by")
    await _save_doc(
        session,
        orm,
        documents.EVENTS_COLLECTION,
        str(event.id),
        event.document,
        size=size,
        by=by if isinstance(by, str) else None,
        account_id=account_id,
        revision=orm.data_revision,
        now=now,
    )


async def mark_event_delivered(
    session: AsyncSession, artifact_id: uuid.UUID, event_id: uuid.UUID, *, delivered: int
) -> None:
    """Record how many watches took the event (`delivered` in its document)."""
    await session.execute(
        update(ArtifactDocument)
        .where(
            ArtifactDocument.artifact_id == artifact_id,
            ArtifactDocument.collection == documents.EVENTS_COLLECTION,
            ArtifactDocument.doc_id == str(event_id),
        )
        .values(
            data=ArtifactDocument.data.op("||")(func.jsonb_build_object("delivered", delivered))
        )
    )


# ── erasure ────────────────────────────────────────────────────────────────


async def count_owned(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    return (
        await session.scalar(select(func.count()).where(Artifact.owner_account_id == account_id))
        or 0
    )


async def delete_owned(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Erasure: the account's pages go, with their watches' queued turns."""
    ids = list(
        await session.scalars(select(Artifact.id).where(Artifact.owner_account_id == account_id))
    )
    return await _delete_rows(session, ids, now=datetime.now(UTC))


def _mentions(account_id: uuid.UUID) -> tuple[ColumnElement[bool], ColumnElement[bool]]:
    listed = Artifact.people.contains([account_id]) | Artifact.editors.contains([account_id])
    return listed, (ArtifactDocument.updated_by_account_id == account_id) | (
        ArtifactDocument.created_by_account_id == account_id
    )


async def count_mentions(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    listed, wrote = _mentions(account_id)
    pages = await session.scalar(select(func.count()).where(listed)) or 0
    docs = await session.scalar(select(func.count()).where(wrote)) or 0
    return pages + docs


async def scrub_account(session: AsyncSession, *, account_id: uuid.UUID) -> int:
    """Erasure: the account leaves other people's pages (people, editors) and its name
    leaves the documents and events it wrote, which stay with the page."""
    listed, wrote = _mentions(account_id)
    pages = await session.execute(
        update(Artifact)
        .where(listed)
        .values(
            people=func.array_remove(Artifact.people, account_id),
            editors=func.array_remove(Artifact.editors, account_id),
        )
    )
    theirs = ArtifactDocument.updated_by_account_id == account_id
    docs = await session.execute(
        update(ArtifactDocument)
        .where(wrote)
        .values(
            updated_by=case((theirs, null()), else_=ArtifactDocument.updated_by),
            updated_by_account_id=case(
                (theirs, null()), else_=ArtifactDocument.updated_by_account_id
            ),
            created_by_account_id=case(
                (ArtifactDocument.created_by_account_id == account_id, null()),
                else_=ArtifactDocument.created_by_account_id,
            ),
            data=case(
                (
                    theirs & (ArtifactDocument.collection == documents.EVENTS_COLLECTION),
                    ArtifactDocument.data.op("-")(literal("by")),
                ),
                else_=ArtifactDocument.data,
            ),
        )
    )
    return _count(pages) + _count(docs)


async def woken_by_outside_page(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, thread_id: str
) -> bool:
    """Whether the thread's running watch turn was woken by a page people outside the
    workspace can read, by one of its events or by a poll of its data: such a turn may not
    write to another such page."""
    woke = (
        select(Watch.source, Watch.page_id, Watch.source_state)
        .join(WatchFiring, WatchFiring.watch_id == Watch.id)
        .join(TaskContinuation, TaskContinuation.idempotency_key == WatchFiring.wake_key)
        .where(
            TaskContinuation.tenant_id == tenant_id,
            TaskContinuation.platform == platform,
            # A report in a new thread runs there; its wake is the one that made the thread.
            or_(
                TaskContinuation.thread_id == thread_id,
                WatchFiring.delivery_thread_id == thread_id,
            ),
            TaskContinuation.status == "claimed",
            Watch.source.in_(("page_event", "poll")),
        )
    )
    for source, page_id, state in (await session.execute(woke)).all():
        if source == "page_event":
            orm = await session.get(Artifact, page_id)
        elif state.get("tool") == PAGE_DATA_TOOL:
            slug = state["arguments"]["slug"]
            orm = await session.scalar(
                select(Artifact).where(Artifact.tenant_id == tenant_id, Artifact.slug == slug)
            )
        else:
            continue
        if orm is not None and await visible_outside_workspace(session, _row(session, orm)):
            return True
    return False
