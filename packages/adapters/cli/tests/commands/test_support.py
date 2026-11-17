"""`daimon support catalog | plan | allowance | requests` over the audited change service."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace

import httpx
import pytest
from daimon.adapters.cli.commands.support import (
    allowance_set,
    catalog_list,
    catalog_rm,
    catalog_set,
    catalog_show,
    plan_clear,
    plan_list,
    plan_set,
    plan_show,
    requests_list,
    requests_settle,
)
from daimon.adapters.cli.main import app
from daimon.adapters.cli.tenant import TenantSelector
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.errors import StoreError
from daimon.core.stores import support as plans
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.security_audit import list_events
from daimon.core.support import (
    CLIENT_NOT_NOTIFIED,
    RequestChoice,
    SupportError,
    SupportPlace,
)
from daimon.core.support_admin import window_text
from daimon.core.support_changes import PlanValue
from daimon.testing.factories import make_tenant
from mux.testing.drivers.anthropic.ma import MARouter, list_response
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from ..harness import FakeCliSettings, build_cli_runtime
from .test_agents import _agent_json  # pyright: ignore[reportPrivateUsage]

type Factory = async_sessionmaker[AsyncSession]

MENU = """
name: Support
options:
  - key: review
    label: Review
    price: {kind: range, min: "1", max: "3"}
"""


def _settings(*, discord: bool = False) -> object:
    bot = SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "t"))
    return SimpleNamespace(
        cli=FakeCliSettings.cli,
        discord=bot if discord else None,
        support=SimpleNamespace(credits_per_user=20),
    )


def _console() -> tuple[Console, StringIO]:
    out = StringIO()
    return Console(file=out, width=200, highlight=False), out


async def _tenant(factory: Factory, platform: str = "slack") -> tuple[uuid.UUID, TenantSelector]:
    async with factory.begin() as session:
        tenant = await make_tenant(session, platform=platform, workspace_id="g1")
    return tenant.id, TenantSelector(tenant_id=str(tenant.id), guild_id=None)


async def _pending(factory: Factory, tenant_id: uuid.UUID, message_id: str = "m1") -> uuid.UUID:
    """A pending Review request on the tenant's `support` menu, as it stands."""
    async with factory.begin() as session:
        menu = await plans.get_catalog(session, tenant_id=tenant_id, key="support")
        review = None if menu is None else menu.spec.option("review")
        assert review is not None, "the menu has Review"
        outcome = await ledger.submit_request(
            session,
            tenant_id=tenant_id,
            account_id=None,
            platform="slack",
            platform_user_id="U1",
            channel_id="C1",
            message_id=message_id,
            ma_session_id=None,
            place=SupportPlace(channel_id="C1", agent_id=None),
            choice=RequestChoice.of(review),
            default_allowance=20,
        )
    assert outcome.row is not None, "a pending request"
    return outcome.row.id


async def test_catalog_when_shown_and_set_back_then_round_trips_and_audits(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    tenant_id, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await catalog_set(**where, key="support", text=MENU, expected="none")
    await catalog_show(**where, key="support", as_json=False)
    shown = out.getvalue().split("\n", 1)[1]
    assert shown.startswith("# support, revision 1"), "the revision heads the YAML"
    await catalog_set(**where, key="support", text=shown, expected="1")
    with pytest.raises(StoreError, match=r"Now: revision 2\.$"):
        await catalog_set(**where, key="support", text=MENU, expected="1")
    await catalog_list(**where, as_json=True)
    listed = json.loads(out.getvalue().strip().splitlines()[-1])
    assert listed == [
        {"key": "support", "name": "Support", "revision": 2, "options": "Review (1-3 credits)"}
    ], "the shown YAML set back unchanged"
    with pytest.raises(ChannelChangeRefused, match="key suport. Menus: support."):
        await catalog_show(**where, key="suport", as_json=False)
    await catalog_rm(**where, key="support", expected="2")
    with pytest.raises(ChannelChangeRefused, match="key support. There are no menus yet."):
        await catalog_rm(**where, key="support")
    async with db_session_factory() as session:
        events = await list_events(session, tenant_id=tenant_id)
    assert sorted(e.tool_name for e in events) == [
        "cli/support catalog rm",
        "cli/support catalog set",
        "cli/support catalog set",
    ], "each write audited once; the refused and empty ones are not"


async def test_plan_when_set_for_an_agent_name_then_stored_under_its_id(
    db_session_factory: Factory,
) -> None:
    tenant_id, selector = await _tenant(db_session_factory)
    router = MARouter()
    agent = _agent_json(agent_id="agent_helper1", name="helper", tenant_id=tenant_id)
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent]))
    rt = build_cli_runtime(db_session_factory, router=router, settings=_settings())
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await plan_set(**where, scope="agent", scope_id="helper", pool="10", window="monthly")
    assert "agent_helper1" in out.getvalue(), "the agent's id"
    assert "0 of 10 credits used, this month" in out.getvalue(), "summary"
    await plan_list(**where, as_json=True)
    (listed,) = json.loads(out.getvalue().strip().splitlines()[-1])
    assert (listed["scope_id"], listed["pool"], listed["catalog"]) == (
        "agent_helper1",
        "10",
        None,
    ), "the agent's id, the pool and the default menu"
    with pytest.raises(StoreError, match="no agent named"):
        await plan_set(**where, scope="agent", scope_id="nobody", pool="1")


