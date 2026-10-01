"""The setup roster: ordering, paging, and who answers where the caller stands."""

from __future__ import annotations

import uuid

import pytest
from daimon.core.roster import Roster, RosterAgent, load_roster, order_roster, paginate
from daimon.core.scope import ChannelScopeRef, ConfigTier, DeploymentDefault
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, build_fake_anthropic
from sqlalchemy.ext.asyncio import AsyncSession


def _entry(name: str, *, ma_agent_id: str = "ag_1", tier: ConfigTier | None = None) -> RosterAgent:
    return RosterAgent(
        name=name,
        ma_agent_id=ma_agent_id,
        model_id="claude-sonnet-4-6",
        is_built_in=False,
        answering_tier=tier,
    )


# ---------------------------------------------------------------------------
# order_roster
# ---------------------------------------------------------------------------


def test_order_roster_puts_the_answering_agent_first_when_its_name_sorts_last() -> None:
    ordered = order_roster(
        [_entry("alpha", ma_agent_id="ag_a"), _entry("zulu", ma_agent_id="ag_z")],
        answering_here="zulu",
    )
    assert [row.name for row in ordered] == ["zulu", "alpha"], (
        "the agent answering here must lead the roster regardless of its name"
    )


def test_order_roster_sorts_by_casefolded_name_when_nothing_answers_here() -> None:
    ordered = order_roster(
        [
            _entry("Beta", ma_agent_id="ag_b"),
            _entry("alpha", ma_agent_id="ag_a"),
            _entry("Gamma", ma_agent_id="ag_g"),
        ],
        answering_here=None,
    )
    assert [row.name for row in ordered] == ["alpha", "Beta", "Gamma"], (
        "roster order must ignore case, not sort uppercase names ahead of lowercase ones"
    )


def test_order_roster_breaks_a_name_tie_by_ma_agent_id() -> None:
    ordered = order_roster(
        [_entry("dup", ma_agent_id="ag_z"), _entry("dup", ma_agent_id="ag_a")],
        answering_here=None,
    )
    assert [row.ma_agent_id for row in ordered] == ["ag_a", "ag_z"], (
        "two agents sharing a name must still order deterministically, by MA id"
    )


def test_order_roster_ignores_an_answering_name_no_agent_carries() -> None:
    ordered = order_roster(
        [_entry("beta", ma_agent_id="ag_b"), _entry("alpha", ma_agent_id="ag_a")],
        answering_here="ghost",
    )
    assert [row.name for row in ordered] == ["alpha", "beta"], (
        "a routed name with no live agent must not disturb the name ordering"
    )


# ---------------------------------------------------------------------------
# paginate
# ---------------------------------------------------------------------------


def test_paginate_returns_one_empty_page_when_there_are_no_items() -> None:
    page = paginate([], page=0, page_size=8)
    assert page.items == (), "an empty list must yield no items"
    assert (page.page, page.page_count, page.total) == (0, 1, 0), (
        "an empty list must still be one page so callers need no special case"
    )
    assert not page.has_previous and not page.has_next, "an empty page has no neighbours"


def test_paginate_returns_the_remainder_on_the_last_page() -> None:
    page = paginate(list(range(7)), page=2, page_size=3)
    assert page.items == (6,), "the last page must hold only the remainder"
    assert page.page_count == 3, "seven items at three per page is three pages"
    assert page.has_previous and not page.has_next, "the last page has a previous but no next"


def test_paginate_clamps_a_page_beyond_the_end_to_the_last_page() -> None:
    page = paginate(list(range(5)), page=99, page_size=2)
    assert page.page == 2, "a page number past the end must clamp to the last page"
    assert page.items == (4,), "clamping must return the last page's items"


def test_paginate_clamps_a_negative_page_to_the_first_page() -> None:
    page = paginate(list(range(5)), page=-3, page_size=2)
    assert page.page == 0, "a negative page number must clamp to the first page"
    assert page.items == (0, 1), "clamping must return the first page's items"


def test_paginate_rejects_a_page_size_below_one() -> None:
    with pytest.raises(ValueError, match="page_size"):
        paginate([1, 2, 3], page=0, page_size=0)


def test_paginate_carries_roster_agents_without_reordering_them() -> None:
    rows = (_entry("b", ma_agent_id="ag_b"), _entry("a", ma_agent_id="ag_a"))
    page = paginate(rows, page=0, page_size=8)
    assert page.items == rows, "paginate must window an already-ordered list, not re-sort it"


# ---------------------------------------------------------------------------
# load_roster
# ---------------------------------------------------------------------------


