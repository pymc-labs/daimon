"""Import a page bundle (`daimon.core.artifact_pages.importing`) into one workspace.

`import_page` runs in the caller's transaction: the CLI commits each page on its own, or
rolls the whole run back for a dry run. A page that cannot be imported is refused before
anything is written. A rerun finds the page by `imported_from` and replaces its versions,
data and imported watches in place, so its id, threads and settings made here stay.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal

from daimon.core._models import Artifact, ArtifactDocument, ArtifactVersion
from daimon.core.artifact_pages import documents
from daimon.core.artifact_pages.events import MAX_WATCHES_PER_PAGE
from daimon.core.artifact_pages.importing import (
    PageIn,
    PersonRef,
    WatchIn,
    compat_warnings,
)
from daimon.core.artifact_pages.model import ArtifactError, Limits, check_title, new_link_secret
from daimon.core.artifact_pages.standing import visitor_accounts
from daimon.core.config import ArtifactPagesSettings
from daimon.core.continuity.watch_projection import projection_path
from daimon.core.stores import artifact_pages, watches, web_identities, web_people
from daimon.core.stores.domain import WebPersonRow
from daimon.core.stores.identity import get_or_create_platform_principal
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

_EVENTS: Final = documents.EVENTS_COLLECTION
_MEMBER_PLACES: Final = ("channel", "thread")


@dataclass(frozen=True)
class ImportTarget:
    """Where pages go and what they run under; injected so the import reads no settings."""

    tenant_id: uuid.UUID
    agent_id: str
    agent_name: str
    limits: ArtifactPagesSettings
    crypto_keys: tuple[str, ...]
    asset_hosts: tuple[str, ...]
    content_host: bool
    # Slugs of every page in the run, for links between them.
    known_slugs: frozenset[str]


@dataclass(frozen=True)
class PageReport:
    """What importing one page did, or would do. Never holds HTML, data or secrets."""

    slug: str
    action: Literal["create", "replace", "refused", "expired"]
    members: int = 0
    visitors: int = 0
    dropped: int = 0
    watches: int = 0
    orphans_ended: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple[str, ...])
    problems: tuple[str, ...] = field(default_factory=tuple[str, ...])


@dataclass
class _People:
    accounts: list[uuid.UUID] = field(default_factory=list[uuid.UUID])
    members: int = 0
    visitors: int = 0
    dropped: int = 0


def _problems(page: PageIn, limits: ArtifactPagesSettings) -> list[str]:
    """Everything that makes the page unimportable, found before any write."""
    found: list[str] = []
    try:
        check_title(page.title)
    except ArtifactError as exc:
        found.append(str(exc))
    for version in page.versions:
        size = len(version.html.encode())
        if size > limits.html_bytes:
            found.append(
                f"version {version.version} is {size:,} bytes; the limit is {limits.html_bytes:,}"
            )
    kept = sorted((v.version for v in page.versions), reverse=True)[: limits.versions_kept]
    if page.current_version not in kept:
        found.append(f"current_version {page.current_version} is older than the versions kept")
    count = size = 0
    for doc in page.documents:
        try:
            if doc.collection != _EVENTS:
                documents.check_writable_collection(doc.collection)
            documents.check_collection(doc.collection)
            documents.check_doc_id(doc.doc_id)
            measured = documents.measure(doc.data, limits.document_bytes)
        except ArtifactError as exc:
            found.append(f"document {doc.collection}/{doc.doc_id}: {exc}")
            continue
        if doc.collection != _EVENTS:
            count, size = count + 1, size + measured
    scale = page.data_scale
    if count > limits.documents * scale or size > limits.data_bytes * scale:
        found.append(f"the data ({count} documents, {size:,} bytes) is over this page's limits")
    return found


async def _account(
    session: AsyncSession, cache: dict[str, uuid.UUID], tenant_id: uuid.UUID, discord_id: str
) -> uuid.UUID:
    if discord_id not in cache:
        principal = await get_or_create_platform_principal(
            session, tenant_id=tenant_id, platform="discord", external_id=discord_id
        )
        cache[discord_id] = principal.account_id
    return cache[discord_id]


async def _visitor_for(
    session: AsyncSession, *, tenant_id: uuid.UUID, email: str, invited_by: uuid.UUID
) -> tuple[WebPersonRow, bool]:
    """The workspace's person with this email, else a new visitor the owner invited."""
    address = email.strip().lower()
    for person in await web_people.list_people(session, tenant_id=tenant_id, search=address):
        if (person.email or "").lower() == address:
            return person, False
    person_id = uuid.uuid4()
    principal = await get_or_create_platform_principal(
        session, tenant_id=tenant_id, platform="web", external_id=str(person_id)
    )
    others = await web_people.list_people(session, tenant_id=tenant_id, limit=50)
    person = await web_people.create_invited(
        session,
        person_id=person_id,
        tenant_id=tenant_id,
        account_id=principal.account_id,
        kind="visitor",
        display_name=address.split("@")[0] or "Guest",
        workspace_name=next((p.workspace_name for p in others if p.workspace_name), ""),
        email=address,
        invite_id=None,
        invited_by_account_id=invited_by,
    )
    return person, True


