"""Keep mirrored skills current: check the server, gate the update, deploy, repin followers.

Every step is derived from the database again on the next run, so a crash or a failed
provider is retried then. A provider's agents move only to a version that provider holds.
An agent whose backend consent can't be renewed for the update is held, not moved.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

import structlog
from daimon.core.errors import DaimonError
from daimon.core.mcp_skills.install import ServerSkill
from daimon.core.mcp_skills.manifest import SkillEntry
from daimon.core.server_skills.gate import (
    AttachedAgent,
    Repin,
    allowed_tools_of,
    diff_manifests,
    manifest_digest,
    manifest_of,
    needs_approval,
    plan_repins,
)
from daimon.core.skills.ingest import SkillBundle, SkillIngestError, bundle_from_files
from daimon.core.stores import server_skills as store
from daimon.core.stores.domain import ServerSkillRow, ServerSkillStatus
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "CheckResult",
    "NeedsReconfirm",
    "SkillTarget",
    "SyncDeps",
    "TargetError",
    "Upstream",
    "UpstreamGone",
    "UpstreamUnreachable",
    "approve_update",
    "check_mirror",
    "follow_now",
    "version_label",
]

log = structlog.get_logger(__name__)

CheckResult = Literal["unchanged", "updated", "pending", "unavailable", "failed"]


class UpstreamGone(DaimonError):
    """The person who added the skill can no longer see it, or the server refuses them."""


class UpstreamUnreachable(DaimonError):
    """The server did not answer; the next run asks again."""


class TargetError(DaimonError):
    """A provider refused a publish or a repin; the next run tries again."""


class NeedsReconfirm(DaimonError):
    """The agent's backend consent can't be renewed for this update; its owner re-confirms."""


class Upstream(Protocol):
    """The skills server, asked as the person who added the mirror."""

    async def entry(self, mirror: ServerSkillRow) -> SkillEntry | None:
        """The listing entry for the mirror's URI, or None when it is gone."""
        ...

    async def fetch(self, mirror: ServerSkillRow) -> ServerSkill:
        """Every file of the mirror's skill, verified against the manifest."""
        ...


class SkillTarget(Protocol):
    """One provider: where a skill version is published and agents pin it."""

    @property
    def provider(self) -> str: ...

    async def publish(self, mirror: ServerSkillRow, bundle: SkillBundle) -> str:
        """Publish the bundle as the mirror's next version; the concrete version."""
        ...

    async def attached(self, mirror: ServerSkillRow) -> list[AttachedAgent]:
        """The provider's agents that have the mirror's skill attached."""
        ...

    async def repin(self, mirror: ServerSkillRow, agent_id: str, version: str) -> None:
        """Pin the agent to `version` as a new agent revision.

        Raises `NeedsReconfirm`, leaving the agent as it is, when its backend consent
        would not hold after the update.
        """
        ...


@dataclass(frozen=True)
class SyncDeps:
    server: str
    """The configured server's name; a mirror of any other server is unavailable."""
    sessionmaker: async_sessionmaker[AsyncSession]
    upstream: Callable[[ServerSkillRow], Upstream]
    targets: Sequence[SkillTarget]
    lock: Callable[[ServerSkillRow], AbstractAsyncContextManager[None]]
    now: Callable[[], datetime]


async def _row(deps: SyncDeps, mirror_id: uuid.UUID) -> ServerSkillRow | None:
    async with deps.sessionmaker() as session:
        return await store.get_mirror(session, mirror_id)


async def _check(
    deps: SyncDeps, mirror: ServerSkillRow, *, status: ServerSkillStatus, error: str | None
) -> None:
    async with deps.sessionmaker.begin() as session:
        await store.record_check(session, mirror.id, status=status, error=error, now=deps.now())


async def _bundle(
    deps: SyncDeps, mirror: ServerSkillRow, digest: str
) -> tuple[SkillBundle, ServerSkill]:
    """The upstream files as a checked bundle, refused unless they are `digest`."""
    fetched = await deps.upstream(mirror).fetch(mirror)
    if manifest_digest(manifest_of(fetched.entry)) != digest:
        raise UpstreamUnreachable("the skill changed while it was read")
    bundle = bundle_from_files(fetched.files)
    if bundle.preview.name != mirror.name:
        raise SkillIngestError(f"SKILL.md name must stay {mirror.name}.")
    return bundle, fetched