async def test_plan_set_when_expected_is_what_show_printed_then_accepted_else_stale(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    _, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await plan_set(**where, scope="channel", scope_id="C1", pool="5", expected="none")
    await plan_show(**where, scope="channel", scope_id="C1", as_json=True)
    shown = out.getvalue().strip().splitlines()[-1]
    await plan_set(**where, scope="channel", scope_id="C1", pool="6", expected=shown)
    with pytest.raises(StoreError, match="changed since you opened it"):
        await plan_set(**where, scope="channel", scope_id="C1", pool="7", expected=shown)
    with pytest.raises(StoreError, match="changed since you opened it"):
        await plan_clear(**where, scope="channel", scope_id="C1", expected="none")
    await plan_clear(**where, scope="channel", scope_id="C1")
    await plan_clear(**where, scope="channel", scope_id="C1")
    assert "channel C1: plan cleared" in out.getvalue(), "cleared"
    assert "channel C1: had no plan" in out.getvalue(), "clearing nothing says so"


async def test_plan_set_when_a_discord_thread_then_its_parent_channel(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings(discord=True))
    _, selector = await _tenant(db_session_factory, platform="discord")
    thread = {"id": "901", "type": 11, "guild_id": "g1", "parent_id": "900"}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=thread))
    console, out = _console()
    await plan_set(
        rt=rt,
        console=console,
        selector=selector,
        scope="channel",
        scope_id="901",
        per_person="2",
        discord_transport=transport,
    )
    assert "900" in out.getvalue() and "901" not in out.getvalue(), "the thread's parent"


