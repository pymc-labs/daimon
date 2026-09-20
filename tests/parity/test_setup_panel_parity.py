"""Scenario: the read-only setup panel says the same things on both platforms.

Every test here opens the panel through the platform's own entry point — the
Discord `/agent-setup` cog, the Slack slash handler — clicks the real controls,
and reads whichever screen the click landed on back through
`drivers.views`. The claims are then asserted against one shared expectation,
so "Discord and Slack agree" is proved by both halves of a parametrized test
meeting the same constant rather than by one platform being compared to the
other's output.

What the two platforms agree on, and this module pins: which agents the roster
lists and in what order, that a member and an admin see the same controls, the
routing sentence under an unrouted agent, the empty-roster copy, that creating
an agent lands on its Details and says it answers nowhere yet, that a page
number survives a trip into Details and back, and each long Details list
behind Show more.

What they deliberately do not agree on, and this module pins per platform
instead of hiding, is recorded in `_DIVERGENCES` below — each one is a place
the two designs chose different words or different sizes on purpose.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Final, cast

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.discord.agent_setup.budget import ROSTER_PAGE_SIZE
from daimon.adapters.slack.agent_setup.state import PANEL_PAGE_SIZE
from daimon.core.agent_detail_lists import DETAIL_LIST_COLLAPSED_COUNT
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.routing_facts import (
    PRECEDENCE_LINE,
    UNROUTED_LINE,
    build_routing_request,
    build_unrouted_note,
)
from daimon.core.scope import ChannelScopeRef, TenantScopeRef
from daimon.core.setup_conversations import EMPTY_ROSTER_COPY, shared_keys_sentence
from daimon.core.stores.agent_files import put_agent_file
from daimon.core.stores.agent_repo_binding import set_binding
from daimon.core.stores.domain import Platform, RepoAccessProof
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing import ma_agent, tenant_metadata
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter, list_response, make_fake_ma_handler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .drivers.protocol import PlatformDriver
from .drivers.views import CapturedView, captured_titles

#: The deliberate per-platform differences these scenarios hold in place. Each
#: is a design decision, not drift, and each is asserted per platform below so
#: a change to either one fails a test rather than passing quietly.
_DIVERGENCES: Final[Mapping[str, str]] = {
    "roster title": "Discord titles the card by the channel; Slack titles the modal 'Agents'.",
    "back": "Discord draws a Back button; Slack pops its own view stack.",
    "page size": (
        f"Discord fits {ROSTER_PAGE_SIZE} rows on a page against the component budget; "
        f"Slack fits {PANEL_PAGE_SIZE} against the block budget."
    ),
}

#: The workspace, channel and user each platform's panel is opened in. Both
#: drivers alias their own channel and user to `here` / `you`, so a sentence
#: naming either reads identically once normalised.
_IDS: Final[Mapping[str, tuple[str, str, str]]] = {
    "discord": ("880000000000001", "990000000000002", "770000000000003"),
    "slack": ("T_PARITY_PANEL", "C_PARITY_PANEL", "U_PARITY_PANEL"),
}

_CHANNEL_LABEL: Final = "#here"

_ANSWERING = "answers-here"
_BUILT_IN = "daimon"
_UNROUTED = "unrouted-agent"

_PAGE_SIZES: Final[Mapping[str, int]] = {"discord": ROSTER_PAGE_SIZE, "slack": PANEL_PAGE_SIZE}
_KEY_NAMES: Final[tuple[str, ...]] = tuple(f"KEY_{index:02d}" for index in range(1, 13))
_SKILL_NAMES: Final[tuple[str, ...]] = tuple(f"skill-{index:02d}" for index in range(1, 13))
_CONNECTION_NAMES: Final[tuple[str, ...]] = tuple(
    f"connection-{index:02d}" for index in range(1, 13)
)
_UNROUTED_ROSTER_STATUS: Final[Mapping[str, str]] = {
    "discord": "not assigned",
    "slack": "Not assigned",
}
_ANSWERING_ROSTER_STATUS: Final[Mapping[str, str]] = {
    "discord": "answers here",
    "slack": "Answers in #here",
}

#: The Details sections, in the order both panels draw them.
_DETAILS_SECTIONS: Final[tuple[str, ...]] = (
    "Model",
    "Repository",
    "Branch",
    "Skills",
    "Connections",
    "Keys",
)


def _section_headings(details: CapturedView) -> tuple[str, ...]:
    """The Details section headings, in the order the screen emitted them.

    A section's body differs by platform — one puts an empty state in subtext,
    the other in the body — so only the heading each labelled block leads with
    is read back.
    """
    return tuple(
        heading
        for text in details.fields
        for heading in _DETAILS_SECTIONS
        if text.startswith(heading)
    )


def _list_value(details: CapturedView, heading: str) -> str | None:
    """Read a list body while ignoring a platform's label punctuation."""
    labelled = details.labelled(heading)
    if labelled is None:
        return None
    return labelled.removeprefix(heading).removeprefix(":").strip()


