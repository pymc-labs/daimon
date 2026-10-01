"""Real-DB behavior tests for the agent_skill_repo_credentials store.

The load-bearing property is separation: a skill-repo enrollment must never
move the agent's working-repo binding, and vice versa.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.stores.agent_repo_binding import get_binding, set_binding
from daimon.core.stores.agent_skill_repo_credentials import (
    delete_skill_repo_credential,
    get_skill_repo_credential,
    get_tenant_skill_repo_proof_kind,
    list_skill_repo_credentials_for_repo,
    set_skill_repo_credential,
)
from daimon.core.stores.domain import RepoAccessProof
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_set_skill_repo_credential_normalizes_the_repo_url(
    db_session: AsyncSession,
) -> None:
    """The stored key is canonical owner/repo, whatever shape the caller passed."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    row = await set_skill_repo_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/acme/skills.git",
        default_branch="main",
        path="",
        ma_secret_ref="secret-1",
        proof=None,
    )

    assert row.repo_url == "acme/skills", (
        "repo_url must be normalized on write, or a lookup by 'owner/repo' misses"
    )


@pytest.mark.parametrize(
    "lookup_url",
    [
        "acme/skills",
        "github.com/acme/skills",
        "https://github.com/acme/skills",
        "https://github.com/acme/skills.git",
        "https://github.com/acme/skills/",
    ],
)
async def test_get_skill_repo_credential_normalizes_every_lookup_shape(
    db_session: AsyncSession, lookup_url: str
) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await set_skill_repo_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/acme/skills",
        default_branch="main",
        path="skills",
        ma_secret_ref="secret-1",
        proof=None,
    )

    row = await get_skill_repo_credential(
        db_session, tenant_id=tenant.id, agent_id=agent_id, repo_url=lookup_url
    )

    assert row is not None, f"lookup by {lookup_url!r} must normalize to the stored key"
    assert row.path == "skills", "the stored path must round-trip"


async def test_get_skill_repo_credential_returns_none_when_unenrolled(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)

    row = await get_skill_repo_credential(
        db_session, tenant_id=tenant.id, agent_id=uuid.uuid4(), repo_url="acme/skills"
    )

    assert row is None, "an unenrolled (tenant, agent, repo) must read as None"


async def test_set_skill_repo_credential_upserts_without_evicting_another_repo(
    db_session: AsyncSession,
) -> None:
    """Two skill repos on one agent coexist — that is why repo_url is in the PK."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()

    for repo in ("acme/skills-a", "acme/skills-b"):
        await set_skill_repo_credential(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            repo_url=repo,
            default_branch="main",
            path="",
            ma_secret_ref=f"secret-{repo}",
            proof=None,
        )

    first = await get_skill_repo_credential(
        db_session, tenant_id=tenant.id, agent_id=agent_id, repo_url="acme/skills-a"
    )
    second = await get_skill_repo_credential(
        db_session, tenant_id=tenant.id, agent_id=agent_id, repo_url="acme/skills-b"
    )
    assert first is not None, "enrolling a second skill repo must not evict the first"
    assert second is not None, "the second skill repo must be enrolled"


async def test_set_skill_repo_credential_clears_stale_proof_when_none_is_stated(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    await set_skill_repo_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="acme/skills",
        default_branch="main",
        path="",
        ma_secret_ref="secret-1",
        proof=RepoAccessProof(kind="pat", at=datetime.now(tz=UTC), account_id=account.id),
    )

    reenrolled = await set_skill_repo_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="acme/skills",
        default_branch="main",
        path="",
        ma_secret_ref="secret-2",
        proof=None,
    )

    assert reenrolled.proof_kind is None, (
        "an explicitly absent proof must clear the stale one, not inherit it"
    )
    assert reenrolled.proof_at is None, "proof_at must be cleared with proof_kind"
    assert reenrolled.proof_account_id is None, "proof_account_id must be cleared with proof_kind"


async def test_list_skill_repo_credentials_for_repo_is_tenant_scoped(
    db_session: AsyncSession,
) -> None:
    """Another install's enrollment of the same repo must never be visible."""
    mine = await make_tenant(db_session)
    theirs = await make_tenant(db_session)
    my_agent = uuid.uuid4()
    their_agent = uuid.uuid4()
    for tenant_id, agent_id in ((mine.id, my_agent), (theirs.id, their_agent)):
        await set_skill_repo_credential(
            db_session,
            tenant_id=tenant_id,
            agent_id=agent_id,
            repo_url="acme/skills",
            default_branch="main",
            path="",
            ma_secret_ref="secret",
            proof=None,
        )

    rows = await list_skill_repo_credentials_for_repo(
        db_session, tenant_id=mine.id, repo_url="https://github.com/acme/skills"
    )

    assert [r.agent_id for r in rows] == [my_agent], (
        "the listing must be scoped to the calling tenant; another install's row leaked"
    )


