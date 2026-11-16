"""Store for direct message grants, ordered rules and per-person picks."""

from __future__ import annotations

from datetime import UTC, datetime

from daimon.core.stores import dm_access
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def test_set_grant_when_replaced_then_reads_back_and_members_drops_the_row(
    db_session: AsyncSession,
) -> None:
    """A listed grant round-trips with deduplicated ids; `members` is the absence of a row."""
    tenant = await make_tenant(db_session)

    await dm_access.set_grant(
        db_session, tenant_id=tenant.id, agent_name="a", who="listed",
        user_ids=["u1", "u1"], group_ids=["g"], set_by="admin", now=NOW,
    )  # fmt: skip
    grants = await dm_access.list_grants(db_session, tenant_id=tenant.id)
    assert grants["a"].who == "listed", "mode is stored"
    assert grants["a"].user_ids == ("u1",), "ids are deduplicated"
    assert grants["a"].group_ids == ("g",), "groups are stored"

    await dm_access.set_grant(
        db_session, tenant_id=tenant.id, agent_name="a", who="excluded",
        user_ids=[], group_ids=[], set_by=None, now=NOW,
    )  # fmt: skip
    assert (await dm_access.list_grants(db_session, tenant_id=tenant.id))["a"].who == "excluded", (
        "a second write replaces the first"
    )

    await dm_access.set_grant(
        db_session, tenant_id=tenant.id, agent_name="a", who="members",
        user_ids=[], group_ids=[], set_by=None, now=NOW,
    )  # fmt: skip
    assert await dm_access.list_grants(db_session, tenant_id=tenant.id) == {}, (
        "members leaves no row"
    )


async def test_rules_when_added_moved_and_removed_then_positions_stay_contiguous(
    db_session: AsyncSession,
) -> None:
    """Add appends or inserts, move reorders, remove closes the gap."""
    tenant = await make_tenant(db_session)

    def add(agent: str, position: int | None = None):
        return dm_access.add_rule(
            db_session, tenant_id=tenant.id, subject="everyone", subject_id=None,
            agent_name=agent, locked=False, set_by=None, now=NOW, position=position,
        )  # fmt: skip

    await add("a")
    await add("b")
    await add("c", 0)
    names = [r.agent_name for r in await dm_access.list_rules(db_session, tenant_id=tenant.id)]
    assert names == ["c", "a", "b"], "position 0 inserts first"

    assert await dm_access.move_rule(db_session, tenant_id=tenant.id, position=2, to=0, now=NOW)
    rules = await dm_access.list_rules(db_session, tenant_id=tenant.id)
    assert [r.agent_name for r in rules] == ["b", "c", "a"], "move reorders"
    assert [r.position for r in rules] == [0, 1, 2], "positions are contiguous"

    assert await dm_access.remove_rule(db_session, tenant_id=tenant.id, position=1, now=NOW)
    rules = await dm_access.list_rules(db_session, tenant_id=tenant.id)
    assert [(r.position, r.agent_name) for r in rules] == [(0, "b"), (1, "a")], "gap closes"
    assert not await dm_access.remove_rule(db_session, tenant_id=tenant.id, position=9, now=NOW), (
        "an unknown position reports false"
    )
    assert not await dm_access.move_rule(
        db_session, tenant_id=tenant.id, position=9, to=0, now=NOW
    ), "moving an unknown position reports false"


async def test_add_rule_when_subject_id_does_not_fit_the_subject_then_refuses(
    db_session: AsyncSession,
) -> None:
    """A user rule needs an id and an everyone rule takes none."""
    import pytest
    from daimon.core.errors import UserFacingError

    tenant = await make_tenant(db_session)
    for subject, subject_id in (("user", None), ("everyone", "x")):
        with pytest.raises(UserFacingError):
            await dm_access.add_rule(
                db_session, tenant_id=tenant.id, subject=subject, subject_id=subject_id,  # type: ignore[arg-type]
                agent_name=None, locked=False, set_by=None, now=NOW,
            )  # fmt: skip


