"""The content fingerprint for seeded skills.

MA offers no carrier for skill-content idempotence, so this table is the only
thing standing between "a `defaults/skills/**` edit reaches every install" and
"it reaches none of them".
"""

from __future__ import annotations

from daimon.core.stores.seeded_skills import (
    list_seeded_skills,
    load_seeded_skill,
    prune_seeded_skills,
    record_seeded_skill,
)
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def test_absent_row_reads_as_never_seeded(db_session: AsyncSession) -> None:
    """An install predating this table has no row, which must drive one refresh."""
    tenant = await make_tenant(db_session)

    assert await load_seeded_skill(db_session, tenant_id=tenant.id, name="brainstorming") is None


async def test_record_round_trips_and_overwrites_on_conflict(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await record_seeded_skill(
        db_session,
        tenant_id=tenant.id,
        name="brainstorming",
        content_hash="hash-v1",
        anthropic_id="sk_1",
    )

    row = await load_seeded_skill(db_session, tenant_id=tenant.id, name="brainstorming")
    assert row is not None
    assert row.content_hash == "hash-v1"

    await record_seeded_skill(
        db_session,
        tenant_id=tenant.id,
        name="brainstorming",
        content_hash="hash-v2",
        anthropic_id="sk_1",
    )

    row = await load_seeded_skill(db_session, tenant_id=tenant.id, name="brainstorming")
    assert row is not None
    assert row.content_hash == "hash-v2", (
        "a second upload must replace the fingerprint, not accumulate rows"
    )


async def test_fingerprints_do_not_leak_across_tenants(db_session: AsyncSession) -> None:
    """Skills are per-tenant on MA; a neighbour's upload must not make us skip ours."""
    one = await make_tenant(db_session, workspace_id="guild-1")
    two = await make_tenant(db_session, workspace_id="guild-2")
    await record_seeded_skill(
        db_session,
        tenant_id=one.id,
        name="brainstorming",
        content_hash="hash-v1",
        anthropic_id="sk_1",
    )

    assert await load_seeded_skill(db_session, tenant_id=two.id, name="brainstorming") is None


async def test_list_seeded_skills_is_scoped_to_the_tenant(db_session: AsyncSession) -> None:
    """Library imports read this to fence seeded names, so a neighbour's must not count."""
    one = await make_tenant(db_session, workspace_id="guild-1")
    two = await make_tenant(db_session, workspace_id="guild-2")
    for name in ("brainstorming", "eda"):
        await record_seeded_skill(
            db_session, tenant_id=one.id, name=name, content_hash="h", anthropic_id=f"sk_{name}"
        )

    names_one = set(await list_seeded_skills(db_session, tenant_id=one.id))
    names_two = set(await list_seeded_skills(db_session, tenant_id=two.id))

    assert names_one == {"brainstorming", "eda"}, "every seeded name is listed"
    assert names_two == set(), "another tenant's seeded skills are not this tenant's"


async def test_prune_keeps_present_names_and_other_tenants(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    other = await make_tenant(db_session)
    for tenant_id, name in ((tenant.id, "kept"), (tenant.id, "removed"), (other.id, "removed")):
        await record_seeded_skill(
            db_session, tenant_id=tenant_id, name=name, content_hash="h", anthropic_id="sk"
        )

    await prune_seeded_skills(db_session, tenant_id=tenant.id, present_names={"kept"})

    assert set(await list_seeded_skills(db_session, tenant_id=tenant.id)) == {"kept"}
    assert set(await list_seeded_skills(db_session, tenant_id=other.id)) == {"removed"}, (
        "pruning is per tenant"
    )
