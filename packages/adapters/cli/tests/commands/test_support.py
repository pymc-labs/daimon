"""`daimon support limit | menu | channel | requests` over the audited change service."""

from __future__ import annotations

import json
import shlex
import uuid
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import click
import httpx
import pytest
import typer
from daimon.adapters.cli.commands import support as support_cli
from daimon.adapters.cli.commands.support import (
    channel_reset,
    channel_set,
    channel_show,
    limit_clear,
    limit_list,
    limit_set,
    menu_list,
    menu_rm,
    menu_set,
    menu_show,
    menu_use,
    requests_list,
    requests_settle,
)
from daimon.adapters.cli.main import app
from daimon.adapters.cli.tenant import TenantSelector
from daimon.core import support_routing
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.errors import StoreError
from daimon.core.stores import support as store
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores.security_audit import list_events
from daimon.core.support import (
    CLIENT_NOT_NOTIFIED,
    RequestChoice,
    SupportError,
    SupportPlace,
)
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
        support=SimpleNamespace(
            credits_per_user=20,
            escalation_channel_id=None,
            slack_escalation_channel_id=None,
            slack_escalation_team_id=None,
            teams_escalation_channel_id=None,
        ),
    )


def _allow_own_channel(monkeypatch: pytest.MonkeyPatch, allowed: bool = True) -> None:
    """The `support_own_channel` feature on or off, for the CLI and the routing it shares."""
    monkeypatch.setattr(support_cli, "own_channel_allowed", lambda settings: allowed)
    monkeypatch.setattr(support_routing, "own_channel_allowed", lambda settings: allowed)


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
        menu = await store.get_menu(session, tenant_id=tenant_id, key="support")
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


