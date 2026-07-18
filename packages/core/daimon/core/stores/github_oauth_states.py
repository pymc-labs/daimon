"""OAuth state GDPR helpers — the OAuth flow

itself (create/peek/get_by_state/consume), but legacy `github_oauth_states`
rows may still exist from before the removal and must stay purgeable. These
two read/delete helpers are retained solely for `daimon.core.privacy` /
`daimon.core.purge` erasure of that legacy PII. No try/except —
exceptions propagate.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

from daimon.core._models import GitHubOauthState
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession


async def delete_states_for_platform_user(
    session: AsyncSession,
    *,
    platform: str,
    platform_user_id: str,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Delete ALL oauth-state rows for a (platform, platform_user_id). Idempotent.

    Returns rowcount; never raises on 0. Used by the GDPR purge orchestrator.

    Deliberately does NOT filter consumed_at or _cutoff() — consumed and expired
    handshake rows still carry platform_user_id PII and must be erased. This
    mirrors the design of get_by_state, which also omits the TTL/consumed filters
    to access the full row regardless of its lifecycle state.

    Callers pass platform="cli" with platform_user_id=<os_user> for CLI principals
    (the CLI auth flow writes such rows), as well as real platform/external_id pairs
    for platform principals.

    `tenant_id` (optional): adds `AND tenant_id = :tenant_id`. Callers should
    always pass it: neither `os_user` (CLI) nor a platform `external_id` is
    globally unique. Two machines can both be `ubuntu`, and Slack user ids are
    workspace-scoped (`U123` in two workspaces are two different humans), so a
    tenant-agnostic delete erases another tenant's in-flight handshake rows.
    None is left permitted only for a deliberate cross-tenant sweep of ghost
    rows under stale tenant_ids (re-key drift); it must not be used for a
    per-account GDPR purge.
    """
    predicates = [
        GitHubOauthState.platform == platform,
        GitHubOauthState.platform_user_id == platform_user_id,
    ]
    if tenant_id is not None:
        predicates.append(GitHubOauthState.tenant_id == tenant_id)
    result = await session.execute(delete(GitHubOauthState).where(*predicates))
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_states_for_platform_user(
    session: AsyncSession,
    *,
    platform: str,
    platform_user_id: str,
    tenant_id: uuid.UUID | None = None,
) -> int:
    """Count oauth-state rows that `delete_states_for_platform_user` would delete. Read-only.

    Pass the SAME `tenant_id` argument the delete caller uses, or the preview
    diverges from the purge (parity contract in daimon.core.privacy).
    """
    predicates = [
        GitHubOauthState.platform == platform,
        GitHubOauthState.platform_user_id == platform_user_id,
    ]
    if tenant_id is not None:
        predicates.append(GitHubOauthState.tenant_id == tenant_id)
    stmt = select(func.count()).select_from(GitHubOauthState).where(*predicates)
    return int((await session.execute(stmt)).scalar_one())
