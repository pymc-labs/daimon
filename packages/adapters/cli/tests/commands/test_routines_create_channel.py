"""Operator creation of channel and thread routines runs the same checks as chat."""

from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import daimon.adapters.cli.commands.routines as routines_cli
import pytest
from daimon.adapters.cli.commands.routines import run_create_channel_routine
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.errors import UserFacingError
from daimon.core.membership import CachedMembershipResolver, Membership
from daimon.core.routine_service import Placement, RoutineRefused
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.routines import list_routines_for_tenant
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter
from mux.testing.drivers.anthropic.ma import list_response
from rich.console import Console
from typer.testing import CliRunner

from ..harness import build_cli_runtime


async def _member(*_args):
    return Membership(is_member=True)


async def _setup(db_session_factory, *, restrict=None):
    async with db_session_factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="424242")
        if restrict is not None:
            await set_access_policy(
                session,
                tenant_id=tenant.id,
                policy=TenantAccessPolicy(channel_rules={restrict: ChannelRule(writers="none")}),
            )
    agent = ma_agent(
        id="ag",
        name="daimon",
        metadata={MA_METADATA_KEY_NAME: "daimon", MA_METADATA_KEY_TENANT: str(tenant.id)},
    )
    router = MARouter()
    router.add(
        "GET", r"/v1/agents", lambda _req, _m: list_response([agent.model_dump(mode="json")])
    )
    return tenant, build_cli_runtime(
        db_session_factory, router=router, settings=SimpleNamespace(direct_message_policies={})
    )


def _resolver(placement=None, refusal=None):
    async def resolve(kind, destination_id):
        resolve.calls.append((kind, destination_id))
        if refusal is not None:
            raise RoutineRefused(refusal)
        return placement

    resolve.calls = []
    return resolve


async def _create(rt, tenant, kind, destination_id, resolver, *, dry_run=False, paused=False):
    output = StringIO()
    await run_create_channel_routine(
        rt=rt,
        console=Console(file=output),
        tenant_id=tenant.id,
        owner="123",
        agent_name="daimon",
        schedule="0 9 * * *",
        timezone="UTC",
        trigger="summarize",
        kind=kind,
        destination_id=destination_id,
        resolver=CachedMembershipResolver(_member),
        destination_resolver=resolver,
        dry_run=dry_run,
        paused=paused,
    )
    return output.getvalue()


async def _rows(db_session_factory, tenant):
    async with db_session_factory() as session:
        return await list_routines_for_tenant(session, tenant_id=tenant.id)


async def test_channel_routine_is_owned_by_the_owner_and_posts_there(db_session_factory):
    """The created row names the owner, the channel and the channel as its budget."""
    tenant, rt = await _setup(db_session_factory)
    resolver = _resolver(Placement(channel_id="555"))

    output = await _create(rt, tenant, "channel", "555", resolver)

    (row,) = await _rows(db_session_factory, tenant)
    assert (row.created_by_user_id, row.destination_kind, row.destination_id) == (
        "123",
        "channel",
        "555",
    ), "owner and destination"
    assert row.channel_id == "555", "budget channel"
    assert row.enabled is True and row.next_fire_at is not None, "enabled and scheduled"
    assert resolver.calls == [("channel", "555")], "destination checked once"
    assert f"Created routine {row.id}." in output, "reports the new id"


async def test_thread_routine_budgets_against_the_parent_channel(db_session_factory):
    """A thread destination keeps its own id and counts spend against its parent."""
    tenant, rt = await _setup(db_session_factory)
    resolver = _resolver(Placement(channel_id="777", parent_channel_id="555"))

    await _create(rt, tenant, "thread", "777", resolver, paused=True)

    (row,) = await _rows(db_session_factory, tenant)
    assert (row.destination_kind, row.destination_id, row.channel_id) == ("thread", "777", "555")
    assert row.enabled is False, "paused routine is not enabled"


async def test_dry_run_checks_everything_and_writes_nothing(db_session_factory):
    """A dry run reaches the resolver and reports the plan, but saves no routine."""
    tenant, rt = await _setup(db_session_factory)
    resolver = _resolver(Placement(channel_id="555"))

    output = await _create(rt, tenant, "channel", "555", resolver, dry_run=True)

    assert await _rows(db_session_factory, tenant) == [], "nothing saved"
    assert resolver.calls == [("channel", "555")], "destination still verified"
    assert "Dry run: no routine created. Would be created enabled." in output, "plan reported"