async def test_plan_set_when_rerun_with_one_option_then_keeps_menu_and_dates(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    _, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await catalog_set(**where, key="support", text=MENU)
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    dates = {"starts_at": start.isoformat(), "ends_at": (start + timedelta(days=6)).isoformat()}
    await plan_set(
        **where,
        scope="channel",
        scope_id="C1",
        pool="5",
        catalog="support",
        window="fixed",
        **dates,
    )
    await plan_set(**where, scope="channel", scope_id="C1", pool="8")

    def last_row() -> str:
        return [line for line in out.getvalue().splitlines() if "C1" in line][-1]

    window = window_text(
        PlanValue("channel", "C1", None, None, None, "fixed", start, start + timedelta(days=6)),
        now=datetime.now(UTC),
    )
    assert "support" in last_row(), f"the menu is kept: {last_row()}"
    assert f"0 of 8 credits used, {window}" in last_row(), f"dates kept, in full: {last_row()}"
    await plan_set(
        **where, scope="channel", scope_id="C1", window="total", pool="none", per_person="1"
    )
    assert "1 per person, since" in last_row(), f"pool and end cleared: {last_row()}"


async def test_allowance_when_set_then_listed_and_cleared_under_its_plan(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    _, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    with pytest.raises(ChannelChangeRefused, match="no plan there"):
        await allowance_set(**where, platform_user_id="U1", credits="4", plan="channel:C1")
    await plan_set(**where, scope="channel", scope_id="C1", pool="5", per_person="1")
    await allowance_set(**where, platform_user_id="U1", credits="4", plan="channel:C1")
    await plan_show(**where, scope="channel", scope_id="C1", as_json=True)
    shown = json.loads(out.getvalue().strip().splitlines()[-1])
    assert shown["allowances"] == {"U1": "4"}, "the person's own cap"
    await allowance_set(
        **where, platform_user_id="U1", credits=None, plan="channel:C1", expected="4"
    )
    assert "U1 under the channel C1: cap cleared" in out.getvalue(), "cleared"


async def test_allowance_when_a_mention_or_a_malformed_id_or_plan_then_unwraps_or_hints(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    _, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    with pytest.raises(SupportError, match="alice is not a Slack user id"):
        await allowance_set(**where, platform_user_id="alice", credits="4")
    with pytest.raises(SupportError, match="Use workspace, agent:<name> or channel:C1."):
        await allowance_set(**where, platform_user_id="U1", credits="4", plan="C1")
    with pytest.raises(SupportError, match="or a channel, not team."):
        await allowance_set(**where, platform_user_id="U1", credits="4", plan="team:C1")
    await allowance_set(**where, platform_user_id="<@U1|alice>", credits="4")
    assert "U1 under the default plan: cap 4" in out.getvalue(), f"unwrapped: {out.getvalue()}"


async def test_requests_when_settled_then_the_settler_is_told_the_client_is_not(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    tenant_id, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await catalog_set(**where, key="support", text=MENU)
    await plan_set(**where, scope="channel", scope_id="C1", pool="5", catalog="support")
    request_id = await _pending(db_session_factory, tenant_id)
    await requests_list(**where, pending=True, plan="channel:C1", as_json=True)
    (listed,) = json.loads(out.getvalue().strip().splitlines()[-1])
    assert (listed["status"], listed["counted"]) == ("pending", "1"), "holds its minimum"
    with pytest.raises(StoreError, match="Enter an amount from 1 to 3."):
        await requests_settle(**where, request_id=str(request_id), charge="9")
    await requests_settle(**where, request_id=str(request_id), charge="2")
    assert f"Charged 2 credits. {CLIENT_NOT_NOTIFIED}" in out.getvalue(), "decision 8"
    with pytest.raises(StoreError, match="Already charged 2 credits"):
        await requests_settle(**where, request_id=str(request_id), charge=None)
    await requests_settle(**where, request_id=str(request_id), charge=None, expected="2")
    assert f"Declined. No credits used. {CLIENT_NOT_NOTIFIED}" in out.getvalue(), "a refund"
    with pytest.raises(StoreError, match="No such request."):
        await requests_settle(**where, request_id=str(uuid.uuid4()), charge="1")


async def test_requests_list_when_undelivered_then_only_requests_no_channel_received(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    tenant_id, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await catalog_set(**where, key="support", text=MENU)
    await plan_set(**where, scope="channel", scope_id="C1", pool="5", catalog="support")
    lost = await _pending(db_session_factory, tenant_id)
    posted = await _pending(db_session_factory, tenant_id, message_id="m2")
    async with db_session_factory.begin() as session:
        await ledger.mark_delivered(session, escalation_id=posted)
    await requests_list(**where, undelivered=True, as_json=True)
    listed = json.loads(out.getvalue().strip().splitlines()[-1])
    assert [(r["id"], r["delivered"]) for r in listed] == [(str(lost), False)], "only the lost"
    await requests_list(**where)
    cells = {
        line.split()[1]: line.split()[-2]
        for line in out.getvalue().splitlines()
        if "-" in line[2:12]
    }
    assert (cells[str(lost)], cells[str(posted)]) == ("no", "yes"), "a delivered column"


def test_support_help_when_invoked_then_lists_each_group() -> None:
    result = CliRunner().invoke(app, ["support", "--help"])
    assert result.exit_code == 0, result.stdout
    for group in ("catalog", "plan", "allowance", "requests"):
        assert group in result.stdout, f"{group} listed"


async def test_catalog_when_expected_zero_then_it_means_none_as_in_the_mcp_tool(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    _, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await catalog_set(**where, key="support", text=MENU, expected="0")
    with pytest.raises(StoreError):
        await catalog_set(**where, key="support", text=MENU, expected="0")
    with pytest.raises(StoreError):
        await catalog_rm(**where, key="support", expected="0")
