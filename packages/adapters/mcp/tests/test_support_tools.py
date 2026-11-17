"""DB-backed tests for the support MCP tools.

The Discord visibility lookup is patched on `_channel_target`, as in
`test_channel_budget_tools.py`; the agent lookup is patched on the tool module.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import re
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import jsonschema
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _channel_target as channel_target
from daimon.adapters.mcp.tools import support as tools
from daimon.adapters.mcp.tools.support import (
    _clear_limit_impl,  # pyright: ignore[reportPrivateUsage]
    _get_channel_impl,  # pyright: ignore[reportPrivateUsage]
    _get_menus_impl,  # pyright: ignore[reportPrivateUsage]
    _list_limits_impl,  # pyright: ignore[reportPrivateUsage]
    _list_requests_impl,  # pyright: ignore[reportPrivateUsage]
    _set_channel_impl,  # pyright: ignore[reportPrivateUsage]
    _set_limit_impl,  # pyright: ignore[reportPrivateUsage]
    _set_menu_impl,  # pyright: ignore[reportPrivateUsage]
    _settle_request_impl,  # pyright: ignore[reportPrivateUsage]
    _use_menu_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core import support_routing
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import support as store
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.stores.teams_installations import record_teams_installation
from daimon.core.support import (
    CLIENT_NOT_NOTIFIED,
    MAX_FIELDS,
    MAX_OPTIONS,
    MAX_PARAGRAPH_CHARS,
    MAX_TEXT_CHARS,
    RequestChoice,
    SupportPlace,
    parse_menu,
)
from daimon.testing.factories import make_account, make_tenant
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

type Factory = async_sessionmaker[AsyncSession]

_PARENT = "222"
_THREAD = "999"
_MENU: dict[str, object] = {
    "name": "Support",
    "options": [
        {"key": "review", "label": "Review", "price": {"kind": "range", "min": "1", "max": "3"}}
    ],
}
_PERSON = "100000000000000001"
_LIMIT = {
    "scope_id": None,
    "shared": None,
    "each_person": None,
    "window": None,
    "starts_at": None,
    "ends_at": None,
    "person": None,
    "expected_revision": None,
}
_NO_MENU = ("menu", "body", "expected_revision")


def _limit(**changes: object) -> dict[str, object]:
    return {**_LIMIT, **changes}


@pytest.fixture
def own_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `support_own_channel` feature on, where the tool and the routing both ask."""
    monkeypatch.setattr(tools, "own_channel_allowed", lambda settings: True)
    monkeypatch.setattr(support_routing, "own_channel_allowed", lambda settings: True)


@pytest.fixture(autouse=True)
def visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every channel is visible and `_THREAD`'s parent is `_PARENT`."""

    async def fake(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
        return _PARENT if channel_id == _THREAD else channel_id

    monkeypatch.setattr(channel_target, "resolve_visible_channel", fake)


def _runtime(sessionmaker: Factory) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        ),
        deployment_default=DeploymentDefault(),
    )


async def _seed(sessionmaker: Factory) -> tuple[TenantRow, uuid.UUID]:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        return tenant, (await make_account(session, tenant=tenant)).id


def _auth(tenant: TenantRow, account_id: uuid.UUID, *, admin: bool = True) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id,
        tenant_id=tenant.id,
        role=Role.ADMIN if admin else Role.USER,
        platform="discord",
        external_id=tenant.external_id,
        platform_user_id="admin1",
        is_admin=admin,
        is_platform_admin=admin,
        token_kind="cli",
    )