async def _resolve_people(
    session: AsyncSession,
    refs: list[PersonRef],
    *,
    tenant_id: uuid.UUID,
    owner: uuid.UUID,
    cache: dict[str, uuid.UUID],
    warnings: list[str],
) -> _People:
    result = _People()
    for ref in refs:
        visitor = False
        if ref.discord_id is not None:
            account = await _account(session, cache, tenant_id, ref.discord_id)
        else:
            assert ref.email is not None, "a person has a discord_id or an email"
            person, _ = await _visitor_for(
                session, tenant_id=tenant_id, email=ref.email, invited_by=owner
            )
            if person.status != "active":
                result.dropped += 1
                continue
            account = person.account_id
            visitor = person.kind != "member"
            if (
                visitor
                and ref.google_sub is not None
                and not await _pin(session, person, ref.google_sub, tenant_id)
            ):
                warnings.append(
                    "a Google sign-in belongs to someone else here, so it is not pinned"
                )
        if account == owner or account in result.accounts:
            result.dropped += 1
            continue
        result.accounts.append(account)
        if visitor:
            result.visitors += 1
        else:
            result.members += 1
    return result


async def _pin(
    session: AsyncSession, person: WebPersonRow, subject: str, tenant_id: uuid.UUID
) -> bool:
    """Pin the visitor's Google sign-in; False when another person here already holds it."""
    held = [
        i
        for i in await web_identities.find_identity(
            session, provider="google", issuer="", subject=subject
        )
        if i.tenant_id == tenant_id
    ]
    if any(i.person_id != person.id for i in held):
        return False
    await web_identities.link_identity(
        session,
        tenant_id=tenant_id,
        person_id=person.id,
        provider="google",
        issuer="",
        subject=subject,
        label=person.email or "",
        groups=(),
    )
    return True


async def _existing(
    session: AsyncSession, target: ImportTarget, page: PageIn
) -> tuple[Artifact | None, list[str]]:
    """The page this one replaces, and why it cannot be imported if another page is in the way."""
    tenant_id = target.tenant_id
    same = await session.scalar(
        select(Artifact)
        .where(Artifact.tenant_id == tenant_id, Artifact.imported_from == page.legacy_url)
        .with_for_update()
    )
    taken = await session.scalar(
        select(Artifact).where(Artifact.tenant_id == tenant_id, Artifact.slug == page.slug)
    )
    by_id = await session.scalar(select(Artifact).where(Artifact.public_id == page.public_id))
    found: list[str] = []
    if taken is not None and (same is None or taken.id != same.id):
        found.append(f"the name {page.slug!r} is taken by another page")
    if by_id is not None and (same is None or by_id.id != same.id):
        found.append(f"the id {page.public_id} belongs to another page")
    if same is not None and (same.slug != page.slug or same.public_id != page.public_id):
        found.append("this address was imported with another name or id")
    return same, found


