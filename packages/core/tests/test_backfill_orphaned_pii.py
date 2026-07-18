"""Behavioral tests for the orphaned-PII backfill (migration 0024).

These tests replay the three DELETE statements as of migration 0024's landing
(a hand-maintained mirror, NOT mechanically coupled — an edit to the migration's
predicates would not fail these tests) and prove the three constraints:
  1. Orphaned rows (no surviving principal) are deleted.
  2. Live principal rows (cli and platform) are NEVER deleted — including
     CLI users' platform='cli' oauth_state rows (the load-bearing CLI predicate
     correction in the migration).
  3. Re-running the sweep a second time deletes zero rows (idempotent).

Run against the schema-per-test Postgres fixture:
  DAIMON_DATABASE__TEST_URL=postgresql+asyncpg://... uv run pytest packages/core/tests/test_backfill_orphaned_pii.py -x
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import sqlalchemy as sa
from daimon.core._models import GitHubCredential, GitHubOauthState, UserSkill
from daimon.testing.factories import (
    make_cli_principal,
    make_platform_principal,
    make_tenant,
)
from sqlalchemy import select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# SQL text — mirrors migration 0024's predicates as of its landing
# (guideline:testing: inline SQL, never import alembic migration files as
# Python modules). The mirror is hand-maintained; if the migration's
# predicates are ever edited post-landing (they should not be — it's a
# one-shot sweep), update these strings to match.
# ---------------------------------------------------------------------------

# Backfill: mirrors 0024_backfill_delete_orphaned_pii.py upgrade().
_DELETE_USER_SKILLS = sa.text(
    "DELETE FROM user_skills us"
    " WHERE NOT EXISTS (SELECT 1 FROM cli_principals c WHERE c.id = us.principal_id)"
    " AND NOT EXISTS (SELECT 1 FROM platform_principals p WHERE p.id = us.principal_id)"
)

_DELETE_GITHUB_CREDENTIALS = sa.text(
    "DELETE FROM github_credentials gc"
    " WHERE NOT EXISTS (SELECT 1 FROM cli_principals c WHERE c.id = gc.principal_id)"
    " AND NOT EXISTS (SELECT 1 FROM platform_principals p WHERE p.id = gc.principal_id)"
)

_DELETE_GITHUB_OAUTH_STATES = sa.text(
    "DELETE FROM github_oauth_states gos"
    " WHERE"
    "  (gos.platform = 'cli'"
    "   AND NOT EXISTS (SELECT 1 FROM cli_principals c WHERE c.os_user = gos.platform_user_id))"
    " OR"
    "  (gos.platform <> 'cli'"
    "   AND NOT EXISTS (SELECT 1 FROM platform_principals p"
    "                   WHERE p.platform = gos.platform AND p.external_id = gos.platform_user_id))"
)


async def _run_sweep(session: AsyncSession) -> tuple[int, int, int]:
    """Execute all three DELETE statements and return rowcounts."""
    r_us = cast(CursorResult[Any], await session.execute(_DELETE_USER_SKILLS))
    r_gc = cast(CursorResult[Any], await session.execute(_DELETE_GITHUB_CREDENTIALS))
    r_gos = cast(CursorResult[Any], await session.execute(_DELETE_GITHUB_OAUTH_STATES))
    await session.commit()
    return r_us.rowcount, r_gc.rowcount, r_gos.rowcount


# ---------------------------------------------------------------------------
# Test 1: orphans are deleted
# ---------------------------------------------------------------------------


async def test_sweep_deletes_orphaned_rows(
    db_session: AsyncSession,
) -> None:
    """Rows with principal_id (or platform key) matching no surviving principal are deleted.

    Simulates a pre-fix purge: the principal rows are gone but user_skills /
    github_credentials / github_oauth_states rows remain.  After the sweep all
    three should be gone.
    """
    tenant = await make_tenant(db_session)
    await db_session.commit()

    orphaned_principal_id = uuid.uuid4()  # no matching cli_principals or platform_principals row

    # Seed orphaned user_skill row
    db_session.add(
        UserSkill(
            tenant_id=tenant.id,
            principal_id=orphaned_principal_id,
            agent_name="daimon",
            name="some-skill",
            source_repo_url="https://github.com/example/repo",
            source_repo_branch="main",
            source_path="skills/some-skill",
            content_hash="abc123",
            anthropic_id=None,
            anthropic_latest_version=None,
        )
    )

    # Seed orphaned github_credentials row
    db_session.add(
        GitHubCredential(
            principal_id=orphaned_principal_id,
            github_login="ghost-user",
            encrypted_token=b"encrypted-bytes",
            scopes=["repo"],
        )
    )

    # Seed orphaned github_oauth_states row (platform principal — no matching row)
    db_session.add(
        GitHubOauthState(
            platform="discord",
            platform_user_id="ghost-discord-id",
            scopes=["repo"],
            tenant_id=tenant.id,
        )
    )

    await db_session.flush()
    await db_session.commit()

    # Verify seeds exist before sweep
    us_before = (
        await db_session.execute(
            select(UserSkill).where(UserSkill.principal_id == orphaned_principal_id)
        )
    ).scalar_one_or_none()
    assert us_before is not None, "user_skill row should exist before sweep"

    gc_before = (
        await db_session.execute(
            select(GitHubCredential).where(GitHubCredential.principal_id == orphaned_principal_id)
        )
    ).scalar_one_or_none()
    assert gc_before is not None, "github_credentials row should exist before sweep"

    # Run the sweep
    us_count, gc_count, gos_count = await _run_sweep(db_session)

    assert us_count > 0, "sweep should delete at least one orphaned user_skill row"
    assert gc_count > 0, "sweep should delete at least one orphaned github_credentials row"
    assert gos_count > 0, "sweep should delete at least one orphaned github_oauth_states row"

    # Verify rows are gone
    us_after = (
        await db_session.execute(
            select(UserSkill).where(UserSkill.principal_id == orphaned_principal_id)
        )
    ).scalar_one_or_none()
    assert us_after is None, "orphaned user_skill row must be deleted by sweep"

    gc_after = (
        await db_session.execute(
            select(GitHubCredential).where(GitHubCredential.principal_id == orphaned_principal_id)
        )
    ).scalar_one_or_none()
    assert gc_after is None, "orphaned github_credentials row must be deleted by sweep"


# ---------------------------------------------------------------------------
# Test 2: live principal rows survive — including CLI platform='cli' handshakes
# ---------------------------------------------------------------------------


async def test_sweep_preserves_live_principal_rows(
    db_session: AsyncSession,
) -> None:
    """Rows belonging to surviving principals must NOT be touched by the sweep.

    Regression lock for the CLI-handshake predicate correction: a naive anti-join
    against platform_principals alone would classify every live CLI user's
    github_oauth_states row (platform='cli', platform_user_id=<os_user>) as an
    orphan and wrongly delete it. The corrected predicate branches on platform.
    """
    tenant = await make_tenant(db_session)
    cli = await make_cli_principal(db_session, os_user="live-cli-user", tenant=tenant)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="live-discord-user",
        tenant=tenant,
    )
    await db_session.commit()

    # Seed user_skill for live CLI principal
    db_session.add(
        UserSkill(
            tenant_id=tenant.id,
            principal_id=cli.id,
            agent_name="daimon",
            name="cli-skill",
            source_repo_url="https://github.com/example/repo",
            source_repo_branch="main",
            source_path="skills/cli-skill",
            content_hash="hash-cli",
            anthropic_id=None,
            anthropic_latest_version=None,
        )
    )

    # Seed user_skill for live platform principal
    db_session.add(
        UserSkill(
            tenant_id=tenant.id,
            principal_id=pp.id,
            agent_name="daimon",
            name="pp-skill",
            source_repo_url="https://github.com/example/repo",
            source_repo_branch="main",
            source_path="skills/pp-skill",
            content_hash="hash-pp",
            anthropic_id=None,
            anthropic_latest_version=None,
        )
    )

    # Seed github_credentials for live CLI principal
    db_session.add(
        GitHubCredential(
            principal_id=cli.id,
            github_login="live-cli-user-gh",
            encrypted_token=b"cli-cred",
            scopes=["repo"],
        )
    )

    # Seed github_oauth_states for live CLI principal: platform='cli', platform_user_id=os_user.
    # This is the critical regression case — the old predicate (anti-join against
    # platform_principals only) would DELETE this row because no platform_principals row
    # has external_id='live-cli-user'. The corrected predicate checks cli_principals.os_user.
    db_session.add(
        GitHubOauthState(
            platform="cli",
            platform_user_id="live-cli-user",  # must match cli.os_user
            scopes=["repo"],
            tenant_id=tenant.id,
        )
    )

    # Seed github_oauth_states for live platform principal
    db_session.add(
        GitHubOauthState(
            platform="discord",
            platform_user_id="live-discord-user",  # must match pp.external_id
            scopes=["repo"],
            tenant_id=tenant.id,
        )
    )

    await db_session.flush()
    await db_session.commit()

    # Run the sweep — should delete NOTHING for live principals
    us_count, gc_count, gos_count = await _run_sweep(db_session)

    assert us_count == 0, "sweep must not delete user_skills for live principals"
    assert gc_count == 0, "sweep must not delete github_credentials for live principals"
    assert gos_count == 0, (
        "sweep must not delete github_oauth_states for live principals "
        "(including CLI platform='cli' rows keyed on os_user)"
    )

    # DB-level assertion: all rows survive
    cli_us = (
        await db_session.execute(select(UserSkill).where(UserSkill.principal_id == cli.id))
    ).scalar_one_or_none()
    assert cli_us is not None, "cli principal's user_skill must survive sweep"

    pp_us = (
        await db_session.execute(select(UserSkill).where(UserSkill.principal_id == pp.id))
    ).scalar_one_or_none()
    assert pp_us is not None, "platform principal's user_skill must survive sweep"

    cli_gc = (
        await db_session.execute(
            select(GitHubCredential).where(GitHubCredential.principal_id == cli.id)
        )
    ).scalar_one_or_none()
    assert cli_gc is not None, "cli principal's github_credentials must survive sweep"

    cli_gos = (
        await db_session.execute(
            select(GitHubOauthState).where(
                GitHubOauthState.platform == "cli",
                GitHubOauthState.platform_user_id == "live-cli-user",
            )
        )
    ).scalar_one_or_none()
    assert cli_gos is not None, (
        "CLI handshake row (platform='cli', platform_user_id=os_user) must survive — "
        "regression lock for the cli_principals.os_user predicate branch"
    )

    pp_gos = (
        await db_session.execute(
            select(GitHubOauthState).where(
                GitHubOauthState.platform == "discord",
                GitHubOauthState.platform_user_id == "live-discord-user",
            )
        )
    ).scalar_one_or_none()
    assert pp_gos is not None, "platform principal's oauth_state must survive sweep"


# ---------------------------------------------------------------------------
# Test 3: idempotency — second run deletes zero rows
# ---------------------------------------------------------------------------


async def test_sweep_is_idempotent_second_run_deletes_zero(
    db_session: AsyncSession,
) -> None:
    """Running the sweep twice: the second run finds nothing to delete.

    Applies regardless of whether the first run deleted orphans or not.
    """
    tenant = await make_tenant(db_session)
    await db_session.commit()

    orphaned_principal_id = uuid.uuid4()

    # Seed orphaned rows (same as test 1)
    db_session.add(
        UserSkill(
            tenant_id=tenant.id,
            principal_id=orphaned_principal_id,
            agent_name="daimon",
            name="idem-skill",
            source_repo_url="https://github.com/example/repo",
            source_repo_branch="main",
            source_path="skills/idem-skill",
            content_hash="idem-hash",
            anthropic_id=None,
            anthropic_latest_version=None,
        )
    )
    db_session.add(
        GitHubCredential(
            principal_id=orphaned_principal_id,
            github_login="idem-user",
            encrypted_token=b"idem-cred",
            scopes=["repo"],
        )
    )
    db_session.add(
        GitHubOauthState(
            platform="discord",
            platform_user_id="idem-discord-id",
            scopes=["repo"],
            tenant_id=tenant.id,
        )
    )

    await db_session.flush()
    await db_session.commit()

    # First run: deletes orphans
    first_us, first_gc, first_gos = await _run_sweep(db_session)
    assert first_us > 0, "first run should delete orphaned user_skills"
    assert first_gc > 0, "first run should delete orphaned github_credentials"
    assert first_gos > 0, "first run should delete orphaned github_oauth_states"

    # Second run: nothing left to delete
    second_us, second_gc, second_gos = await _run_sweep(db_session)
    assert second_us == 0, "second run must delete zero user_skills (idempotent)"
    assert second_gc == 0, "second run must delete zero github_credentials (idempotent)"
    assert second_gos == 0, "second run must delete zero github_oauth_states (idempotent)"