async def _repin(deps: SyncDeps, target: SkillTarget, mirror: ServerSkillRow, repin: Repin) -> bool:
    """Move one agent; one that is refused stays as it is and the others still move."""
    try:
        await target.repin(mirror, repin.agent_id, repin.version)
    except NeedsReconfirm as err:
        async with deps.sessionmaker.begin() as session:
            await store.hold_update(
                session,
                tenant_id=mirror.tenant_id,
                agent_id=repin.agent_id,
                skill_id=mirror.skill_id,
                digest=mirror.digest,
                reason=str(err)[:200],
                now=deps.now(),
            )
        log.info(
            "server_skill.repin_held",
            mirror_id=str(mirror.id),
            provider=target.provider,
            agent_id=repin.agent_id,
        )
        # Held is a settled outcome: the owner's re-confirm, not a retry, moves it on.
        return True
    except TargetError as err:
        log.warning(
            "server_skill.repin_failed",
            mirror_id=str(mirror.id),
            provider=target.provider,
            agent_id=repin.agent_id,
            err_type=type(err).__name__,
        )
        return False
    async with deps.sessionmaker.begin() as session:
        await store.release_holds(
            session,
            tenant_id=mirror.tenant_id,
            agent_ids=[repin.agent_id],
            skill_id=mirror.skill_id,
        )
    log.info(
        "server_skill.agent_repinned",
        mirror_id=str(mirror.id),
        provider=target.provider,
        agent_id=repin.agent_id,
        version=repin.version,
    )
    return True


async def _deploy(deps: SyncDeps, mirror: ServerSkillRow, bundle: SkillBundle | None) -> bool:
    """Publish the accepted files where missing, then repin each provider's followers.

    True when every provider holds them. A provider that fails keeps its agents as they are.
    """
    complete = True
    async with deps.sessionmaker() as session:
        deployments = await store.list_deployments(session, mirror.id)
        pinned = await store.pinned_agents(
            session, tenant_id=mirror.tenant_id, skill_id=mirror.skill_id
        )
    for target in deps.targets:
        held = deployments.get(target.provider)
        version = held.version if held is not None and held.digest == mirror.digest else None
        try:
            if version is None:
                if bundle is None:
                    bundle, _ = await _bundle(deps, mirror, mirror.digest)
                version = await target.publish(mirror, bundle)
                async with deps.sessionmaker.begin() as session:
                    await store.put_deployment(
                        session,
                        mirror_id=mirror.id,
                        tenant_id=mirror.tenant_id,
                        provider=target.provider,
                        digest=mirror.digest,
                        version=version,
                        now=deps.now(),
                    )
                log.info(
                    "server_skill.version_published",
                    mirror_id=str(mirror.id),
                    provider=target.provider,
                    version=version,
                )
            attached = await target.attached(mirror)
            for repin in plan_repins(attached, version=version, pinned=pinned):
                complete = await _repin(deps, target, mirror, repin) and complete
            # One held after its repin moved (its consent changed meanwhile) is released
            # here next run; from then on its backend settings ask for the re-confirm.
            async with deps.sessionmaker.begin() as session:
                await store.release_holds(
                    session,
                    tenant_id=mirror.tenant_id,
                    agent_ids=[a.agent_id for a in attached if a.version == version],
                    skill_id=mirror.skill_id,
                )
        except (TargetError, UpstreamGone, UpstreamUnreachable, SkillIngestError) as err:
            complete = False
            log.warning(
                "server_skill.deploy_failed",
                mirror_id=str(mirror.id),
                provider=target.provider,
                err_type=type(err).__name__,
            )
    return complete


async def _accept(
    deps: SyncDeps,
    mirror: ServerSkillRow,
    entry: SkillEntry,
    digest: str,
    bundle: SkillBundle | None,
    *,
    allowed_tools: JsonValue,
) -> CheckResult:
    async with deps.sessionmaker.begin() as session:
        await store.accept_version(
            session,
            mirror.id,
            digest=digest,
            manifest=manifest_of(entry),
            version_label=version_label(entry),
            allowed_tools=allowed_tools,
            now=deps.now(),
        )
        accepted = await store.get_mirror(session, mirror.id)
    if accepted is None:
        return "failed"
    if digest != mirror.digest:
        log.info("server_skill.update_accepted", mirror_id=str(mirror.id), digest=digest)
    return "updated" if await _deploy(deps, accepted, bundle) else "failed"


def version_label(entry: SkillEntry) -> str | None:
    """The version the server names, from its frontmatter, if it names one."""
    meta = entry.frontmatter.get("metadata")
    raw = meta.get("version") if isinstance(meta, dict) else entry.frontmatter.get("version")
    return str(raw)[:64] if isinstance(raw, str | int | float) else None


async def _gone(deps: SyncDeps, mirror: ServerSkillRow) -> CheckResult:
    """Keep the last version attached and stop updating until the server serves it again."""
    await _check(deps, mirror, status="unavailable", error=None)
    if mirror.status != "unavailable":
        log.info("server_skill.marked_unavailable", mirror_id=str(mirror.id))
    return "unavailable"