def _unrouted_note(agent_name: str, *, is_admin: bool) -> str:
    """The core's own sentence, on one line the way both panels render it."""
    return build_unrouted_note(
        agent_name=agent_name, channel_label=_CHANNEL_LABEL, is_admin=is_admin
    ).replace("\n", " ")


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _ids(driver: PlatformDriver) -> tuple[str, str, str]:
    return _IDS[driver.param_id]


def _agents(
    tenant_id: uuid.UUID, names: Sequence[tuple[str, dict[str, str]]]
) -> list[BetaManagedAgentsAgent]:
    return [
        ma_agent(id=f"ag_{name}", name=name, metadata=tenant_metadata(tenant_id, name, **extra))
        for name, extra in names
    ]


def _read_only_router(agents: Sequence[BetaManagedAgentsAgent]) -> MARouter:
    """Serve exactly these agents, plus the empty listings a read touches.

    The panel lists the tenant's agents once and retrieves one of them when a
    reader opens Details; nothing it does writes, so the router serves reads
    and would fail loudly on anything else.
    """
    router = MARouter()
    router.add_agent_list(*agents)
    for agent in agents:
        router.add_agent(agent)
    router.add("GET", r"/v1/skills", lambda _r, _m: list_response([]))
    router.add("GET", r"/v1/environments", lambda _r, _m: list_response([]))
    return router


def _writable_router() -> MARouter:
    """The stateful MA fake, for the one scenario that creates an agent."""
    handler = make_fake_ma_handler()
    router = MARouter()
    for method in ("GET", "POST", "PATCH"):
        router.add(method, r".*", lambda request, _match: handler(request))
    return router


async def _seed_tenant(
    driver: PlatformDriver, db_session: AsyncSession, *, workspace_id: str
) -> uuid.UUID:
    tenant = await make_tenant(
        db_session, platform=cast(Platform, driver.param_id), workspace_id=workspace_id
    )
    await db_session.commit()
    return tenant.id


async def _route_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    channel_id: str,
    agent_name: str,
) -> None:
    async with db_session_factory() as session, session.begin():
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant_id, channel_id=channel_id),
            tenant_id=tenant_id,
            agent_name=agent_name,
            mode="agent",
        )


async def _route_workspace(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
) -> None:
    async with db_session_factory() as session, session.begin():
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=tenant_id),
            tenant_id=tenant_id,
            agent_name=agent_name,
            mode="agent",
        )


