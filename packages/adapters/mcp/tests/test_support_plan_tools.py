"""DB-backed tests for the support plan MCP tools.

The Discord visibility lookup is patched on `_channel_target`, as in
`test_channel_budget_tools.py`; the agent lookup is patched on the tool module.
"""

from __future__ import annotations

import dataclasses
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _channel_target as channel_target
from daimon.adapters.mcp.tools import support_plans as tools
from daimon.adapters.mcp.tools.support_plans import (
    _clear_plan_impl,  # pyright: ignore[reportPrivateUsage]
    _get_catalogs_impl,  # pyright: ignore[reportPrivateUsage]
    _list_plans_impl,  # pyright: ignore[reportPrivateUsage]
    _list_requests_impl,  # pyright: ignore[reportPrivateUsage]
    _set_allowance_impl,  # pyright: ignore[reportPrivateUsage]
    _set_catalog_impl,  # pyright: ignore[reportPrivateUsage]
    _set_plan_impl,  # pyright: ignore[reportPrivateUsage]
    _settle_request_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import AnthropicSettings, DatabaseSettings, McpSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores import support_plans as store
from daimon.core.stores.domain import Role, TenantRow
from daimon.core.support_plans import (
    CLIENT_NOT_NOTIFIED,
    RequestChoice,
    SupportPlace,
    parse_catalog,
)
from daimon.testing.factories import make_account, make_tenant
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
_PLAN = {
    "scope_id": None,
    "catalog": None,
    "pool": None,
    "per_person": None,
    "window": "total",
    "starts_at": None,
    "ends_at": None,
    "expected": None,
}


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
        platform_user_id="admin1",
        is_admin=admin,
        is_platform_admin=admin,
        token_kind="cli",
    )