async def check_mirror(
    deps: SyncDeps, mirror_id: uuid.UUID, *, approve: str | None = None
) -> CheckResult:
    """Check one mirror against its server and act on what changed.

    `approve` is the pending digest a person approved: that update is applied.
    """
    first = await _row(deps, mirror_id)
    if first is None:
        return "failed"
    async with deps.lock(first):
        mirror = await _row(deps, mirror_id)
        if mirror is None:
            return "failed"
        return await _check_locked(deps, mirror, approve=approve)


async def _check_locked(
    deps: SyncDeps, mirror: ServerSkillRow, *, approve: str | None
) -> CheckResult:
    try:
        if mirror.server != deps.server:
            raise UpstreamGone("its server is no longer configured")
        if mirror.added_by_person_id is None:
            raise UpstreamGone("the person who added it is gone")
        entry = await deps.upstream(mirror).entry(mirror)
        if entry is None:
            raise UpstreamGone("the server no longer lists it")
    except UpstreamGone:
        return await _gone(deps, mirror)
    except UpstreamUnreachable as err:
        await _check(deps, mirror, status=mirror.status, error=str(err)[:200])
        return "failed"
    manifest = manifest_of(entry)
    digest = manifest_digest(manifest)
    if digest == mirror.digest:
        if mirror.status != "current":
            log.info("server_skill.resumed", mirror_id=str(mirror.id))
        # Same files, so the same tools: an unchecked listing never rewrites them.
        result = await _accept(
            deps, mirror, entry, digest, None, allowed_tools=mirror.allowed_tools
        )
        return "unchanged" if result == "updated" else result
    if digest == mirror.pending_digest and approve != digest:
        await _check(deps, mirror, status="pending", error=None)
        return "pending"
    try:
        bundle, fetched = await _bundle(deps, mirror, digest)
    except UpstreamGone:
        return await _gone(deps, mirror)
    except (UpstreamUnreachable, SkillIngestError) as err:
        await _check(deps, mirror, status=mirror.status, error=str(err)[:200])
        return "failed"
    # The fetched entry's frontmatter is checked against SKILL.md; the listing's is not.
    entry = fetched.entry
    changes = diff_manifests(mirror.manifest, manifest)
    gated = needs_approval(
        changes,
        fetched.files,
        old_tools=mirror.allowed_tools,
        new_tools=allowed_tools_of(entry.frontmatter),
    )
    if gated and approve != digest:
        async with deps.sessionmaker.begin() as session:
            await store.hold_for_approval(
                session,
                mirror.id,
                digest=digest,
                version_label=version_label(entry),
                changes=changes.as_json(),
                now=deps.now(),
            )
        log.info("server_skill.approval_requested", mirror_id=str(mirror.id), digest=digest)
        return "pending"
    return await _accept(
        deps, mirror, entry, digest, bundle, allowed_tools=allowed_tools_of(entry.frontmatter)
    )


async def follow_now(deps: SyncDeps, *, tenant_id: uuid.UUID, skill_id: str, agent_id: str) -> None:
    """Move an agent that just chose to follow onto the current version, as the sync would."""
    async with deps.sessionmaker() as session:
        first = await store.mirror_of_skill(session, tenant_id=tenant_id, skill_id=skill_id)
    if first is None:
        return
    async with deps.lock(first):
        async with deps.sessionmaker() as session:
            mirror = await store.get_mirror(session, first.id)
            deployments = await store.list_deployments(session, first.id)
            pinned = await store.pinned_agents(session, tenant_id=tenant_id, skill_id=skill_id)
        if mirror is None or agent_id in pinned:
            return
        for target in deps.targets:
            held = deployments.get(target.provider)
            if held is None or held.digest != mirror.digest:
                continue  # the next check deploys it first
            try:
                attached = [a for a in await target.attached(mirror) if a.agent_id == agent_id]
            except TargetError:
                continue  # the next check moves it
            for repin in plan_repins(attached, version=held.version, pinned=pinned):
                await _repin(deps, target, mirror, repin)


async def approve_update(deps: SyncDeps, mirror_id: uuid.UUID, *, digest: str) -> CheckResult:
    """Apply the update the person reviewed (`digest`), if it still waits and is still served.

    A newer update that replaced it is left waiting for its own review.
    """
    mirror = await _row(deps, mirror_id)
    if mirror is None or mirror.pending_digest is None:
        return "failed"
    if mirror.pending_digest != digest:
        return "pending"
    result = await check_mirror(deps, mirror_id, approve=digest)
    if result == "updated":
        log.info("server_skill.update_approved", mirror_id=str(mirror_id))
    return result