async def test_pick_when_set_then_replaced_and_cleared(db_session: AsyncSession) -> None:
    """One pick per person and workspace; setting again replaces it."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)

    assert await dm_access.get_pick(db_session, tenant_id=tenant.id, account_id=account.id) is None
    await dm_access.set_pick(
        db_session, tenant_id=tenant.id, account_id=account.id, agent_name="a", now=NOW
    )
    await dm_access.set_pick(
        db_session, tenant_id=tenant.id, account_id=account.id, agent_name="b", now=NOW
    )
    pick = await dm_access.get_pick(db_session, tenant_id=tenant.id, account_id=account.id)
    assert pick is not None
    assert pick.agent_name == "b", "the latest pick wins"
    assert await dm_access.count_picks_for_account(db_session, account_id=account.id) == 1, (
        "one row per person"
    )

    await dm_access.clear_pick(db_session, tenant_id=tenant.id, account_id=account.id)
    assert await dm_access.get_pick(db_session, tenant_id=tenant.id, account_id=account.id) is None


async def test_person_scoped_rows_when_purged_then_picks_user_rules_and_grant_ids_go(
    db_session: AsyncSession,
) -> None:
    """Erasing a person removes their pick, their user rules and their ids in grant lists."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await dm_access.set_pick(
        db_session, tenant_id=tenant.id, account_id=account.id, agent_name="a", now=NOW
    )
    for subject, sid in (("user", "gone"), ("group", "gone"), ("user", "kept")):
        await dm_access.add_rule(
            db_session, tenant_id=tenant.id, subject=subject, subject_id=sid,  # type: ignore[arg-type]
            agent_name="a", locked=False, set_by=None, now=NOW,
        )  # fmt: skip
    await dm_access.set_grant(
        db_session, tenant_id=tenant.id, agent_name="a", who="listed",
        user_ids=["gone", "kept"], group_ids=["gone"], set_by=None, now=NOW,
    )  # fmt: skip
    keys = [(tenant.id, "gone")]

    assert await dm_access.count_user_rules(db_session, keys=keys) == 1, "only user rules count"
    assert await dm_access.delete_user_rules(db_session, keys=keys, now=NOW) == 1
    assert await dm_access.scrub_user_from_grants(db_session, keys=keys, now=NOW) == 1
    assert await dm_access.delete_picks_for_account(db_session, account_id=account.id) == 1

    rules = await dm_access.list_rules(db_session, tenant_id=tenant.id)
    assert [(r.subject, r.subject_id, r.position) for r in rules] == [
        ("group", "gone", 0),
        ("user", "kept", 1),
    ], "group rule stays and positions close up"
    grant = (await dm_access.list_grants(db_session, tenant_id=tenant.id))["a"]
    assert grant.user_ids == ("kept",), "the person's id leaves the list"
    assert grant.group_ids == ("gone",), "group ids are not personal"


async def test_update_rule_when_changed_then_keeps_its_place_and_a_lock_needs_an_agent(
    db_session: AsyncSession,
) -> None:
    """The agent and lock change in place; a lock with no agent is refused everywhere."""
    import pytest
    from daimon.core.errors import UserFacingError

    tenant = await make_tenant(db_session)
    for agent in ("a", "b"):
        await dm_access.add_rule(
            db_session, tenant_id=tenant.id, subject="everyone", subject_id=None,
            agent_name=agent, locked=False, set_by=None, now=NOW,
        )  # fmt: skip

    changed = await dm_access.update_rule(
        db_session, tenant_id=tenant.id, position=1, agent_name="c", locked=True,
        set_by="admin", now=NOW,
    )  # fmt: skip
    rules = await dm_access.list_rules(db_session, tenant_id=tenant.id)
    assert changed is not None and changed.agent_name == "c", "the changed rule is returned"
    assert [(r.agent_name, r.locked) for r in rules] == [("a", False), ("c", True)], (
        "only that rule changes, in its place"
    )
    assert (
        await dm_access.update_rule(
            db_session,
            tenant_id=tenant.id,
            position=5,
            agent_name=None,
            locked=False,
            set_by=None,
            now=NOW,
        )  # fmt: skip
        is None
    ), "an unknown position reports None"
    with pytest.raises(UserFacingError):
        await dm_access.update_rule(
            db_session, tenant_id=tenant.id, position=0, agent_name=None, locked=True,
            set_by=None, now=NOW,
        )  # fmt: skip
    with pytest.raises(UserFacingError):
        await dm_access.add_rule(
            db_session, tenant_id=tenant.id, subject="everyone", subject_id=None,
            agent_name=None, locked=True, set_by=None, now=NOW,
        )  # fmt: skip