async def _seed_three_agent_roster(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[MARouter, uuid.UUID, tuple[str, str, str]]:
    """One agent answering here, one built-in workspace default, one unrouted."""
    workspace_id, channel_id, user_id = _ids(driver)
    tenant_id = await _seed_tenant(driver, db_session, workspace_id=workspace_id)
    await _route_channel(
        db_session_factory, tenant_id=tenant_id, channel_id=channel_id, agent_name=_ANSWERING
    )
    await _route_workspace(db_session_factory, tenant_id=tenant_id, agent_name=_BUILT_IN)
    router = _read_only_router(
        _agents(
            tenant_id,
            [(_ANSWERING, {}), (_BUILT_IN, {"daimon_managed": "true"}), (_UNROUTED, {})],
        )
    )
    return router, tenant_id, (workspace_id, channel_id, user_id)


async def _open_three_agent_roster(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    is_admin: bool,
) -> tuple[CapturedView, MARouter, uuid.UUID, tuple[str, str, str]]:
    router, tenant_id, (workspace_id, channel_id, user_id) = await _seed_three_agent_roster(
        driver, db_session, db_session_factory
    )
    view = await driver.open_setup_panel(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        is_admin=is_admin,
    )
    return view, router, tenant_id, (workspace_id, channel_id, user_id)


# ---------------------------------------------------------------------------
# The roster
# ---------------------------------------------------------------------------


async def test_roster_lists_the_same_agents_in_the_same_order_on_both_platforms(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    view, _router, _tenant_id, _ids_used = await _open_three_agent_roster(
        driver, db_session, db_session_factory, is_admin=True
    )

    assert tuple(row.split()[0] for row in view.rows) == (_ANSWERING, _BUILT_IN, _UNROUTED), (
        f"{driver.param_id}: the agent answering here leads the roster, then name order"
    )
    for status in (
        _ANSWERING_ROSTER_STATUS[driver.param_id],
        _UNROUTED_ROSTER_STATUS[driver.param_id],
    ):
        assert status in view.body, f"{driver.param_id}: the compact roster keeps status {status!r}"
    assert view.action_labels == (
        "Details",
        "Details",
        "Details",
        f"Set up {_ANSWERING}",
        "New agent",
        "Who answers where",
        "Done",
    ), (
        f"{driver.param_id}: every roster row carries its own Details, and the screen's actions agree"
    )
    assert "built in" not in view.body.lower(), (
        f"{driver.param_id}: the roster has no built-in metadata subline"
    )
    assert view.page is None, f"{driver.param_id}: three agents need no pager"


async def test_roster_shows_a_member_and_an_admin_identical_controls(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Entering a conversation about a change is not the change.

    Hiding the roster from members made them ask an admin what the bot even
    does, so role changes the voice of the routing sentence on Details and
    nothing on this screen.
    """
    router, tenant_id, (workspace_id, channel_id, user_id) = await _seed_three_agent_roster(
        driver, db_session, db_session_factory
    )
    member_view = await driver.open_setup_panel(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        is_admin=False,
    )
    admin_view = await driver.open_setup_panel(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        is_admin=True,
    )

    assert admin_view.lines == member_view.lines, (
        f"{driver.param_id}: the roster reads the same for both roles"
    )
    assert admin_view.action_labels == member_view.action_labels, (
        f"{driver.param_id}: the roster offers both roles the same controls"
    )


async def test_empty_roster_shows_the_setup_copy_and_keeps_the_setup_action(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, channel_id, user_id = _ids(driver)
    tenant_id = await _seed_tenant(driver, db_session, workspace_id=workspace_id)

    view = await driver.open_setup_panel(
        sessionmaker=db_session_factory,
        router=_read_only_router([]),
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        is_admin=True,
    )

    assert view.rows == (EMPTY_ROSTER_COPY,), (
        f"{driver.param_id}: an empty roster says what to do next, and lists nothing else"
    )
    assert "Set up an agent" in view.action_labels, (
        f"{driver.param_id}: setup stays available with no agent answering here"
    )
    assert view.page is None, f"{driver.param_id}: an empty roster needs no pager"


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------


async def test_details_of_an_unrouted_agent_states_the_routing_request(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    (
        _roster,
        router,
        tenant_id,
        (workspace_id, channel_id, user_id),
    ) = await _open_three_agent_roster(driver, db_session, db_session_factory, is_admin=True)
    details = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="details",
        agent_name=_UNROUTED,
    )

    assert details.title == _UNROUTED, (
        f"{driver.param_id}: Details is titled by the agent it describes"
    )
    assert details.says(_unrouted_note(_UNROUTED, is_admin=True)), (
        f"{driver.param_id}: an admin is given the sentence to say, in their own voice"
    )
    assert details.body.count(UNROUTED_LINE) == 1, (
        f"{driver.param_id}: the agent's routing state is stated once, not echoed in a header"
    )
    assert _section_headings(details) == ("Model",), (
        f"{driver.param_id}: absent optional configuration is omitted rather than advertised"
    )


async def test_details_of_an_unrouted_agent_asks_a_member_to_find_an_admin(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    (
        _roster,
        router,
        tenant_id,
        (workspace_id, channel_id, user_id),
    ) = await _open_three_agent_roster(driver, db_session, db_session_factory, is_admin=False)
    details = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="details",
        agent_name=_UNROUTED,
    )

    assert details.says(_unrouted_note(_UNROUTED, is_admin=False)), (
        f"{driver.param_id}: a member is told who can make the change instead"
    )


async def test_details_reads_the_same_sections_in_the_same_order_on_both_platforms(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both panels show only present configuration, in one shared order."""
    workspace_id, channel_id, user_id = _ids(driver)
    tenant_id = await _seed_tenant(driver, db_session, workspace_id=workspace_id)
    agent = ma_agent(
        id=f"ag_{_UNROUTED}",
        name=_UNROUTED,
        metadata=tenant_metadata(tenant_id, _UNROUTED),
        skills=[{"type": "custom", "skill_id": name, "version": "1"} for name in _SKILL_NAMES[:2]],
        mcp_servers=[
            {
                "type": "url",
                "name": name,
                "url": f"https://{name}.example.com/mcp",
            }
            for name in _CONNECTION_NAMES[:2]
        ],
    )
    router = _read_only_router([agent])
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id)
    async with db_session_factory() as session, session.begin():
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            repo_url="https://github.com/example/research",
            default_branch="main",
            ma_secret_ref="secret-ref",
            proof=RepoAccessProof(kind="public", at=datetime.now(UTC), account_id=None),
        )
        await put_agent_file(
            session,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            key=_KEY_NAMES[0],
            content="unread",
            set_by_account_id=None,
        )
    await driver.open_setup_panel(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        is_admin=True,
    )
    details = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="details",
        agent_name=_UNROUTED,
    )

    assert _section_headings(details) == _DETAILS_SECTIONS, (
        f"{driver.param_id}: Details carries one section per thing the agent is wired to, "
        "in one shared order"
    )
    assert details.action_labels[0] == "Set up with Daimon", (
        f"{driver.param_id}: setup is the primary action before configuration details"
    )
    assert "claude mcp add" not in details.body, (
        f"{driver.param_id}: Details does not expose coding-tool commands before the action is used"
    )


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