async def test_admin_sets_a_limit_on_a_threads_parent_and_round_trips_revision(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    place = {"scope": "channel", "scope_id": _THREAD}
    result = await _set_limit_impl(
        runtime, auth, **_limit(**place, shared="5", expected_revision=0)
    )
    assert (result.scope_id, result.shared, result.summary) == (
        _PARENT,
        "5",
        "0 of 5 credits used, all time",
    ), "stored on the parent, credits as decimal strings"
    listed = (await _list_limits_impl(runtime, auth)).limits
    assert [(x.scope_id, x.revision) for x in listed] == [(_PARENT, result.revision)]
    changed = await _set_limit_impl(
        runtime, auth, **_limit(**place, shared="0.5", expected_revision=listed[0].revision)
    )
    assert changed.shared == "0.5", "the listed revision is a valid `expected_revision`"
    with pytest.raises(ToolError, match="changed since you opened it.*Nothing was saved"):
        await _set_limit_impl(
            runtime, auth, **_limit(**place, shared="9", expected_revision=result.revision)
        )
    cleared = await _clear_limit_impl(
        runtime, auth, scope="channel", scope_id=_THREAD, person=None, expected_revision=None
    )
    assert (cleared.scope_id, cleared.cleared) == (_PARENT, True), "the parent's limit cleared"


async def test_an_agent_limit_is_stored_under_the_agents_id(
    committing_sessionmaker: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def find(
        client: object, *, tenant_id: uuid.UUID, ref: str, records: object = None
    ) -> str | None:
        return {"helper": "agent_helper1", "agent_helper1": "agent_helper1"}.get(ref)

    monkeypatch.setattr(tools, "agent_id_in_tenant", find)
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    limit = _limit(scope="agent", each_person="2", window="monthly")
    result = await _set_limit_impl(runtime, auth, **{**limit, "scope_id": "helper"})
    assert (result.scope_id, result.summary) == (
        "agent_helper1",
        "2 each, each month",
    ), "the agent's id"
    with pytest.raises(ToolError, match="No agent 'nobody' here"):
        await _set_limit_impl(runtime, auth, **{**limit, "scope_id": "nobody"})
    with pytest.raises(ToolError, match="No agent 'agent_nope' here"):
        await _set_limit_impl(runtime, auth, **{**limit, "scope_id": "agent_nope"})
    kept = await _set_limit_impl(runtime, auth, **{**limit, "scope_id": "agent_helper1"})
    assert kept.scope_id == "agent_helper1", "an existing agent id is taken as it is"


async def test_every_tool_is_admin_only(committing_sessionmaker: Factory) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, member = _runtime(committing_sessionmaker), _auth(tenant, account_id, admin=False)
    calls = [
        _list_limits_impl(runtime, member),
        _set_limit_impl(runtime, member, **_limit(scope="workspace", shared="1")),
        _clear_limit_impl(
            runtime, member, scope="workspace", scope_id=None, person=None, expected_revision=None
        ),
        _get_menus_impl(runtime, member, None),
        _set_menu_impl(runtime, member, key="support", body=_MENU, expected_revision=None),
        _use_menu_impl(
            runtime, member, key="support", scope="workspace", scope_id=None, expected=None
        ),
        _get_channel_impl(runtime, member),
        _set_channel_impl(
            runtime, member, channel_id=_PARENT, service_url=None, expected_revision=None
        ),
        _list_requests_impl(runtime, member, pending=True, scope=None, scope_id=None, limit=50),
    ]
    for call in calls:
        with pytest.raises(ToolError, match="live platform admin"):
            await call
    async with committing_sessionmaker() as session:
        assert await store.list_limits(session, tenant_id=tenant.id) == [], "nothing written"
        assert await store.list_menus(session, tenant_id=tenant.id) == [], "nothing written"


async def test_a_person_limit_sets_under_a_threads_place_and_everywhere_then_clears(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    await _set_limit_impl(runtime, auth, **_limit(scope="channel", scope_id=_PARENT, shared="5"))
    place = await _set_limit_impl(
        runtime,
        auth,
        **_limit(scope="channel", scope_id=_THREAD, each_person="0.5", person=_PERSON),
    )
    assert (place.scope_id, place.person, place.each_person) == (_PARENT, _PERSON, "0.5")
    everywhere = await _set_limit_impl(
        runtime, auth, **_limit(scope="workspace", each_person="3", person=f"<@!{_PERSON}>")
    )
    assert (everywhere.scope, everywhere.person) == ("workspace", _PERSON), "the mention unwrapped"
    listed = await _list_limits_impl(runtime, auth)
    assert [(x.scope, x.person, x.each_person) for x in listed.people] == [
        ("channel", _PERSON, "0.5"),
        ("workspace", _PERSON, "3"),
    ] or {(x.scope, x.each_person) for x in listed.people} == {
        ("channel", "0.5"),
        ("workspace", "3"),
    }
    assert [x.scope_id for x in listed.limits] == [_PARENT], "people are not places"
    with pytest.raises(ToolError, match="Nothing was saved"):
        await _clear_limit_impl(
            runtime,
            auth,
            scope="channel",
            scope_id=_THREAD,
            person=_PERSON,
            expected_revision=place.revision + 1000,
        )
    cleared = await _clear_limit_impl(
        runtime,
        auth,
        scope="channel",
        scope_id=_THREAD,
        person=_PERSON,
        expected_revision=place.revision,
    )
    assert (cleared.person, cleared.cleared) == (_PERSON, True)
    assert len((await _list_limits_impl(runtime, auth)).people) == 1, "the other stays"


async def test_a_person_limit_when_id_is_not_a_platform_id_then_refuses_and_saves_nothing(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    with pytest.raises(ToolError, match="u1 is not a Discord user id"):
        await _set_limit_impl(
            runtime, auth, **_limit(scope="workspace", each_person="2", person="u1")
        )
    async with committing_sessionmaker() as session:
        assert await store.list_limits(session, tenant_id=tenant.id, people=True) == []


async def test_set_limit_when_arguments_left_out_then_keeps_the_limits_values(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    place = _limit(scope="channel", scope_id=_PARENT)
    await _set_limit_impl(
        runtime,
        auth,
        **{
            **place,
            "shared": "5",
            "window": "monthly",
            "starts_at": "2026-11-18T09:00",
            "ends_at": "2026-11-24T09:00",
        },
    )
    changed = await _set_limit_impl(runtime, auth, **{**place, "each_person": "1"})
    assert (changed.shared, changed.each_person, changed.window) == ("5", "1", "monthly")
    assert changed.ends_at == "2026-11-24T09:00:00+00:00", "the dates are kept"
    cleared = await _set_limit_impl(runtime, auth, **{**place, "ends_at": "none"})
    assert (cleared.ends_at, cleared.starts_at is not None) == (None, True), "none clears one date"


async def test_menu_set_when_stale_or_removed_twice_then_says_what_is_there(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    made = await _set_menu_impl(runtime, auth, key="support", body=_MENU, expected_revision=0)
    assert made.summary == "Review (1-3 credits)", "a menu in one line"
    with pytest.raises(ToolError, match=rf"Now: revision {made.revision}\. Nothing was saved\.$"):
        await _set_menu_impl(runtime, auth, key="support", body=_MENU, expected_revision=0)
    (shown,) = await _get_menus_impl(runtime, auth, "support")
    again = await _set_menu_impl(
        runtime, auth, key="support", body=shown.body, expected_revision=shown.revision
    )
    assert again.revision != made.revision, "the shown menu sets back"
    with pytest.raises(ToolError, match="Nothing was saved"):
        await _set_menu_impl(
            runtime, auth, key="support", body={"name": "x"}, expected_revision=None
        )
    removed = await _set_menu_impl(
        runtime, auth, key="support", body=None, expected_revision=again.revision
    )
    assert removed.revision == 0, "removed"
    assert await _get_menus_impl(runtime, auth, None) == [], "no menus left"
    with pytest.raises(ToolError, match="No menu has that key. There are no menus yet. Nothing"):
        await _set_menu_impl(runtime, auth, key="support", body=None, expected_revision=None)


async def test_menu_get_when_key_is_unknown_then_refuses_naming_the_keys_there_are(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    with pytest.raises(ToolError, match="No menu has that key. There are no menus yet."):
        await _get_menus_impl(runtime, auth, "nope")
    await _set_menu_impl(runtime, auth, key="support", body=_MENU, expected_revision=None)
    with pytest.raises(ToolError, match="No menu has that key. Menus: support."):
        await _get_menus_impl(runtime, auth, "nope")
    assert [m.key for m in await _get_menus_impl(runtime, auth, None)] == ["support"]


async def test_menu_use_when_picked_then_default_then_stale_or_unknown(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    await _set_menu_impl(runtime, auth, key="support", body=_MENU, expected_revision=None)
    place = {"scope": "channel", "scope_id": _THREAD}
    picked = await _use_menu_impl(runtime, auth, key=" support ", **place, expected="None")
    assert (picked.scope_id, picked.menu) == (_PARENT, "support"), "the parent uses the menu"
    await _set_limit_impl(runtime, auth, **_limit(scope="channel", scope_id=_PARENT, shared="5"))
    (limit,) = (await _list_limits_impl(runtime, auth)).limits
    assert limit.menu == "support", "the limit listing names the pick"
    with pytest.raises(ToolError, match=r"Now: menu support\. Nothing was saved\.$"):
        await _use_menu_impl(runtime, auth, key="support", **place, expected="none")
    with pytest.raises(ToolError, match="No menu has that key. Menus: support. Nothing"):
        await _use_menu_impl(runtime, auth, key="nope", **place, expected=None)
    with pytest.raises(ToolError, match="Nothing was saved"):
        await _set_menu_impl(runtime, auth, key="support", body=None, expected_revision=None)
    with pytest.raises(ToolError, match="A menu key is 1-32"):
        await _set_menu_impl(runtime, auth, key="Bad Key", body=None, expected_revision=None)
    with pytest.raises(ToolError, match="A menu key is 1-32"):
        await _use_menu_impl(runtime, auth, key="support", **place, expected="Bad Key")
    dropped = await _use_menu_impl(runtime, auth, key="default", **place, expected="support")
    assert dropped.menu is None, "the place's own pick is dropped"
    async with committing_sessionmaker() as session:
        assert await store.list_menu_uses(session, tenant_id=tenant.id) == []


async def test_channel_when_the_deployment_keeps_it_then_refuses(
    committing_sessionmaker: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "own_channel_allowed", lambda settings: False)
    monkeypatch.setattr(support_routing, "own_channel_allowed", lambda settings: False)
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    shown = await _get_channel_impl(runtime, auth)
    assert (shown.own_allowed, shown.channel_id, shown.revision) == (False, None, 0)
    with pytest.raises(
        ToolError,
        match="Requests go to the default channel. Ask whoever runs Daimon. Nothing was saved.",
    ):
        await _set_channel_impl(
            runtime, auth, channel_id=_THREAD, service_url=None, expected_revision=None
        )


@pytest.mark.usefixtures("own_channel")
async def test_channel_when_allowed_then_sets_on_the_parent_and_resets(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    set_ = await _set_channel_impl(
        runtime, auth, channel_id=_THREAD, service_url=None, expected_revision=0
    )
    assert (set_.own_allowed, set_.own, set_.destination, set_.channel_id) == (
        True,
        True,
        _PARENT,
        _PARENT,
    ), "requests go to the thread's parent channel"
    with pytest.raises(ToolError, match=r"Now: revision \d+\. Nothing was saved\."):
        await _set_channel_impl(
            runtime, auth, channel_id=_PARENT, service_url=None, expected_revision=0
        )
    reset = await _set_channel_impl(
        runtime, auth, channel_id=None, service_url=None, expected_revision=set_.revision
    )
    assert (reset.channel_id, reset.own, reset.revision) == (None, False, 0), "back to the default"


@pytest.mark.usefixtures("own_channel")
async def test_channel_when_the_workspace_is_not_the_tenants_then_refuses_and_saves_nothing(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    auth = dataclasses.replace(_auth(tenant, account_id), external_id="another-guild")
    with pytest.raises(ToolError, match="not in this workspace"):
        await _set_channel_impl(
            runtime, auth, channel_id=_PARENT, service_url=None, expected_revision=None
        )
    async with committing_sessionmaker() as session:
        assert await store.get_support_channel(session, tenant_id=tenant.id) is None


@pytest.mark.usefixtures("own_channel")
async def test_channel_on_teams_when_no_service_url_then_refuses_and_with_one_saves_the_team(
    committing_sessionmaker: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def team(runtime: McpRuntime, auth: AuthIdentity, channel_id: str) -> str:
        return "team-1"

    async def teams_channel(
        runtime: McpRuntime, auth: AuthIdentity, raw: str, *, lenient: bool = False
    ) -> object:
        return SimpleNamespace(channel_id=raw)

    monkeypatch.setattr(tools, "_teams_team", team)
    monkeypatch.setattr(tools, "resolve_channel", teams_channel)
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    auth = dataclasses.replace(_auth(tenant, account_id), platform="teams")
    with pytest.raises(ToolError, match="Teams needs service_url"):
        await _set_channel_impl(
            runtime, auth, channel_id="19:c", service_url=None, expected_revision=None
        )
    with pytest.raises(ToolError, match="not in this workspace"):
        await _set_channel_impl(
            runtime,
            auth,
            channel_id="19:c",
            service_url="https://smba.example/",
            expected_revision=None,
        )
    async with committing_sessionmaker.begin() as session:
        await record_teams_installation(
            session, tenant_id=tenant.id, team_id="team-1", group_id="g1", name="Team"
        )
    await _set_channel_impl(
        runtime,
        auth,
        channel_id="19:c",
        service_url="https://smba.example/",
        expected_revision=None,
    )
    async with committing_sessionmaker() as session:
        row = await store.get_support_channel(session, tenant_id=tenant.id)
    assert row is not None, "saved"
    assert (row.teams_team_id, row.teams_service_url) == ("team-1", "https://smba.example/")


async def _submit(
    sessionmaker: Factory,
    tenant: TenantRow,
    *,
    user: str = "u1",
    message: str = "m1",
    menu: dict[str, object] = _MENU,
) -> uuid.UUID:
    async with sessionmaker.begin() as session:
        outcome = await ledger.submit_request(
            session,
            tenant_id=tenant.id,
            account_id=None,
            platform="discord",
            platform_user_id=user,
            channel_id=_PARENT,
            message_id=message,
            ma_session_id=None,
            place=SupportPlace(channel_id=_PARENT, agent_id=None),
            choice=RequestChoice.of(parse_menu(menu).options[0]),
            default_allowance=20,
        )
    assert outcome.row is not None, "recorded"
    return outcome.row.id


async def _menu_and_limit(runtime: McpRuntime, auth: AuthIdentity) -> None:
    await _set_menu_impl(runtime, auth, key="support", body=_MENU, expected_revision=None)
    await _set_limit_impl(runtime, auth, **_limit(scope="channel", scope_id=_PARENT, shared="5"))
    await _use_menu_impl(
        runtime, auth, key="support", scope="channel", scope_id=_PARENT, expected=None
    )


async def test_requests_list_when_undelivered_then_only_requests_no_channel_received(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    await _menu_and_limit(runtime, auth)
    ids = [await _submit(committing_sessionmaker, tenant, message=m) for m in ("m1", "m2")]
    async with committing_sessionmaker.begin() as session:
        await ledger.mark_delivered(session, escalation_id=ids[1])
    every = await _list_requests_impl(
        runtime, auth, pending=False, scope=None, scope_id=None, limit=50
    )
    assert [(r.id, r.delivered) for r in every] == [
        (str(ids[1]), True),
        (str(ids[0]), False),
    ], "each says whether a support channel received it"
    lost = await _list_requests_impl(
        runtime, auth, pending=False, scope=None, scope_id=None, limit=50, undelivered=True
    )
    assert [r.id for r in lost] == [str(ids[0])], "only the one nobody received"
    with pytest.raises(ToolError, match="There is no limit there."):
        await _list_requests_impl(
            runtime, auth, pending=True, scope="channel", scope_id="404", limit=50
        )


async def test_settle_tells_the_settler_the_client_is_not_told_and_corrects(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    await _menu_and_limit(runtime, auth)
    request_id = str(await _submit(committing_sessionmaker, tenant))
    (pending,) = await _list_requests_impl(
        runtime, auth, pending=True, scope="channel", scope_id=_THREAD, limit=50
    )
    assert (pending.id, pending.counted) == (request_id, "1"), "holds its minimum"
    settle = {"request_id": request_id, "expected_charge": None}
    with pytest.raises(ToolError, match="Enter an amount from 1 to 3."):
        await _settle_request_impl(runtime, auth, **settle, charge="9")
    done = await _settle_request_impl(runtime, auth, **settle, charge="2.5")
    assert done.text == f"Charged 2.5 credits. {CLIENT_NOT_NOTIFIED}", "decision 8"
    refund = await _settle_request_impl(
        runtime, auth, request_id=request_id, charge=None, expected_charge="2.5"
    )
    assert (refund.status, refund.charge) == ("declined", "0"), "a correction to a refund"
    with pytest.raises(ToolError, match="No such request."):
        await _settle_request_impl(
            runtime, auth, request_id=str(uuid.uuid4()), charge="1", expected_charge=None
        )


_EXAMPLE = re.compile(r"``(\w+)=([^`]+)``")


def _example_args(doc: str) -> dict[str, object]:
    """The `Example:` of a docstring as arguments: ``name=value`` pairs, values Python literals."""
    tail = " ".join(doc.split("Example:")[1].split("Example::")[0].split())
    return {name: ast.literal_eval(value) for name, value in _EXAMPLE.findall(tail)}


async def test_every_docstring_example_is_valid_and_runs(
    committing_sessionmaker: Factory, own_channel: None
) -> None:
    """Each tool's documented example passes its schema, and the story of them works."""
    mcp = FastMCP(name="t")
    tools.register_support_tools(mcp, MagicMock())
    listed = {t.name: t for t in await mcp.list_tools()}
    assert set(listed) == {
        "support_limit_list",
        "support_limit_set",
        "support_limit_clear",
        "support_menu_get",
        "support_menu_set",
        "support_menu_use",
        "support_channel_get",
        "support_channel_set",
        "support_requests_list",
        "support_requests_settle",
    }, "the tools this page documents"
    menu_doc = listed["support_menu_set"].description or ""
    body = json.loads(menu_doc.split("Example::")[1].split("\n\n")[1])
    examples: dict[str, dict[str, object]] = {
        name: _example_args(tool.description or "")
        for name, tool in listed.items()
        if "Example:" in (tool.description or "") and name != "support_menu_set"
    }
    examples["support_menu_set"] = {"key": "support", "body": body}
    examples["support_requests_settle"] = {"request_id": str(uuid.uuid4()), "charge": "2.5"}
    for name, args in examples.items():
        jsonschema.validate(args, {**listed[name].parameters, "required": []})

    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    limit = examples["support_limit_set"]
    set_ = await _set_limit_impl(runtime, auth, **{**_LIMIT, **limit})
    assert set_.summary == "0 of 5 credits used, 1 each, each month", "the example limit"
    menu = await _set_menu_impl(
        runtime, auth, **examples["support_menu_set"], expected_revision=None
    )
    assert menu.summary.startswith("Review this thread (0.5 credits)"), "the example menu"
    shown = await _get_menus_impl(runtime, auth, **examples["support_menu_get"])
    assert [m.key for m in shown] == ["support"], "the example key is the example menu's"
    use = await _use_menu_impl(runtime, auth, **examples["support_menu_use"], expected=None)
    assert use.menu == "support"
    channel = await _set_channel_impl(
        runtime, auth, **examples["support_channel_set"], service_url=None, expected_revision=None
    )
    assert channel.destination == _PARENT
    await _list_requests_impl(
        runtime,
        auth,
        **{"scope": None, "scope_id": None, "limit": 50, **examples["support_requests_list"]},
    )
    await _set_menu_impl(runtime, auth, key="support", body=_MENU, expected_revision=None)
    await _set_limit_impl(
        runtime, auth, **_limit(scope="channel", scope_id=_PARENT, each_person="5")
    )
    request_id = str(await _submit(committing_sessionmaker, tenant))
    settled = await _settle_request_impl(
        runtime,
        auth,
        **{**examples["support_requests_settle"], "request_id": request_id},
        expected_charge=None,
    )
    assert settled.charge == "2.5"
    cleared = await _clear_limit_impl(
        runtime, auth, **examples["support_limit_clear"], person=None, expected_revision=None
    )
    assert cleared.cleared, "the example place had a limit"
    flat = " ".join(menu_doc.split())
    for number in (MAX_OPTIONS, MAX_FIELDS, MAX_TEXT_CHARS, MAX_PARAGRAPH_CHARS):
        assert f"to {number}" in flat, f"the stated limit {number} is the real one"