async def test_get_tenant_skill_repo_proof_kind_returns_this_tenants_proof(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await set_skill_repo_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        repo_url="acme/skills",
        default_branch="main",
        path="",
        ma_secret_ref="secret",
        proof=RepoAccessProof(kind="public", at=datetime.now(tz=UTC), account_id=account.id),
    )

    kind = await get_tenant_skill_repo_proof_kind(
        db_session, tenant_id=tenant.id, repo_url="acme/skills"
    )

    assert kind == "public", "a proof this tenant established must be reported back"


async def test_get_tenant_skill_repo_proof_kind_ignores_another_tenants_proof(
    db_session: AsyncSession,
) -> None:
    mine = await make_tenant(db_session)
    theirs = await make_tenant(db_session)
    their_account = await make_account(db_session, tenant=theirs)
    await set_skill_repo_credential(
        db_session,
        tenant_id=theirs.id,
        agent_id=uuid.uuid4(),
        repo_url="acme/skills",
        default_branch="main",
        path="",
        ma_secret_ref="secret",
        proof=RepoAccessProof(kind="pat", at=datetime.now(tz=UTC), account_id=their_account.id),
    )

    kind = await get_tenant_skill_repo_proof_kind(
        db_session, tenant_id=mine.id, repo_url="acme/skills"
    )

    assert kind is None, (
        "another install's demonstrated access is not evidence that this tenant has any"
    )


async def test_set_skill_repo_credential_does_not_touch_agent_repo_binding(
    db_session: AsyncSession,
) -> None:
    """The whole reason this table exists: enrolling skills must not repoint the clone."""
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    await set_binding(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/acme/working-repo",
        default_branch="main",
        ma_secret_ref="working-secret",
        proof=None,
    )

    await set_skill_repo_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/acme/skills",
        default_branch="trunk",
        path="skills",
        ma_secret_ref="skills-secret",
        proof=None,
    )

    binding = await get_binding(db_session, tenant_id=tenant.id, agent_id=agent_id)
    assert binding is not None, "the working-repo binding must still exist"
    assert binding.repo_url == "acme/working-repo", (
        "enrolling a skill repo must not repoint the repo the agent clones"
    )
    assert binding.default_branch == "main", "the working repo's branch must be untouched"
    assert binding.ma_secret_ref == "working-secret", "the working repo's token must be untouched"


async def test_delete_skill_repo_credential_removes_only_that_repo(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    agent_id = uuid.uuid4()
    for repo in ("acme/skills-a", "acme/skills-b"):
        await set_skill_repo_credential(
            db_session,
            tenant_id=tenant.id,
            agent_id=agent_id,
            repo_url=repo,
            default_branch="main",
            path="",
            ma_secret_ref="secret",
            proof=None,
        )

    await delete_skill_repo_credential(
        db_session,
        tenant_id=tenant.id,
        agent_id=agent_id,
        repo_url="https://github.com/acme/skills-a",
    )

    assert (
        await get_skill_repo_credential(
            db_session, tenant_id=tenant.id, agent_id=agent_id, repo_url="acme/skills-a"
        )
        is None
    ), "the named enrollment must be gone (and the lookup key must normalize)"
    assert (
        await get_skill_repo_credential(
            db_session, tenant_id=tenant.id, agent_id=agent_id, repo_url="acme/skills-b"
        )
        is not None
    ), "the other enrollment must survive"


async def test_delete_skill_repo_credential_is_idempotent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)

    await delete_skill_repo_credential(
        db_session, tenant_id=tenant.id, agent_id=uuid.uuid4(), repo_url="acme/never-enrolled"
    )