async def test_menu_when_shown_and_set_back_then_round_trips_and_audits(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    tenant_id, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await menu_set(**where, key="support", text=MENU, expected="none")
    await menu_show(**where, key="support", as_json=False)
    shown = out.getvalue().split("\n", 1)[1]
    assert shown.startswith("# support, revision 1"), "the revision heads the YAML"
    await menu_set(**where, key="support", text=shown, expected="1")
    with pytest.raises(StoreError, match=r"Now: revision 2\.$"):
        await menu_set(**where, key="support", text=MENU, expected="1")
    await menu_list(**where, as_json=True)
    listed = json.loads(out.getvalue().strip().splitlines()[-1])
    assert listed == [
        {
            "key": "support",
            "name": "Support",
            "revision": 2,
            "options": "Review (1-3 credits)",
            "used_by": [],
        }
    ], "the shown YAML set back unchanged"
    await menu_show(**where, key="support", as_json=True)
    assert "body" in json.loads(out.getvalue().strip().splitlines()[-1]), "the JSON key is body"
    with pytest.raises(ChannelChangeRefused, match="No menu has that key. Menus: support."):
        await menu_show(**where, key="suport", as_json=False)
    await menu_rm(**where, key="support", expected="2")
    with pytest.raises(ChannelChangeRefused, match="No menu has that key. There are no menus yet."):
        await menu_rm(**where, key="support")
    async with db_session_factory() as session:
        events = await list_events(session, tenant_id=tenant_id)
    assert sorted(e.tool_name for e in events) == [
        "cli/support menu rm",
        "cli/support menu set",
        "cli/support menu set",
    ], "each write audited once; the refused and empty ones are not"


async def test_menu_use_when_picked_then_listed_under_the_place_and_unknown_keys_refused(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    _, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await menu_set(**where, key="support", text=MENU)
    with pytest.raises(ChannelChangeRefused, match="No menu has that key. Menus: support."):
        await menu_use(**where, key="suport", scope="channel", scope_id="C1")
    await menu_use(**where, key="support", scope="channel", scope_id="C1", expected="none")
    assert "channel C1: uses menu support" in out.getvalue(), "picked"
    await menu_list(**where, as_json=True)
    (listed,) = json.loads(out.getvalue().strip().splitlines()[-1])
    assert listed["used_by"] == ["channel C1"], "listed under its place"
    with pytest.raises(StoreError, match="Now: menu support."):
        await menu_use(**where, key="default", scope="channel", scope_id="C1", expected="none")
    await menu_use(**where, key="default", scope="channel", scope_id="C1", expected="support")
    assert "channel C1: uses the default menu" in out.getvalue(), "dropped"


async def test_limit_when_set_for_an_agent_name_then_stored_under_its_id(
    db_session_factory: Factory,
) -> None:
    tenant_id, selector = await _tenant(db_session_factory)
    router = MARouter()
    agent = _agent_json(agent_id="agent_helper1", name="helper", tenant_id=tenant_id)
    router.add("GET", r"/v1/agents", lambda req, m: list_response([agent]))
    rt = build_cli_runtime(db_session_factory, router=router, settings=_settings())
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await limit_set(**where, scope="agent", scope_id="helper", shared="10", window="monthly")
    assert "agent_helper1" in out.getvalue(), "the agent's id"
    assert "0 of 10 credits used, each month" in out.getvalue(), "summary"
    await limit_list(**where, as_json=True)
    (listed,) = json.loads(out.getvalue().strip().splitlines()[-1])["limits"]
    assert (listed["scope_id"], listed["shared"], listed["menu"]) == (
        "agent_helper1",
        "10",
        None,
    ), "the agent's id, the shared credits and the default menu"
    with pytest.raises(StoreError, match="no agent named"):
        await limit_set(**where, scope="agent", scope_id="nobody", shared="1")


async def test_limit_set_when_expected_is_a_revision_then_accepted_else_stale(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    _, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await limit_set(**where, scope="channel", scope_id="C1", shared="5", expected="none")
    assert "0 of 5 credits used, all time" in out.getvalue(), "no dates reads as all time"
    await limit_set(**where, scope="channel", scope_id="C1", shared="6", expected="1")
    with pytest.raises(StoreError, match=r"changed since you opened it.*Now: revision 2\."):
        await limit_set(**where, scope="channel", scope_id="C1", shared="7", expected="1")
    with pytest.raises(StoreError, match="changed since you opened it"):
        await limit_clear(**where, scope="channel", scope_id="C1", expected="none")
    await limit_clear(**where, scope="channel", scope_id="C1")
    await limit_clear(**where, scope="channel", scope_id="C1")
    assert "channel C1: limit cleared" in out.getvalue(), "cleared"
    assert "channel C1: had no limit" in out.getvalue(), "clearing nothing says so"


async def test_limit_set_when_a_discord_thread_then_its_parent_channel(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings(discord=True))
    _, selector = await _tenant(db_session_factory, platform="discord")
    thread = {"id": "901", "type": 11, "guild_id": "g1", "parent_id": "900"}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=thread))
    console, out = _console()
    await limit_set(
        rt=rt,
        console=console,
        selector=selector,
        scope="channel",
        scope_id="901",
        each_person="2",
        discord_transport=transport,
    )
    assert "900" in out.getvalue() and "901" not in out.getvalue(), "the thread's parent"


async def test_limit_set_when_rerun_with_one_option_then_keeps_the_other_values(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    tenant_id, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await menu_set(**where, key="support", text=MENU)
    await menu_use(**where, key="support", scope="channel", scope_id="C1")
    start = datetime(2030, 5, 1, tzinfo=UTC)
    await limit_set(
        **where,
        scope="channel",
        scope_id="C1",
        shared="5",
        starts_at="2030-05-01T00:00:00",
        ends_at="2030-06-01T00:00:00",
    )
    await limit_set(**where, scope="channel", scope_id="C1", shared="8")

    def last_row() -> str:
        return [line for line in out.getvalue().splitlines() if "C1" in line][-1]

    assert "support" in last_row(), f"the menu is kept: {last_row()}"
    assert (
        "Not started: 0 of 8 credits used, from 1 May 2030 00:00 UTC to 1 Jun 2030 00:00 UTC"
        in " ".join(last_row().split())
    ), f"dates kept, in full: {last_row()}"
    await limit_set(
        **where, scope="channel", scope_id="C1", shared="none", each_person="1", ends_at="none"
    )
    assert "1 each, since" in last_row(), f"shared credits and end cleared: {last_row()}"
    async with rt.sessionmaker() as session:
        row = await store.get_limit(session, tenant_id=tenant_id, scope="channel", scope_id="C1")
    assert row is not None and row.starts_at == start, "the start date kept"


async def test_limit_person_when_set_then_listed_and_cleared_at_its_place(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    _, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await limit_set(**where, scope="channel", scope_id="C1", shared="5", each_person="1")
    await limit_set(**where, scope="channel", scope_id="C1", each_person="4", person="U1")
    await limit_set(**where, scope="workspace", scope_id=None, each_person="9", person="U1")
    await limit_list(**where, as_json=True)
    listed = json.loads(out.getvalue().strip().splitlines()[-1])
    assert [(p["scope"], p["person"], p["each_person"]) for p in listed["people"]] == [
        ("channel", "U1", "4"),
        ("workspace", "U1", "9"),
    ], "each person row at its own place, Everywhere as workspace"
    assert listed["limits"][0]["people"] == {"U1": "4"}, "listed under their place"
    own = listed["people"][0]["revision"]
    with pytest.raises(StoreError, match="changed since you opened it"):
        await limit_clear(
            **where, scope="channel", scope_id="C1", person="U1", expected=str(own + 100)
        )
    await limit_clear(**where, scope="channel", scope_id="C1", person="U1", expected=str(own))
    assert "U1 at channel C1: limit cleared" in out.getvalue(), "cleared"


async def test_limit_person_when_a_mention_or_a_malformed_id_or_place_then_unwraps_or_refuses(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    _, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    with pytest.raises(SupportError, match="alice is not a Slack user id"):
        await limit_set(**where, scope="workspace", scope_id=None, each_person="4", person="alice")
    with pytest.raises(SupportError, match="Everywhere takes no id"):
        await limit_set(**where, scope="workspace", scope_id="C1", each_person="4")
    with pytest.raises(SupportError, match="or a channel, not team."):
        await limit_set(**where, scope="team", scope_id="C1", each_person="4")
    await limit_set(
        **where, scope="workspace", scope_id=None, each_person="4", person="<@U1|alice>"
    )
    assert "U1 at everywhere: 4 credits" in out.getvalue(), f"unwrapped: {out.getvalue()}"


async def test_channel_when_the_deployment_keeps_it_then_refused_else_set_shown_and_reset(
    db_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    _, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    _allow_own_channel(monkeypatch, allowed=False)
    with pytest.raises(ChannelChangeRefused, match="keeps its own request channel"):
        await channel_set(**where, channel_id="C9")
    _allow_own_channel(monkeypatch)
    await channel_set(**where, channel_id="C9", expected="none")
    await channel_show(**where, as_json=True)
    shown = json.loads(out.getvalue().strip().splitlines()[-1])
    assert shown["destination"] == {"channel_id": "C9", "own": True}, "the own channel"
    assert shown["own"]["revision"] == 1, "its revision"
    with pytest.raises(StoreError, match=r"Now: revision 1\."):
        await channel_reset(**where, expected="none")
    await channel_reset(**where, expected="1")
    await channel_show(**where, as_json=False)
    assert "Requests go nowhere" in out.getvalue(), "no default channel is set in the test"


async def test_channel_set_when_teams_then_needs_the_team_and_service_url(
    db_session_factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    tenant_id, selector = await _tenant(db_session_factory, platform="teams")
    console, _ = _console()
    _allow_own_channel(monkeypatch)
    with pytest.raises(typer.BadParameter, match="--team and --service-url"):
        await channel_set(
            rt=rt, console=console, selector=selector, channel_id="19:abc@thread.tacv2"
        )
    await channel_set(
        rt=rt,
        console=console,
        selector=selector,
        channel_id="19:abc@thread.tacv2",
        team="T1",
        service_url="https://smba.example/",
    )
    async with rt.sessionmaker() as session:
        row = await store.get_support_channel(session, tenant_id=tenant_id)
    assert row is not None and (row.teams_team_id, row.teams_service_url) == (
        "T1",
        "https://smba.example/",
    ), "the Teams details are stored"


async def test_requests_when_settled_then_the_settler_is_told_the_client_is_not(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    tenant_id, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await menu_set(**where, key="support", text=MENU)
    await menu_use(**where, key="support", scope="channel", scope_id="C1")
    await limit_set(**where, scope="channel", scope_id="C1", shared="5")
    request_id = await _pending(db_session_factory, tenant_id)
    await requests_list(**where, pending=True, place="channel:C1", as_json=True)
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
    await menu_set(**where, key="support", text=MENU)
    await menu_use(**where, key="support", scope="channel", scope_id="C1")
    await limit_set(**where, scope="channel", scope_id="C1", shared="5")
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
    for group in ("limit", "menu", "channel", "requests"):
        assert group in result.stdout, f"{group} listed"


async def test_menu_when_expected_zero_then_it_means_none_as_in_the_mcp_tool(
    db_session_factory: Factory,
) -> None:
    rt = build_cli_runtime(db_session_factory, settings=_settings())
    _, selector = await _tenant(db_session_factory)
    console, out = _console()
    where = {"rt": rt, "console": console, "selector": selector}
    await menu_set(**where, key="support", text=MENU, expected="0")
    with pytest.raises(StoreError):
        await menu_set(**where, key="support", text=MENU, expected="0")
    with pytest.raises(StoreError):
        await menu_rm(**where, key="support", expected="0")


def _docs_commands() -> list[list[str]]:
    """Each `daimon support ...` line of the support page, as words."""
    page = next(p for p in Path(__file__).parents if (p / "docs" / "support.md").exists())
    lines = (page / "docs" / "support.md").read_text().splitlines()
    return [shlex.split(x)[1:] for x in lines if x.startswith("daimon support ")]


@pytest.mark.parametrize("words", _docs_commands(), ids=" ".join)
def test_docs_commands_when_read_then_name_real_commands_and_options(words: list[str]) -> None:
    """The commands on docs/support.md exist, and so do the options they use."""
    node: click.Command = typer.main.get_command(app)
    for word in words:
        if isinstance(node, click.Group) and word in node.commands:
            node = node.commands[word]
    assert not isinstance(node, click.Group), f"{words} stops at a group"
    options = {o for p in node.params for o in getattr(p, "opts", [])}
    used = [w for w in words if w.startswith("--")]
    assert set(used) <= options, f"{set(used) - options} is not an option of {words}"
