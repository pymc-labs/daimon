"""Store for the scrub map: a keyed hash of each real value and the fake that replaced it."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from daimon.core._models import (
    EvalScrubEntity,
    PlatformChannelName,
    PlatformUserName,
    TenantGitHubRepo,
    WebPerson,
)
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession


async def load_map(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[tuple[str, str], str]:
    rows = await session.execute(
        select(EvalScrubEntity.kind, EvalScrubEntity.key_hash, EvalScrubEntity.fake).where(
            EvalScrubEntity.tenant_id == tenant_id
        )
    )
    return {(kind, digest): fake for kind, digest, fake in rows}


async def save_map(
    session: AsyncSession, *, tenant_id: uuid.UUID, rows: Mapping[tuple[str, str], str]
) -> None:
    """Keep new entries; one already kept for the same value stays as it is."""
    if rows:
        await session.execute(
            insert(EvalScrubEntity)
            .values(
                [
                    {"tenant_id": tenant_id, "kind": k, "key_hash": h, "fake": f}
                    for (k, h), f in rows.items()
                ]
            )
            .on_conflict_do_nothing(
                index_elements=[
                    EvalScrubEntity.tenant_id,
                    EvalScrubEntity.kind,
                    EvalScrubEntity.key_hash,
                ]
            )
        )


_MAX_NAMES = 20_000


@dataclass(frozen=True)
class DeploymentNames:
    """What the deployment itself knows is private, for the scrub dictionary."""

    people: list[tuple[str, str | None, str | None, str | None]]
    """(name, handle, platform id, email) per person."""
    places: list[str]
    """Channels, their categories and teams, and workspace names."""
    repos: list[str]


async def deployment_names(session: AsyncSession, *, tenant_id: uuid.UUID) -> DeploymentNames:
    users = await session.execute(
        select(
            PlatformUserName.display_name,
            PlatformUserName.handle,
            PlatformUserName.platform_user_id,
        )
        .where(PlatformUserName.tenant_id == tenant_id)
        .limit(_MAX_NAMES)
    )
    web = await session.execute(
        select(WebPerson.display_name, WebPerson.email, WebPerson.workspace_name)
        .where(WebPerson.tenant_id == tenant_id)
        .limit(_MAX_NAMES)
    )
    channels = await session.execute(
        select(PlatformChannelName.name, PlatformChannelName.parent_name)
        .where(PlatformChannelName.tenant_id == tenant_id)
        .limit(_MAX_NAMES)
    )
    repos = await session.scalars(
        select(TenantGitHubRepo.repo_full_name)
        .where(TenantGitHubRepo.tenant_id == tenant_id)
        .limit(_MAX_NAMES)
    )
    web_rows = list(web)
    people = [(name or handle or "", handle, uid, None) for name, handle, uid in users]
    people += [(name, None, None, email) for name, email, _ in web_rows]
    places = [p for row in channels for p in row if p]
    places += [w for _, _, w in web_rows if w]
    return DeploymentNames(
        people=[p for p in people if p[0]], places=sorted(set(places)), repos=list(repos)
    )