async def import_page(
    session: AsyncSession, page: PageIn, target: ImportTarget, *, now: datetime
) -> PageReport:
    """Create the page, or replace the one imported from the same address."""
    if page.expires_at is not None and page.expires_at <= now:
        return PageReport(page.slug, "expired")
    problems = _problems(page, target.limits)
    same, collisions = await _existing(session, target, page)
    problems += collisions
    if problems:
        return PageReport(page.slug, "refused", problems=tuple(problems))

    warnings: list[str] = []
    cache: dict[str, uuid.UUID] = {}
    tenant_id = target.tenant_id
    owner = await _account(session, cache, tenant_id, page.owner.discord_id)
    editors: list[uuid.UUID] = []
    for ref in page.editors:
        account = await _account(session, cache, tenant_id, ref.discord_id)
        if account != owner and account not in editors:
            editors.append(account)
    # The store's rule (`_require_members`): a visitor or outsider never edits a page.
    barred = await visitor_accounts(session, tenant_id=tenant_id, account_ids=[owner, *editors])
    if barred:
        return PageReport(
            page.slug,
            "refused",
            problems=(
                f"{len(barred)} of the owner and editors are outsiders here; "
                "only members can edit a page",
            ),
        )
    dropped = len(page.editors) - len(editors)
    if len(editors) > Limits.EDITORS:
        dropped += len(editors) - Limits.EDITORS
        editors = editors[: Limits.EDITORS]
    people = await _resolve_people(
        session, page.people, tenant_id=tenant_id, owner=owner, cache=cache, warnings=warnings
    )
    if len(people.accounts) > Limits.PEOPLE:
        people.dropped += len(people.accounts) - Limits.PEOPLE
        people.accounts = people.accounts[: Limits.PEOPLE]
    if dropped or people.dropped:
        warnings.append(
            f"dropped {dropped} editors and {people.dropped} people (duplicates, owner, limits)"
        )

    secret: bytes | None = None
    if page.access == "public":
        if page.link_secret is None:
            warnings.append("a public page came without a link secret, so it gets a new link")
        secret = artifact_pages.seal_link_secret(
            session,
            page.link_secret.get_secret_value() if page.link_secret else new_link_secret(),
        )
    elif page.link_secret is not None:
        warnings.append("a link secret on a page that is not public is ignored")

    versions = sorted(page.versions, key=lambda v: v.version, reverse=True)[
        : target.limits.versions_kept
    ]
    current = next(v for v in versions if v.version == page.current_version)
    values = {
        "title": page.title,
        "agent_id": target.agent_id,
        "owner_account_id": owner,
        "access": page.access,
        "people": people.accounts,
        "editors": editors,
        "editors_can_share": page.editors_can_share,
        "data": page.data,
        "events": page.events,
        "data_scale": page.data_scale,
        "link_secret": secret,
        "expires_at": page.expires_at,
        "current_version": page.current_version,
        "size_bytes": len(current.html.encode()),
        "created_at": page.created_at,
        "updated_at": page.updated_at,
    }
    if same is None:
        orm = Artifact(
            tenant_id=tenant_id,
            public_id=page.public_id,
            slug=page.slug,
            imported_from=page.legacy_url,
            data_revision=page.data_revision,
            **values,
        )
        session.add(orm)
    else:
        orm = same
        for name, value in values.items():
            setattr(orm, name, value)
        orm.data_revision = max(page.data_revision, same.data_revision + 1)
        await session.execute(delete(ArtifactVersion).where(ArtifactVersion.artifact_id == orm.id))
        await session.execute(
            delete(ArtifactDocument).where(ArtifactDocument.artifact_id == orm.id)
        )
    await session.flush()
    for version in versions:
        publisher = (
            None
            if version.published_by is None
            else await _account(session, cache, tenant_id, version.published_by.discord_id)
        )
        session.add(
            ArtifactVersion(
                tenant_id=tenant_id,
                artifact_id=orm.id,
                version=version.version,
                html=version.html,
                size_bytes=len(version.html.encode()),
                published_by_account_id=publisher,
                agent_id=target.agent_id,
                created_at=version.created_at,
            )
        )
    for doc in page.documents:
        writer = (
            None
            if doc.updated_by_ref is None
            else await _account(session, cache, tenant_id, doc.updated_by_ref.discord_id)
        )
        session.add(
            ArtifactDocument(
                tenant_id=tenant_id,
                artifact_id=orm.id,
                collection=doc.collection,
                doc_id=doc.doc_id,
                data=doc.data,
                size_bytes=documents.measure(doc.data, target.limits.document_bytes),
                updated_by=doc.updated_by,
                updated_by_account_id=writer,
                revision=1,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
            )
        )
    await session.flush()

    imported, orphans = await _import_watches(
        session, page, orm.id, target, now=now, warnings=warnings
    )
    warnings += compat_warnings(
        current.html,
        asset_hosts=target.asset_hosts,
        content_host=target.content_host,
        legacy_url=page.legacy_url,
        known_slugs=target.known_slugs,
    )
    return PageReport(
        page.slug,
        "create" if same is None else "replace",
        members=people.members,
        visitors=people.visitors,
        dropped=dropped + people.dropped,
        watches=imported,
        orphans_ended=orphans,
        warnings=tuple(warnings),
    )