async def test_creating_an_agent_lands_on_details_saying_it_answers_nowhere_yet(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, channel_id, user_id = _ids(driver)
    tenant_id = await _seed_tenant(driver, db_session, workspace_id=workspace_id)
    router = _writable_router()
    opened = await driver.open_setup_panel(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        is_admin=True,
    )
    assert opened.rows == (EMPTY_ROSTER_COPY,), (
        f"{driver.param_id}: the scenario starts with nothing to list"
    )

    await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="new_agent",
    )
    details = await driver.submit_new_agent(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        name="churn-explorer",
        purpose="looks at churn",
        model=DEFAULT_AGENT_MODEL,
    )

    assert details.title == "churn-explorer", (
        f"{driver.param_id}: creation lands on the new agent's own Details"
    )
    assert details.says(_unrouted_note("churn-explorer", is_admin=True)), (
        f"{driver.param_id}: a brand-new agent answers nowhere, and says so with the "
        "request that would fix it"
    )


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def _open_long_roster(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> tuple[MARouter, uuid.UUID, tuple[str, str, str]]:
    workspace_id, channel_id, user_id = _ids(driver)
    tenant_id = await _seed_tenant(driver, db_session, workspace_id=workspace_id)
    router = _read_only_router(
        _agents(tenant_id, [(f"agent-{index:02d}", {}) for index in range(45)])
    )
    await driver.open_setup_panel(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        is_admin=True,
    )
    return router, tenant_id, (workspace_id, channel_id, user_id)


async def test_page_two_of_a_long_roster_carries_that_platforms_own_page_size(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Both platforms page; how many rows fit on one is each budget's own (D-08)."""
    router, tenant_id, (workspace_id, channel_id, user_id) = await _open_long_roster(
        driver, db_session, db_session_factory
    )

    second = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="next_page",
    )

    size = _PAGE_SIZES[driver.param_id]
    assert second.page == 2, f"{driver.param_id}: Next lands the reader on the second page"
    assert tuple(row.split()[0] for row in second.rows) == tuple(
        f"agent-{index:02d}" for index in range(size, size * 2)
    ), (
        f"{driver.param_id}: the second page holds the rows after the first, "
        f"{size} of them ({_DIVERGENCES['page size']})"
    )


async def test_back_from_details_restores_the_page_the_reader_was_on(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    router, tenant_id, (workspace_id, channel_id, user_id) = await _open_long_roster(
        driver, db_session, db_session_factory
    )
    size = _PAGE_SIZES[driver.param_id]
    second = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="next_page",
    )
    opened_from = second.rows[0].split()[0]
    await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="details",
        agent_name=opened_from,
    )

    restored = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="back",
    )

    assert restored.page == 2, (
        f"{driver.param_id}: coming back from Details returns the reader to the page they left "
        f"({_DIVERGENCES['back']})"
    )
    assert tuple(row.split()[0] for row in restored.rows) == tuple(
        f"agent-{index:02d}" for index in range(size, size * 2)
    ), f"{driver.param_id}: and to the same rows, not to the top of the roster"
    assert captured_titles(driver.captured_views())[-3:] == [
        second.title,
        opened_from,
        second.title,
    ], f"{driver.param_id}: the trip out to Details and back is three screens, not a reload"


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