async def test_admin_sets_a_plan_on_a_threads_parent_and_round_trips_expected(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    plan = {**_PLAN, "scope": "channel", "scope_id": _THREAD}
    result = await _set_plan_impl(runtime, auth, **{**plan, "pool": "5", "expected": "none"})
    assert (result.scope_id, result.pool, result.summary) == (
        _PARENT,
        "5",
        "0 of 5 credits used",
    ), "stored on the parent, credits as decimal strings"
    (listed,) = await _list_plans_impl(runtime, auth)
    shown = dataclasses.asdict(listed)
    changed = await _set_plan_impl(runtime, auth, **{**plan, "pool": "0.5", "expected": shown})
    assert changed.pool == "0.5", "the plan list result is a valid `expected`"
    with pytest.raises(ToolError, match="changed since you opened it.*Nothing was saved"):
        await _set_plan_impl(runtime, auth, **{**plan, "pool": "9", "expected": shown})
    cleared = await _clear_plan_impl(
        runtime, auth, scope="channel", scope_id=_THREAD, expected=None
    )
    assert (cleared.scope_id, cleared.cleared) == (_PARENT, True), "the parent's plan cleared"


async def test_an_agent_plan_is_stored_under_the_agents_id(
    committing_sessionmaker: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def find(client: object, *, tenant_id: uuid.UUID, name: str, records: object) -> object:
        return SimpleNamespace(id="agent_helper1") if name == "helper" else None

    monkeypatch.setattr(tools, "find_agent_by_daimon_tag", find)
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    plan = {**_PLAN, "scope": "agent", "per_person": "2", "window": "monthly"}
    result = await _set_plan_impl(runtime, auth, **{**plan, "scope_id": "helper"})
    assert (result.scope_id, result.summary) == (
        "agent_helper1",
        "2 per person, this month",
    ), "the agent's id"
    with pytest.raises(ToolError, match="No agent named 'nobody'"):
        await _set_plan_impl(runtime, auth, **{**plan, "scope_id": "nobody"})


@pytest.mark.parametrize("call", ["list", "set", "clear", "catalog", "allowance", "requests"])
async def test_every_tool_is_admin_only(committing_sessionmaker: Factory, call: str) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, member = _runtime(committing_sessionmaker), _auth(tenant, account_id, admin=False)
    with pytest.raises(ToolError, match="live platform admin"):
        if call == "list":
            await _list_plans_impl(runtime, member)
        elif call == "set":
            await _set_plan_impl(runtime, member, **{**_PLAN, "scope": "workspace", "pool": "1"})
        elif call == "clear":
            await _clear_plan_impl(runtime, member, scope="workspace", scope_id=None, expected=None)
        elif call == "catalog":
            await _set_catalog_impl(
                runtime, member, key="support", catalog=_MENU, expected_revision=None
            )
        elif call == "allowance":
            await _set_allowance_impl(
                runtime,
                member,
                platform_user_id="u1",
                credits="1",
                scope=None,
                scope_id=None,
                expected=None,
            )
        else:
            await _list_requests_impl(
                runtime, member, pending=True, scope=None, scope_id=None, limit=50
            )
    async with committing_sessionmaker() as session:
        assert await store.list_plans(session, tenant_id=tenant.id) == [], "nothing written"


async def test_a_cap_sets_under_a_threads_plan_and_the_default_then_clears(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    await _set_plan_impl(
        runtime, auth, **{**_PLAN, "scope": "channel", "scope_id": _PARENT, "pool": "5"}
    )

    async def cap(
        credits: str | None, *, scope: str | None = "channel", expected: str | None = None
    ):
        return await _set_allowance_impl(
            runtime,
            auth,
            platform_user_id="u1",
            credits=credits,
            scope=scope,
            scope_id=_THREAD if scope else None,
            expected=expected,
        )

    capped = await cap("0.5")
    assert (capped.scope_id, capped.credits) == (_PARENT, "0.5"), "the thread's parent plan"
    (listed,) = await _list_plans_impl(runtime, auth)
    assert listed.allowances == {"u1": "0.5"}, "listed under the plan"
    default = await cap("3", scope=None)
    assert (default.scope, default.credits) == (None, "3"), "the default allowance"
    with pytest.raises(ToolError, match="Nothing was saved"):
        await cap(None, expected="2")
    assert (await cap(None, expected="0.5")).credits is None, "cleared"
    (listed,) = await _list_plans_impl(runtime, auth)
    assert listed.allowances == {}, "back to the plan's cap"


async def test_catalog_set_checks_the_revision_and_removes_with_none(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    made = await _set_catalog_impl(runtime, auth, key="support", catalog=_MENU, expected_revision=0)
    assert (made.revision, made.summary) == (1, "Review (1-3 credits)"), "a menu in one line"
    with pytest.raises(ToolError, match='"revision": 1'):
        await _set_catalog_impl(runtime, auth, key="support", catalog=_MENU, expected_revision=0)
    (shown,) = await _get_catalogs_impl(runtime, auth, "support")
    again = await _set_catalog_impl(
        runtime, auth, key="support", catalog=shown.catalog, expected_revision=shown.revision
    )
    assert again.revision == 2, "the shown menu sets back"
    with pytest.raises(ToolError, match="Nothing was saved"):
        await _set_catalog_impl(
            runtime, auth, key="support", catalog={"name": "x"}, expected_revision=None
        )
    removed = await _set_catalog_impl(
        runtime, auth, key="support", catalog=None, expected_revision=2
    )
    assert removed.revision == 0, "removed"
    assert await _get_catalogs_impl(runtime, auth, None) == [], "no menus left"


async def test_requests_list_when_undelivered_then_only_requests_no_channel_received(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    await _set_catalog_impl(runtime, auth, key="support", catalog=_MENU, expected_revision=None)
    await _set_plan_impl(
        runtime,
        auth,
        **{**_PLAN, "scope": "channel", "scope_id": _PARENT, "catalog": "support", "pool": "5"},
    )
    ids: list[uuid.UUID] = []
    for message in ("m1", "m2"):
        async with committing_sessionmaker.begin() as session:
            outcome = await ledger.submit_request(
                session,
                tenant_id=tenant.id,
                account_id=None,
                platform="discord",
                platform_user_id="1",
                channel_id=_PARENT,
                message_id=message,
                ma_session_id=None,
                place=SupportPlace(channel_id=_PARENT, agent_id=None),
                choice=RequestChoice.of(parse_catalog(_MENU).options[0]),
                default_allowance=20,
            )
        assert outcome.row is not None, "recorded"
        ids.append(outcome.row.id)
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


async def test_settle_tells_the_settler_the_client_is_not_told_and_corrects(
    committing_sessionmaker: Factory,
) -> None:
    tenant, account_id = await _seed(committing_sessionmaker)
    runtime, auth = _runtime(committing_sessionmaker), _auth(tenant, account_id)
    await _set_catalog_impl(runtime, auth, key="support", catalog=_MENU, expected_revision=None)
    await _set_plan_impl(
        runtime,
        auth,
        **{**_PLAN, "scope": "channel", "scope_id": _PARENT, "catalog": "support", "pool": "5"},
    )
    async with committing_sessionmaker.begin() as session:
        outcome = await ledger.submit_request(
            session,
            tenant_id=tenant.id,
            account_id=None,
            platform="discord",
            platform_user_id="u1",
            channel_id=_PARENT,
            message_id="m1",
            ma_session_id=None,
            place=SupportPlace(channel_id=_PARENT, agent_id=None),
            choice=RequestChoice.of(parse_catalog(_MENU).options[0]),
            default_allowance=20,
        )
    assert outcome.row is not None, "a pending request"
    request_id = str(outcome.row.id)
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