async def _import_watches(
    session: AsyncSession,
    page: PageIn,
    page_id: uuid.UUID,
    target: ImportTarget,
    *,
    now: datetime,
    warnings: list[str],
) -> tuple[int, int]:
    """Replace this page's earlier imported watches with the bundle's; returns how many
    watches were made and how many orphan callback watches ended."""
    tenant_id = target.tenant_id
    ceiling = (await watches.watch_ceilings(session, tenant_id)).per_watch
    legacy_ids = {w.legacy_id for w in page.watches}
    for old in await watches.page_watches(session, tenant_id=tenant_id, page_id=page_id):
        if old.source_state.get("legacy_id") in legacy_ids:
            await watches.delete_watch(session, old.id, now=now)
    made = orphans = 0
    for watch in page.watches:
        note = _unsupported(watch, page, made)
        if note is not None:
            warnings.append(f"watch {watch.legacy_id}: {note}")
            continue
        cap = watch.max_turns_per_day or watches.DEFAULT_WATCH_CAP
        select, paused = _projection(watch)
        if paused:
            warnings.append(
                f"watch {watch.legacy_id}: select is not a path here, so it is paused "
                "without it; use `where` to filter events"
            )
        item = await watches.import_page_watch(
            session,
            tenant_id=tenant_id,
            page_id=page_id,
            legacy_id=watch.legacy_id,
            owner_discord_id=watch.owner.discord_id,
            agent_id=target.agent_id,
            agent_name=target.agent_name,
            channel_id=watch.channel_id,
            thread_id=watch.thread_id,
            delivery=watch.delivery,
            instructions=watch.instructions,
            select_path=select,
            where=watch.where,
            max_turns_per_day=cap,
            expires_at=watch.expires_at,
            paused=watch.paused or paused,
            crypto_keys=target.crypto_keys,
            now=now,
        )
        if item.watch is None:
            warnings.append(f"watch {watch.legacy_id}: skipped ({item.outcome})")
            continue
        made += 1
        orphans += await watches.end_callback_watches(
            session, tenant_id=tenant_id, legacy_id=watch.legacy_id, now=now
        )
        if item.downgraded:
            warnings.append(
                f"watch {watch.legacy_id}: no live conversation in its thread, so it reports "
                "in a new thread"
            )
        if item.truncated:
            warnings.append(f"watch {watch.legacy_id}: instructions were cut")
        if cap > ceiling:
            warnings.append(
                f"watch {watch.legacy_id}: asks for {cap} reports a day but this workspace "
                f"allows {ceiling}; raise it with `daimon watches limits set`"
            )
    return made, orphans


def _projection(watch: WatchIn) -> tuple[str | None, bool]:
    """The watch's `select` when it is a projection path, else none and a pause."""
    if watch.select is None:
        return None, False
    try:
        projection_path(watch.select)
    except ValueError:
        return None, True
    return watch.select, False


def _unsupported(watch: WatchIn, page: PageIn, made: int) -> str | None:
    if watch.place_type not in _MEMBER_PLACES:
        return f"skipped, a {watch.place_type} cannot hold a page watch"
    if not page.events:
        return "skipped, the page does not send events"
    if made >= MAX_WATCHES_PER_PAGE:
        return f"skipped, a page holds at most {MAX_WATCHES_PER_PAGE} watches"
    return None