async def test_details_expands_one_long_list_at_a_time_and_can_collapse_it(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id, channel_id, user_id = _ids(driver)
    tenant_id = await _seed_tenant(driver, db_session, workspace_id=workspace_id)
    agent = ma_agent(
        id=f"ag_{_UNROUTED}",
        name=_UNROUTED,
        metadata=tenant_metadata(tenant_id, _UNROUTED),
        skills=[{"type": "custom", "skill_id": name, "version": "1"} for name in _SKILL_NAMES],
        mcp_servers=[
            {
                "type": "url",
                "name": name,
                "url": f"https://{name}.example.com/mcp",
            }
            for name in _CONNECTION_NAMES
        ],
    )
    router = _read_only_router([agent])
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent.id)
    async with db_session_factory() as session, session.begin():
        for key in _KEY_NAMES:
            await put_agent_file(
                session,
                tenant_id=tenant_id,
                agent_id=agent_uuid,
                key=key,
                content="unread",
                set_by_account_id=None,
            )

    await driver.open_setup_panel(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        is_admin=True,
    )
    collapsed = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="details",
        agent_name=_UNROUTED,
    )
    for heading, names in (
        ("Skills", _SKILL_NAMES),
        ("Connections", _CONNECTION_NAMES),
        ("Keys", _KEY_NAMES),
    ):
        assert _list_value(collapsed, heading) == (
            " ".join(names[:DETAIL_LIST_COLLAPSED_COUNT])
            + f" +{len(names) - DETAIL_LIST_COLLAPSED_COUNT} more"
        ), f"{driver.param_id}: {heading} initially shows six complete entries and the remainder"

    expanded = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="expand_keys",
    )

    assert _list_value(expanded, "Keys") == " ".join(_KEY_NAMES), (
        f"{driver.param_id}: Show more reveals every key name, and only names"
    )
    assert "+6 more" in (_list_value(expanded, "Skills") or ""), (
        f"{driver.param_id}: expanding keys leaves skills collapsed"
    )
    assert shared_keys_sentence(_UNROUTED) in expanded.notes, (
        f"{driver.param_id}: the list is followed by who a stored key actually reaches"
    )

    switched = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="expand_skills",
    )
    assert _list_value(switched, "Skills") == " ".join(_SKILL_NAMES), (
        f"{driver.param_id}: opening skills reveals all skill names"
    )
    assert "+6 more" in (_list_value(switched, "Keys") or ""), (
        f"{driver.param_id}: opening skills resets keys to its collapsed state"
    )

    collapsed_again = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="expand_skills",
    )
    assert "+6 more" in (_list_value(collapsed_again, "Skills") or ""), (
        f"{driver.param_id}: Show fewer returns the active list to six entries"
    )

    connections = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="expand_connections",
    )
    assert _list_value(connections, "Connections") == " ".join(_CONNECTION_NAMES), (
        f"{driver.param_id}: connection expansion uses the same shared limit and state"
    )


# ---------------------------------------------------------------------------
# Who answers where
# ---------------------------------------------------------------------------


async def test_who_answers_where_states_the_precedence_and_the_routing_request(
    driver: PlatformDriver,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    (
        _roster,
        router,
        tenant_id,
        (workspace_id, channel_id, user_id),
    ) = await _open_three_agent_roster(driver, db_session, db_session_factory, is_admin=True)
    routing = await driver.click_panel_action(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        action="who_answers_where",
    )

    assert routing.title == "Who answers where", (
        f"{driver.param_id}: the cascade screen is titled the same on both platforms"
    )
    request = build_routing_request(agent_name=_UNROUTED, channel_label=_CHANNEL_LABEL)
    assert routing.says(f"{PRECEDENCE_LINE} Tell Daimon: {request}"), (
        f"{driver.param_id}: the map states the rule, then the request that acts on it"
    )
    assert routing.says("#here") and routing.says(_ANSWERING), (
        f"{driver.param_id}: the channel the panel was opened in names its own responder"
    )
