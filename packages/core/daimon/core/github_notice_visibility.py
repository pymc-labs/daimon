"""Choose which queued repo names a private GitHub notice may show."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from cryptography.fernet import InvalidToken, MultiFernet
from daimon.core.github_credentials import decrypt_token
from daimon.core.github_visibility import pat_can_access_repo
from daimon.core.stores.github_links import get_account_link, get_user
from daimon.core.stores.github_new_repo_notices import (
    NewRepoNoticeGroup,
    prior_connection_installations,
)
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class NewRepoNoticeCopy:
    text: str
    connect_label: str
    dismiss_label: str | None


def new_repo_notice_copy(visible_names: tuple[str, ...]) -> NewRepoNoticeCopy:
    if len(visible_names) == 1:
        return NewRepoNoticeCopy(
            f"🐙 New on GitHub: {visible_names[0]}\nConnect this repo to Daimon?",
            "Connect repo",
            "Not now",
        )
    if visible_names:
        names = ", ".join(visible_names[:5])
        if len(visible_names) > 5:
            names += ", …"
        return NewRepoNoticeCopy(
            f"{len(visible_names)} new repos on GitHub: {names}. Connect them to Daimon?",
            "Connect repos",
            "Not now",
        )
    return NewRepoNoticeCopy("New repos are available on GitHub.", "Connect more repos", None)


async def visible_new_repo_names(
    session: AsyncSession,
    *,
    group: NewRepoNoticeGroup,
    account_id: uuid.UUID,
    platform: str,
    platform_user_id: str,
    fernet: MultiFernet,
    http_client: httpx.AsyncClient,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Fail closed unless both prior connection and current GitHub reach are proven."""
    link = await get_account_link(session, account_id=account_id)
    if link is None or link.platform != platform or link.platform_user_id != platform_user_id:
        return ()
    user = await get_user(session, github_user_id=link.github_user_id)
    current = now or datetime.now(UTC)
    if user is None or user.status != "active" or user.access_expires_at <= current:
        return ()
    try:
        access_token = decrypt_token(fernet, user.encrypted_access_token)
    except (InvalidToken, ValueError):
        return ()
    prior = await prior_connection_installations(
        session,
        tenant_id=group.tenant_id,
        account_id=account_id,
        github_user_id=link.github_user_id,
    )
    if not prior:
        return ()
    visible: list[str] = []
    for notice in group.notices:
        if notice.installation_id not in prior:
            continue
        try:
            if await pat_can_access_repo(
                http_client, owner_repo=notice.repo_full_name, pat=access_token
            ):
                visible.append(notice.repo_full_name)
        except httpx.HTTPError:
            continue
    return tuple(visible)