async def test_load_roster_marks_the_channel_default_as_answering_here(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    router = MARouter()
    router.add_agent_list(
        ma_agent(id="ag_alpha", name="alpha", tenant_id=tenant.id),
        ma_agent(id="ag_zulu", name="zulu", tenant_id=tenant.id),
    )
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-1"),
        tenant_id=tenant.id,
        agent_name="zulu",
    )

    roster = await load_roster(
        db_session,
        build_fake_anthropic(router.dispatch),
        tenant_id=tenant.id,
        platform="discord",
        channel_id="chan-1",
        thread_id=None,
        default=DeploymentDefault(),
    )

    assert roster.answering is not None, "a channel default must be reported as answering here"
    assert roster.answering.name == "zulu", "the channel's own default is the answering agent"
    assert roster.answering.answering_tier == "channel", (
        "the tier must survive so the caller knows where to change the routing"
    )
    assert [row.name for row in roster.rows] == ["zulu", "alpha"], (
        "the answering agent leads the roster"
    )


async def test_load_roster_reports_no_answering_agent_when_the_channel_is_unknown(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    router = MARouter()
    router.add_agent_list(ma_agent(id="ag_alpha", name="alpha", tenant_id=tenant.id))
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-1"),
        tenant_id=tenant.id,
        agent_name="alpha",
    )

    roster = await load_roster(
        db_session,
        build_fake_anthropic(router.dispatch),
        tenant_id=tenant.id,
        platform="discord",
        channel_id=None,
        thread_id=None,
        default=DeploymentDefault(agent_name="alpha"),
    )

    assert roster.answering is None, (
        "with no channel in hand there is no 'here', so nothing answers here"
    )
    assert [row.answering_tier for row in roster.rows] == [None], (
        "no row may claim a tier when the caller's channel is unknown"
    )


async def test_load_roster_reports_the_thread_responder_when_the_thread_is_bound(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    router = MARouter()
    router.add_agent_list(
        ma_agent(id="ag_daimon", name="daimon", tenant_id=tenant.id),
        ma_agent(id="ag_zulu", name="zulu", tenant_id=tenant.id),
    )
    await set_fields(
        db_session,
        scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="chan-1"),
        tenant_id=tenant.id,
        agent_name="zulu",
    )
    await create_binding(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        parent_channel_id="chan-1",
        thread_id="thread-1",
        responder_ma_agent_id="ag_daimon",
        responder_name="daimon",
    )

    roster = await load_roster(
        db_session,
        build_fake_anthropic(router.dispatch),
        tenant_id=tenant.id,
        platform="discord",
        channel_id="chan-1",
        thread_id="thread-1",
        default=DeploymentDefault(),
    )

    assert roster.answering is not None, "a bound thread has a responder"
    assert roster.answering.name == "daimon", (
        "inside a setup thread the binding's responder answers, not the parent channel's default"
    )
    assert roster.answering.answering_tier == "thread", (
        "the thread tier must be reported as itself, not flattened into the channel tier"
    )


async def test_load_roster_reads_built_in_and_creator_stamps_from_agent_metadata(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    creator = uuid.uuid4()
    router = MARouter()
    router.add_agent_list(
        ma_agent(
            id="ag_daimon",
            name="daimon",
            tenant_id=tenant.id,
            metadata={"daimon_managed": "true"},
        ),
        ma_agent(
            id="ag_mine",
            name="mine",
            tenant_id=tenant.id,
            metadata={"daimon_account": str(creator)},
        ),
        ma_agent(
            id="ag_legacy",
            name="legacy",
            tenant_id=tenant.id,
            metadata={"daimon_account": "not-a-uuid"},
        ),
    )

    roster = await load_roster(
        db_session,
        build_fake_anthropic(router.dispatch),
        tenant_id=tenant.id,
        platform="discord",
        channel_id=None,
        thread_id=None,
        default=DeploymentDefault(),
    )

    by_name = {row.name: row for row in roster.rows}
    assert by_name["daimon"].is_built_in, "daimon_managed=true marks a seeded agent as built in"
    assert not by_name["mine"].is_built_in, "an unmanaged agent is not built in"
    assert by_name["mine"].created_by_account_id == creator, (
        "the daimon_account stamp is the creating account"
    )
    assert by_name["legacy"].created_by_account_id is None, (
        "a daimon_account value that is not a uuid reads as no account, not a crash"
    )


async def test_load_roster_returns_an_empty_roster_when_the_tenant_has_no_agents(
    db_session: AsyncSession,
) -> None:
    tenant = await make_tenant(db_session)
    router = MARouter()
    router.add_agent_list()

    roster = await load_roster(
        db_session,
        build_fake_anthropic(router.dispatch),
        tenant_id=tenant.id,
        platform="discord",
        channel_id="chan-1",
        thread_id=None,
        default=DeploymentDefault(),
    )

    assert roster == Roster(), "a tenant with no agents yields the empty roster"