@pytest.mark.parametrize(
    "refusal",
    [
        "555 is a thread: use destination thread",
        "The owner can't post in 555, so a routine can't deliver there for them.",
        "555 is not in this server. Nothing was saved.",
    ],
)
async def test_destination_refusals_reach_the_operator_and_save_nothing(
    db_session_factory, refusal
):
    """A wrong kind, an owner who can't post, or a foreign guild is refused as plain text."""
    tenant, rt = await _setup(db_session_factory)

    with pytest.raises(UserFacingError, match=refusal.split(".")[0]):
        await _create(rt, tenant, "channel", "555", _resolver(refusal=refusal))

    assert await _rows(db_session_factory, tenant) == [], "nothing saved"


async def test_access_policy_still_applies_to_the_destination(db_session_factory):
    """A channel the tenant protects is refused even when Discord lets the owner post."""
    tenant, rt = await _setup(db_session_factory, restrict="555")

    with pytest.raises(UserFacingError, match="rule lets nobody write"):
        await _create(rt, tenant, "channel", "555", _resolver(Placement(channel_id="555")))

    assert await _rows(db_session_factory, tenant) == [], "nothing saved"


async def test_unverified_owner_is_refused_before_any_destination_lookup(db_session_factory):
    """A user who is not a verified member never reaches the Discord lookup."""
    tenant, rt = await _setup(db_session_factory)
    resolver = _resolver(Placement(channel_id="555"))

    async def unknown(*_args):
        return Membership()

    with pytest.raises(UserFacingError, match="verified"):
        await run_create_channel_routine(
            rt=rt,
            console=Console(file=StringIO()),
            tenant_id=tenant.id,
            owner="123",
            agent_name="daimon",
            schedule="0 9 * * *",
            timezone="UTC",
            trigger="summarize",
            kind="channel",
            destination_id="555",
            resolver=CachedMembershipResolver(unknown),
            destination_resolver=resolver,
        )

    assert resolver.calls == [], "no lookup for an unverified owner"


def _invoke(monkeypatch, *extra):
    runtime = object()

    @asynccontextmanager
    async def build_runtime(_settings):
        yield runtime

    settings = SimpleNamespace(
        discord=SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "offline"))
    )
    mocks = {name: AsyncMock() for name in ("run_create_dm_routine", "run_create_channel_routine")}
    monkeypatch.setattr(routines_cli, "load_settings", lambda: settings)
    monkeypatch.setattr(routines_cli, "build_runtime", build_runtime)
    for name, mock in mocks.items():
        monkeypatch.setattr(routines_cli, name, mock)
    args = [
        "create",
        "--tenant",
        "00000000-0000-0000-0000-000000000001",
        "--owner",
        "123",
        "--agent",
        "daimon",
        "--schedule",
        "0 9 * * *",
        "--timezone",
        "UTC",
        "--trigger",
        "summarize",
        *extra,
    ]
    return CliRunner().invoke(routines_cli.routines_app, args), mocks


def test_command_routes_a_thread_destination_to_the_channel_path(monkeypatch):
    """--destination thread with an id calls the channel path and not the DM one."""
    result, mocks = _invoke(monkeypatch, "--destination", "thread", "--destination-id", "777")

    assert result.exit_code == 0, result.output
    mocks["run_create_channel_routine"].assert_awaited_once()
    mocks["run_create_dm_routine"].assert_not_awaited()
    kwargs = mocks["run_create_channel_routine"].call_args.kwargs
    assert (kwargs["kind"], kwargs["destination_id"]) == ("thread", "777"), "kind and id forwarded"


@pytest.mark.parametrize(
    "extra",
    [
        ("--destination", "channel"),
        ("--destination", "dm", "--destination-id", "555"),
        ("--destination", "channel", "--destination-id", "555", "--source-channel", "9"),
    ],
)
def test_command_rejects_inconsistent_destination_options(monkeypatch, extra):
    """The id is required for a channel or thread, refused for dm, and no source is taken."""
    result, mocks = _invoke(monkeypatch, *extra)

    assert result.exit_code != 0, "inconsistent options must fail"
    mocks["run_create_channel_routine"].assert_not_awaited()
    mocks["run_create_dm_routine"].assert_not_awaited()
