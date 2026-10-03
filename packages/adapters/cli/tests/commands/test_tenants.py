from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from io import StringIO
from typing import cast

import pytest
import typer
from anthropic import AsyncAnthropic
from click import Group, Option
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.commands import tenants as tenants_mod
from daimon.adapters.cli.commands.tenants import (
    _ended_isolation_warning,  # pyright: ignore[reportPrivateUsage]
    tenants_access_policy_get,
    tenants_access_policy_set,
    tenants_cap,
    tenants_credit,
    tenants_delete,
    tenants_list,
    tenants_turn_cap,
)
from daimon.adapters.cli.runtime import CliRuntime
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import ChannelScopeRef, TenantScopeRef
from daimon.core.stores import scoped_config_write, tenant_ledger, tenant_user_caps
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    load_access_policy,
)
from daimon.core.stores.tenants import get_tenant, get_turn_cap
from daimon.testing import MARouter, ma_agent, ma_environment
from daimon.testing.factories import make_tenant
from rich.console import Console
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from typer.main import get_command

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed


class _FakeCli:
    local_user = "testuser"


class _FakeSettings:
    cli = _FakeCli()


def _make_console() -> Console:
    """Console that writes to a StringIO for test output capture."""
    return Console(file=StringIO(), force_terminal=False, highlight=False, width=120)


def test_credit_uses_note_option() -> None:
    command = get_command(tenants_mod.tenants_app)
    assert isinstance(command, Group)
    flags = {
        flag
        for param in command.commands["credit"].params
        if isinstance(param, Option)
        for flag in param.opts
    }
    assert "--note" in flags
    assert "--reason" not in flags


@pytest.mark.asyncio
async def test_credit_is_positive_and_idempotent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = build_cli_runtime(db_session_factory)
    console = _make_console()
    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="credit-guild"
    )
    async with db_session_factory() as s:
        before = await tenant_ledger.get_balance(s, tenant_id=result.tenant_id)
    for _ in range(2):
        await tenants_credit(
            rt=rt,
            console=console,
            platform="discord",
            workspace_id="credit-guild",
            usd="25.00",
            note="Hackathon grant: team #3",
            request_id="grant-1",
        )
    async with db_session_factory() as s:
        after = await tenant_ledger.get_balance(s, tenant_id=result.tenant_id)
        rows = await tenant_ledger.list_for_tenant(s, tenant_id=result.tenant_id)
    assert after == before + Decimal("25.00")
    credits = [row for row in rows if row.reason == "manual_credit"]
    assert len(credits) == 1
    assert credits[0].idempotency_key == (
        f"manual:credit:{result.tenant_id}:hackathon-grant-team-3:usd25.00:grant-1"
    )
    assert "already credited" in cast(StringIO, console.file).getvalue()
    assert "credit id: grant-1" in cast(StringIO, console.file).getvalue()
    with pytest.raises(typer.BadParameter, match="positive"):
        await tenants_credit(
            rt=rt,
            console=console,
            platform="discord",
            workspace_id="credit-guild",
            usd="-1",
            note="invalid",
            request_id=None,
        )
    with pytest.raises(typer.BadParameter, match="note"):
        await tenants_credit(
            rt=rt,
            console=console,
            platform="discord",
            workspace_id="credit-guild",
            usd="1",
            note="!!!",
            request_id=None,
        )


@pytest.mark.asyncio
async def test_cap_sets_default_and_user_override(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = build_cli_runtime(db_session_factory)
    console = _make_console()
    result = await provision_tenant(db_session_factory, platform="slack", workspace_id="cap-team")
    await tenants_cap(
        rt=rt,
        console=console,
        platform="slack",
        workspace_id="cap-team",
        usd="10.00",
        user=None,
    )
    await tenants_cap(
        rt=rt,
        console=console,
        platform="slack",
        workspace_id="cap-team",
        usd="5.00",
        user="U123",
    )
    async with db_session_factory() as s:
        assert await tenant_user_caps.get_effective_cap(
            s, tenant_id=result.tenant_id, user_id="other"
        ) == Decimal("10.00")
        assert await tenant_user_caps.get_effective_cap(
            s, tenant_id=result.tenant_id, user_id="U123"
        ) == Decimal("5.00")


@pytest.mark.asyncio
async def test_tenants_delete_removes_tenant_when_no_dependents(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    console = _make_console()

    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-123")

    await tenants_delete(
        rt=rt,
        console=console,
        platform="discord",
        external_id="guild-123",
        cascade=False,
        yes=True,
    )

    tenant_id = derive_tenant_uuid(platform="discord", workspace_id="guild-123")
    async with db_session_factory() as s, s.begin():
        row = await get_tenant(s, tenant_id)
    assert row is None, "tenant should be deleted when no dependents exist"


@pytest.mark.asyncio
async def test_tenants_delete_refuses_when_dependents_exist_without_cascade(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    console = _make_console()

    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-456"
    )
    tenant_id = result.tenant_id

    async with db_session_factory() as s, s.begin():
        await scoped_config_write.set_fields(
            s,
            scope=TenantScopeRef(tenant_id=tenant_id),
            tenant_id=tenant_id,
            agent_name="a1",
        )

    with pytest.raises(StoreError, match="dependents"):
        await tenants_delete(
            rt=rt,
            console=console,
            platform="discord",
            external_id="guild-456",
            cascade=False,
            yes=True,
        )

    async with db_session_factory() as s, s.begin():
        row = await get_tenant(s, tenant_id)
    assert row is not None, "tenant should still exist after refused delete"


@pytest.mark.asyncio
async def test_tenants_delete_cascade_deletes_tenant_when_dependents_exist(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    console = _make_console()

    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-789"
    )
    tenant_id = result.tenant_id

    async with db_session_factory() as s, s.begin():
        await scoped_config_write.set_fields(
            s,
            scope=TenantScopeRef(tenant_id=tenant_id),
            tenant_id=tenant_id,
            agent_name="a1",
        )

    await tenants_delete(
        rt=rt,
        console=console,
        platform="discord",
        external_id="guild-789",
        cascade=True,
        yes=True,
    )

    async with db_session_factory() as s, s.begin():
        row = await get_tenant(s, tenant_id)
    assert row is None, "tenant should be deleted (DB cascade removes dependents)"


@pytest.mark.asyncio
async def test_tenants_delete_raises_when_tenant_not_found(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    console = _make_console()

    with pytest.raises(StoreError, match="not found"):
        await tenants_delete(
            rt=rt,
            console=console,
            platform="discord",
            external_id="nonexistent-guild",
            cascade=False,
            yes=True,
        )


@pytest.mark.asyncio
async def test_tenants_list_returns_all_tenants(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    console = _make_console()

    async with db_session_factory() as s, s.begin():
        await make_tenant(s, platform="discord", workspace_id="list-guild-1")
        await make_tenant(s, platform="cli", workspace_id="local")

    await tenants_list(rt=rt, console=console, platform=None, as_json=True)

    out = cast(StringIO, console.file).getvalue()
    data = json.loads(out)
    assert len(data) >= 2, "should return at least 2 tenants"
    external_ids = {d["external_id"] for d in data}
    assert "list-guild-1" in external_ids, "should contain discord tenant"
    assert "local" in external_ids, "should contain cli tenant"
    # Verify 5-column shape
    for row in data:
        assert "platform" in row, "row should have platform column"
        assert "external_id" in row, "row should have external_id column"
        assert "provision_status" in row, "row should have provision_status column"
        assert "registered_at" in row, "row should have registered_at column"
        assert "archived_at" in row, "row should have archived_at column"


@pytest.mark.asyncio
async def test_tenants_list_filters_by_platform(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    console = _make_console()

    async with db_session_factory() as s, s.begin():
        await make_tenant(s, platform="discord", workspace_id="filter-guild-1")
        await make_tenant(s, platform="cli", workspace_id="filter-local")

    await tenants_list(rt=rt, console=console, platform="discord", as_json=True)

    out = cast(StringIO, console.file).getvalue()
    data = json.loads(out)
    platforms = {d["platform"] for d in data}
    assert platforms == {"discord"}, "should only return discord tenants when filtered"
    cli_ids = [d["external_id"] for d in data if d["platform"] == "cli"]
    assert len(cli_ids) == 0, "cli tenants should be excluded when filtered to discord"


@pytest.mark.asyncio
async def test_tenants_list_filters_by_slack_platform(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """`--platform slack` must be accepted — slack is a real Platform value.

    The validator previously allowed only discord and cli, so every Slack
    install was unreachable from `daimon tenants list` and `tenants delete`.
    """
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    console = _make_console()

    async with db_session_factory() as s, s.begin():
        await make_tenant(s, platform="slack", workspace_id="T_FILTER_SLACK")
        await make_tenant(s, platform="discord", workspace_id="filter-guild-2")

    await tenants_list(rt=rt, console=console, platform="slack", as_json=True)

    out = cast(StringIO, console.file).getvalue()
    data = json.loads(out)
    assert {d["platform"] for d in data} == {"slack"}, (
        "should only return slack tenants when filtered to slack"
    )
    assert "T_FILTER_SLACK" in {d["external_id"] for d in data}, (
        "the seeded slack tenant must be listed"
    )


@pytest.mark.asyncio
async def test_tenants_list_rejects_unknown_platform(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """An unrecognized platform still fails loudly rather than deriving a wrong UUID."""
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    console = _make_console()

    with pytest.raises(typer.BadParameter, match="discord, cli, slack, teams"):
        await tenants_list(rt=rt, console=console, platform="matrix", as_json=True)


async def test_tenants_funding_mode_changes_only_the_selected_tenant(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    from daimon.adapters.cli.commands.tenants import tenants_funding_mode

    tenant = await make_tenant(db_session, platform="discord", workspace_id="funded")
    other = await make_tenant(db_session, platform="slack", workspace_id="other")
    await db_session.commit()
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await tenants_funding_mode(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="funded",
        mode="operator_funded",
    )
    async with db_session_factory() as session:
        row = await get_tenant(session, tenant.id)
        untouched = await get_tenant(session, other.id)
    assert row is not None and row.funding_mode == "operator_funded"
    assert untouched is not None and untouched.funding_mode == "prepaid"
    with pytest.raises(typer.BadParameter):
        await tenants_funding_mode(
            rt=rt, console=_make_console(), platform="discord", external_id="funded", mode="invalid"
        )


async def test_tenants_turn_cap_sets_and_clears_one_tenant(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="shared-guild")
    other = await make_tenant(db_session, platform="discord", workspace_id="other-guild")
    await db_session.commit()
    rt = build_cli_runtime(db_session_factory)
    console = _make_console()
    await tenants_turn_cap(
        rt=rt, console=console, platform="discord", workspace_id="shared-guild", value="30"
    )
    assert await get_turn_cap(db_session_factory, tenant_id=tenant.id, default=3) == 30
    assert await get_turn_cap(db_session_factory, tenant_id=other.id, default=3) == 3
    assert "turn cap: 30" in cast(StringIO, console.file).getvalue()
    await tenants_turn_cap(
        rt=rt, console=console, platform="discord", workspace_id="shared-guild", value="default"
    )
    assert await get_turn_cap(db_session_factory, tenant_id=tenant.id, default=3) == 3
    for bad in ("0", "-1", "1.5", "nope"):
        with pytest.raises(typer.BadParameter):
            await tenants_turn_cap(
                rt=rt,
                console=console,
                platform="discord",
                workspace_id="shared-guild",
                value=bad,
            )


# --- access-policy -------------------------------------------------------------


async def _policy(
    sessionmaker: async_sessionmaker[AsyncSession], *, workspace_id: str
) -> TenantAccessPolicy:
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=workspace_id)
    async with sessionmaker() as s:
        return await load_access_policy(s, tenant_id=tenant_id)


def _output(console: Console) -> str:
    return cast(StringIO, console.file).getvalue()


@pytest.mark.asyncio
async def test_access_policy_get_reports_a_tenant_without_a_policy_as_open(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    console = _make_console()
    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-ap1")

    await tenants_access_policy_get(
        rt=rt, console=console, platform="discord", external_id="guild-ap1", as_json=False
    )

    assert "access policy: open" in _output(console), _output(console)


@pytest.mark.asyncio
async def test_access_policy_set_warns_when_a_new_seal_meets_an_open_environment(
    db_session_factory: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    """A channel's own pick made before the seal skipped its network rule: warn, don't refuse."""
    result = await provision_tenant(db_session_factory, platform="discord", workspace_id="seal-env")
    router = MARouter()
    router.add_environment_list(
        ma_environment(id="env_open", name="open", tenant_id=result.tenant_id)
    )
    rt = build_cli_runtime(db_session_factory, router=router, settings=_FakeSettings())
    channel = "555555555555555555"
    async with db_session_factory.begin() as session:
        await scoped_config_write.set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=result.tenant_id, channel_id=channel),
            tenant_id=result.tenant_id,
            environment_name="open",
        )

    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="seal-env",
        sealed_channel=[channel],
    )

    err = " ".join(capsys.readouterr().err.split())
    assert f"Sealed {channel}." in err and "a server admin should confirm" in err, err
    assert await _policy(db_session_factory, workspace_id="seal-env") == TenantAccessPolicy(
        sealed_channel_ids=(channel,)
    ), "the warning never refuses the seal"


@pytest.mark.asyncio
async def test_access_policy_set_writes_given_fields_and_keeps_the_rest(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-ap2")

    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="guild-ap2",
        invoker=["111111111111111111", "222222222222222222", "111111111111111111"],
        protected_channel=["444444444444444444"],
    )
    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="guild-ap2",
        invoker=["333333333333333333"],
        sealed_channel=["555555555555555555"],
        dm_memory_read_only=True,
    )

    assert await _policy(db_session_factory, workspace_id="guild-ap2") == TenantAccessPolicy(
        invoker_user_ids=("333333333333333333",),
        protected_channel_ids=("444444444444444444",),
        sealed_channel_ids=("555555555555555555",),
        dm_memory_read_only=True,
    ), "a given flag replaces its field; untouched fields keep their stored value"

    console = _make_console()
    await tenants_access_policy_get(
        rt=rt, console=console, platform="discord", external_id="guild-ap2", as_json=True
    )
    assert json.loads(_output(console))["sealed_channel_ids"] == ["555555555555555555"]


@pytest.mark.asyncio
async def test_access_policy_set_clear_returns_the_tenant_to_open(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-ap3")
    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="guild-ap3",
        protected_category=["666666666666666666"],
    )

    await tenants_access_policy_set(
        rt=rt, console=_make_console(), platform="discord", external_id="guild-ap3", clear=True
    )

    assert await _policy(db_session_factory, workspace_id="guild-ap3") == OPEN_ACCESS_POLICY


@pytest.mark.asyncio
async def test_access_policy_set_refuses_to_overwrite_an_unreadable_row_until_cleared(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    result = await provision_tenant(
        db_session_factory, platform="discord", workspace_id="guild-ap4"
    )
    async with db_session_factory() as s, s.begin():
        await s.execute(
            text("INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, 'null')"),
            {"t": result.tenant_id},
        )

    with pytest.raises(AccessPolicyUnreadable):
        await tenants_access_policy_set(
            rt=rt,
            console=_make_console(),
            platform="discord",
            external_id="guild-ap4",
            invoker=["111111111111111111"],
        )

    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="guild-ap4",
        clear=True,
        replace_pins=True,
    )
    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="guild-ap4",
        invoker=["111111111111111111"],
    )
    policy = await _policy(db_session_factory, workspace_id="guild-ap4")
    assert policy.invoker_user_ids == ("111111111111111111",), (
        "after --clear the policy can be set again"
    )


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"clear": True, "invoker": ["111111111111111111"]}, {"invoker": [" ", ""]}],
    ids=["nothing-to-set", "clear-with-flags", "blank-ids"],
)
@pytest.mark.asyncio
async def test_access_policy_set_rejects_ambiguous_input_and_writes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
    kwargs: dict[str, object],
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-ap5")

    with pytest.raises(typer.BadParameter):
        await tenants_access_policy_set(
            rt=rt,
            console=_make_console(),
            platform="discord",
            external_id="guild-ap5",
            **kwargs,  # type: ignore[arg-type]
        )

    assert await _policy(db_session_factory, workspace_id="guild-ap5") == OPEN_ACCESS_POLICY


@pytest.mark.asyncio
async def test_access_policy_commands_refuse_an_unknown_tenant(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())

    with pytest.raises(StoreError, match="no tenant"):
        await tenants_access_policy_set(
            rt=rt,
            console=_make_console(),
            platform="discord",
            external_id="guild-missing",
            invoker=["111111111111111111"],
        )


def test_access_policy_set_is_registered_with_its_flags() -> None:
    command = get_command(main_mod.app)
    for name in ("tenants", "access-policy", "set"):
        assert isinstance(command, Group)
        command = command.commands[name]
    flags = {
        flag
        for param in command.params
        if isinstance(param, Option)
        for flag in (*param.opts, *param.secondary_opts)
    }
    for flag in (
        "--invoker",
        "--protected-channel",
        "--protected-category",
        "--sealed-channel",
        "--isolated-channel",
        "--dm-memory-read-only",
        "--pin-agent",
        "--clear",
    ):
        assert flag in flags, f"{flag} missing from registered options"


@pytest.mark.parametrize("as_json", [False, True])
async def test_access_policy_get_refuses_null_row(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
    as_json: bool,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    tenant = await provision_tenant(db_session_factory, platform="discord", workspace_id="null-get")
    async with db_session_factory() as session, session.begin():
        await session.execute(
            text("INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, 'null')"),
            {"t": tenant.tenant_id},
        )
    console = _make_console()
    with pytest.raises(AccessPolicyUnreadable):
        await tenants_access_policy_get(
            rt=rt, console=console, platform="discord", external_id="null-get", as_json=as_json
        )
    assert _output(console) == ""


@pytest.mark.parametrize(
    "platform,field,value",
    [
        ("discord", "invoker", "<@111111111111111111>"),
        ("discord", "invoker", "alice"),
        ("discord", "protected_channel", "#general"),
        ("discord", "protected_category", "123"),
        ("discord", "sealed_channel", "1" * 22),
        ("slack", "invoker", "u123ABC"),
        ("slack", "invoker", "C123ABC"),
        ("slack", "protected_channel", "#general"),
        ("slack", "protected_category", "category"),
        ("slack", "sealed_channel", "c123ABC"),
        ("slack", "sealed_channel", "C123ABC:"),
        ("slack", "sealed_channel", "C123ABC:yesterday"),
        ("slack", "protected_channel", "C123ABC:1700000000.000100"),
        ("discord", "isolated_channel", "#general"),
        ("slack", "isolated_channel", "D123ABC"),
        ("slack", "isolated_channel", "C123ABC:1700000000.000100"),
        ("teams", "invoker", "U123ABC"),
        ("teams", "invoker", str(uuid.UUID(int=0xABCDEF)).upper()),
        ("teams", "protected_channel", "C123ABC"),
        ("teams", "protected_category", "19:ok@thread.tacv2"),
        ("teams", "sealed_channel", "19:abc@thread.tacv2;messageid=x"),
        ("cli", "invoker", " "),
        ("discord", "invoker", ""),
    ],
)
async def test_access_policy_rejects_each_bad_id_without_writing(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
    platform: str,
    field: str,
    value: str,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    tenant_id = derive_tenant_uuid(platform=platform, workspace_id="invalid-ids")  # type: ignore[arg-type]
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform=platform, workspace_id="invalid-ids")  # type: ignore[arg-type]
    valid = (
        "111111111111111111"
        if platform == "discord"
        else (str(uuid.UUID(int=7)) if field == "invoker" else "19:ok@thread.tacv2")
        if platform == "teams"
        else "U123ABC"
        if field == "invoker"
        else "C123ABC"
    )
    with pytest.raises(typer.BadParameter) as error:
        await tenants_access_policy_set(
            rt=rt,
            console=_make_console(),
            platform=platform,
            external_id="invalid-ids",
            **{field: [valid, value]},  # type: ignore[arg-type]
        )
    assert field in str(error.value)
    assert repr(value) in str(error.value)
    async with db_session_factory() as session:
        count = await session.scalar(
            text("SELECT count(*) FROM tenant_access_policies WHERE tenant_id=:t"), {"t": tenant_id}
        )
    assert count == 0


@pytest.mark.usefixtures("db_clean")
@pytest.mark.parametrize("existing", [False, True])
async def test_concurrent_policy_edits_preserve_both_fields(
    db_nullpool_engine: AsyncEngine,
    stub_anthropic: AsyncAnthropic,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    factory = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)
    rt = build_cli_runtime(factory, anthropic=stub_anthropic, settings=_FakeSettings())
    async with factory() as session, session.begin():
        await make_tenant(session, platform="discord", workspace_id="race-policy")
        await make_tenant(session, platform="discord", workspace_id="untouched-policy")
    if existing:
        await tenants_access_policy_set(
            rt=rt,
            console=_make_console(),
            platform="discord",
            external_id="race-policy",
            invoker=["111111111111111111"],
        )
    loaded = asyncio.Event()
    release = asyncio.Event()
    second_lock_started = asyncio.Event()
    original_load = tenants_mod.load_access_policy
    original_transaction = tenants_mod.policy_write_transaction
    calls = 0

    async def held_load(session: AsyncSession, *, tenant_id: uuid.UUID) -> TenantAccessPolicy:
        nonlocal calls
        policy = await original_load(session, tenant_id=tenant_id)
        calls += 1
        if calls == 1:
            loaded.set()
            await release.wait()
        return policy

    @asynccontextmanager
    async def observed_transaction(factory, *, tenant_id):
        if loaded.is_set():
            second_lock_started.set()
        async with original_transaction(factory, tenant_id=tenant_id) as session:
            yield session

    monkeypatch.setattr(tenants_mod, "load_access_policy", held_load)
    monkeypatch.setattr(tenants_mod, "policy_write_transaction", observed_transaction)
    first = asyncio.create_task(
        tenants_access_policy_set(
            rt=rt,
            console=_make_console(),
            platform="discord",
            external_id="race-policy",
            protected_channel=["444444444444444444"],
        )
    )
    second = None
    try:
        await asyncio.wait_for(loaded.wait(), 5)
        second = asyncio.create_task(
            tenants_access_policy_set(
                rt=rt,
                console=_make_console(),
                platform="discord",
                external_id="race-policy",
                sealed_channel=["555555555555555555"],
            )
        )
        await asyncio.wait_for(second_lock_started.wait(), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.1)
        assert calls == 1, "the second connection must not read stale policy"
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(first, *([second] if second else [])), 5)
    policy = await _policy(factory, workspace_id="race-policy")
    assert policy.protected_channel_ids == ("444444444444444444",)
    assert policy.sealed_channel_ids == ("555555555555555555",)
    assert policy.invoker_user_ids == (("111111111111111111",) if existing else ())
    assert await _policy(factory, workspace_id="untouched-policy") == OPEN_ACCESS_POLICY
    await tenants_access_policy_set(
        rt=rt, console=_make_console(), platform="discord", external_id="race-policy", clear=True
    )
    assert await _policy(factory, workspace_id="untouched-policy") == OPEN_ACCESS_POLICY


@pytest.mark.parametrize(
    "platform,users,channels",
    [
        ("discord", ["1" * 15, "2" * 21], ["3" * 15, "4" * 21]),
        ("slack", ["U123ABC", "W456DEF"], ["C123ABC", "G456DEF", "D789ABC"]),
        ("cli", ["local-user"], ["local-channel"]),
        (
            "teams",
            [str(uuid.UUID(int=7))],
            [
                "19:abc123@thread.tacv2",
                "19:x_y@thread.skype",
                "19:abc123@thread.tacv2;messageid=17",
            ],
        ),
    ],
)
async def test_access_policy_accepts_platform_ids(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
    platform: str,
    users: list[str],
    channels: list[str],
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform=platform, workspace_id="valid-ids")  # type: ignore[arg-type]
    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform=platform,
        external_id="valid-ids",
        invoker=users,
        protected_channel=channels,
        protected_category=None if platform == "teams" else channels,
        sealed_channel=channels,
    )
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert policy.invoker_user_ids == tuple(users)
    assert policy.protected_channel_ids == tuple(channels)
    assert policy.protected_category_ids == (() if platform == "teams" else tuple(channels))
    assert policy.sealed_channel_ids == tuple(channels)


async def test_access_policy_isolates_a_teams_channel_with_its_own_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Teams takes a whole channel's pin and isolation, as Discord and Slack do."""
    from daimon.testing.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
    from daimon.testing.ma_models import ma_agent

    channel = "19:abc123@thread.tacv2"
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="teams", workspace_id="teams-isolate")
        await scoped_config_write.set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id=channel),
            tenant_id=tenant.id,
            agent_name="local",
            mode="agent",
        )
    state = FakeMAState()
    agent = ma_agent(id="agent_local", name="local", tenant_id=tenant.id)
    state.agents[agent.id] = agent.model_dump(mode="json")
    rt = build_cli_runtime(
        db_session_factory,
        anthropic=build_fake_anthropic(make_fake_ma_handler(state)),
        settings=_FakeSettings(),
    )
    args = {"rt": rt, "platform": "teams", "external_id": "teams-isolate"}

    with pytest.raises(typer.BadParameter, match="invalid teams id"):
        await tenants_access_policy_set(
            **args,  # type: ignore[arg-type]
            console=_make_console(),
            sealed_channel=[channel],
            isolated_channel=[f"{channel};messageid=1"],
        )
    await tenants_access_policy_set(
        **args,  # type: ignore[arg-type]
        console=_make_console(),
        sealed_channel=[channel],
        isolated_channel=[channel],
        add_pin_agent=[f"local={channel}"],
    )
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert policy.isolated_channel_ids == (channel,), "a Teams channel is isolated"
    assert policy.agent_channel_pins == {"local": (channel,)}, "and its own agent pinned to it"


async def test_access_policy_seals_a_single_slack_thread(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """The Slack channel_id:thread_ts form the read tools enforce can be set,
    shown and cleared from the CLI."""
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="slack", workspace_id="thread-seal")
    thread = "C123ABC:1700000000.000100"

    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="slack",
        external_id="thread-seal",
        sealed_channel=[thread, "C456DEF"],
    )
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert policy.sealed_channel_ids == (thread, "C456DEF")

    console = _make_console()
    await tenants_access_policy_get(
        rt=rt, console=console, platform="slack", external_id="thread-seal", as_json=True
    )
    assert json.loads(_output(console))["sealed_channel_ids"] == [thread, "C456DEF"]

    await tenants_access_policy_set(
        rt=rt, console=_make_console(), platform="slack", external_id="thread-seal", clear=True
    )
    async with db_session_factory() as session:
        assert await load_access_policy(session, tenant_id=tenant.id) == OPEN_ACCESS_POLICY


_ACME = "111111111111111111"
_ACME_2 = "222222222222222222"
_CLIENT_B = "333333333333333333"
_AGENTS = ("daimon-rx", "acme-project", "clientb-project")


async def _pin_runtime(
    db_session_factory: async_sessionmaker[AsyncSession], *, workspace_id: str
) -> CliRuntime:
    """A provisioned Discord tenant whose MA listing carries the agents pins may name."""
    await provision_tenant(db_session_factory, platform="discord", workspace_id=workspace_id)
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=workspace_id)
    router = MARouter()
    router.add_agent_list(
        *(ma_agent(id=f"ag_{name}", name=name, tenant_id=tenant_id) for name in _AGENTS)
    )
    return build_cli_runtime(db_session_factory, router=router, settings=_FakeSettings())


async def _set(rt: CliRuntime, workspace_id: str, **flags: object) -> str:
    console = _make_console()
    await tenants_access_policy_set(
        rt=rt,
        console=console,
        platform="discord",
        external_id=workspace_id,
        **flags,  # type: ignore[arg-type]
    )
    return _output(console)


@pytest.mark.asyncio
async def test_access_policy_isolates_only_a_channel_with_its_own_agent(
    db_session_factory: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    from daimon.core.scope import ChannelScopeRef
    from daimon.testing.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
    from daimon.testing.ma_models import ma_agent

    tenant = await provision_tenant(db_session_factory, platform="discord", workspace_id="iso")
    local, shared = "111111111111111111", "222222222222222222"
    async with db_session_factory() as session, session.begin():
        for channel, agent in (
            (local, "local"),
            (shared, "shared"),
            ("333333333333333333", "shared"),
        ):
            await scoped_config_write.set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant.tenant_id, channel_id=channel),
                tenant_id=tenant.tenant_id,
                agent_name=agent,
                mode="agent",
            )
    state = FakeMAState()
    for agent_id, name in (("agent_local", "local"), ("agent_shared", "shared")):
        agent = ma_agent(id=agent_id, name=name, tenant_id=tenant.tenant_id)
        state.agents[agent.id] = agent.model_dump(mode="json")
    rt = build_cli_runtime(
        db_session_factory,
        anthropic=build_fake_anthropic(make_fake_ma_handler(state)),
        settings=_FakeSettings(),
    )

    with pytest.raises(typer.BadParameter, match="must also be sealed"):
        await tenants_access_policy_set(
            rt=rt,
            console=_make_console(),
            platform="discord",
            external_id="iso",
            isolated_channel=[local],
        )
    console = _make_console()
    with pytest.raises(typer.Exit):
        await tenants_access_policy_set(
            rt=rt,
            console=console,
            platform="discord",
            external_id="iso",
            sealed_channel=[local, shared],
            isolated_channel=[local, shared],
            add_pin_agent=[f"local={local}", f"shared={shared}"],
        )
    assert "also answers outside this channel" in _output(console), _output(console)
    assert await _policy(db_session_factory, workspace_id="iso") == OPEN_ACCESS_POLICY, (
        "a refusal writes nothing"
    )
    console = _make_console()
    with pytest.raises(typer.Exit):
        await tenants_access_policy_set(
            rt=rt,
            console=console,
            platform="discord",
            external_id="iso",
            sealed_channel=[local],
            isolated_channel=[local],
        )
    assert "is not one of them" in _output(console), "its agent must be pinned to it"

    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="iso",
        sealed_channel=[local],
        isolated_channel=[local],
        add_pin_agent=[f"local={local}"],
    )
    policy = await _policy(db_session_factory, workspace_id="iso")
    assert policy.isolated_channel_ids == (local,), "its own agent answers only there"
    assert "Isolation ended" not in capsys.readouterr().err

    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="iso",
        clear=True,
        replace_pins=True,
    )
    err = " ".join(capsys.readouterr().err.split())
    assert f"Isolation ended for {local}" in err, "ending it warns"
    assert "no longer private" in err, "clearing everything leaves no seal or pin behind"


@pytest.mark.asyncio
async def test_access_policy_refuses_a_pin_edit_that_breaks_an_isolation(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Widening an isolated channel's own agent's pin would leave the channel with no
    agent of its own, so the edit is refused; an edit that leaves it whole goes through."""
    from daimon.core.scope import ChannelScopeRef
    from daimon.testing.ma import FakeMAState, build_fake_anthropic, make_fake_ma_handler
    from daimon.testing.ma_models import ma_agent

    tenant = await provision_tenant(db_session_factory, platform="discord", workspace_id="iso2")
    local, other = "111111111111111111", "222222222222222222"
    async with db_session_factory() as session, session.begin():
        for channel, agent in ((local, "local"), (other, "shared")):
            await scoped_config_write.set_fields(
                session,
                scope=ChannelScopeRef(tenant_id=tenant.tenant_id, channel_id=channel),
                tenant_id=tenant.tenant_id,
                agent_name=agent,
                mode="agent",
            )
    state = FakeMAState()
    for agent_id, name in (("agent_local", "local"), ("agent_shared", "shared")):
        agent = ma_agent(id=agent_id, name=name, tenant_id=tenant.tenant_id)
        state.agents[agent.id] = agent.model_dump(mode="json")
    rt = build_cli_runtime(
        db_session_factory,
        anthropic=build_fake_anthropic(make_fake_ma_handler(state)),
        settings=_FakeSettings(),
    )
    await _set(
        rt,
        "iso2",
        sealed_channel=[local],
        isolated_channel=[local],
        add_pin_agent=[f"local={local}"],
    )
    isolated = await _policy(db_session_factory, workspace_id="iso2")

    for flags in (
        {"add_pin_agent": [f"local={other}"]},
        {"pin_agent": [f"local={other}"], "replace_pins": True},
        {"remove_pin_agent": ["local"]},
    ):
        console = _make_console()
        with pytest.raises(typer.Exit):
            await tenants_access_policy_set(
                rt=rt, console=console, platform="discord", external_id="iso2", **flags
            )
        output = " ".join(_output(console).split())
        assert f"{local} is isolated, and this change would break it" in output, output
        assert "Nothing was changed" in output
        assert await _policy(db_session_factory, workspace_id="iso2") == isolated, (
            "a refused edit writes nothing"
        )

    await _set(rt, "iso2", add_pin_agent=[f"shared={other}"])
    policy = await _policy(db_session_factory, workspace_id="iso2")
    assert policy.agent_channel_pins == {"local": (local,), "shared": (other,)}, (
        "an edit that keeps the isolation whole goes through"
    )


def test_ended_isolation_warning_says_how_to_lift_what_is_left() -> None:
    """Ending one channel's isolation keeps its seal and pin; the warning names both."""
    policy = TenantAccessPolicy(sealed_channel_ids=("c1",), agent_channel_pins={"local": ("c1",)})
    warning = _ended_isolation_warning(policy, "c1")
    assert "stays private" in warning, warning
    assert warning.endswith("drop c1 from --sealed-channel; --remove-pin-agent local."), warning


@pytest.mark.asyncio
async def test_access_policy_set_pins_an_agent_to_channels(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = await _pin_runtime(db_session_factory, workspace_id="guild-pin")

    await _set(
        rt,
        "guild-pin",
        pin_agent=[
            "daimon-rx=666666666666666666",
            "daimon-rx=777777777777777777",
            "daimon-rx=666666666666666666",
        ],
    )

    assert await _policy(db_session_factory, workspace_id="guild-pin") == TenantAccessPolicy(
        agent_channel_pins={"daimon-rx": ("666666666666666666", "777777777777777777")}
    ), "repeating an agent adds channels; a repeated channel is kept once"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["daimon-rx", "=666666666666666666", "daimon-rx=general"])
async def test_access_policy_set_rejects_a_malformed_pin(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
    value: str,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-pin-bad")

    with pytest.raises(typer.BadParameter):
        await _set(rt, "guild-pin-bad", pin_agent=[value])


@pytest.mark.asyncio
async def test_onboarding_a_second_client_keeps_the_first_clients_pin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The onboarding step run once per client must never unpin an earlier client."""
    rt = await _pin_runtime(db_session_factory, workspace_id="guild-onboard")

    async def pins() -> dict[str, tuple[str, ...]]:
        return (await _policy(db_session_factory, workspace_id="guild-onboard")).agent_channel_pins

    await _set(rt, "guild-onboard", add_pin_agent=[f"acme-project={_ACME}"])
    printed = await _set(rt, "guild-onboard", add_pin_agent=[f"clientb-project={_CLIENT_B}"])
    assert await pins() == {"acme-project": (_ACME,), "clientb-project": (_CLIENT_B,)}
    assert f"acme-project -> {_ACME}" in printed and f"clientb-project -> {_CLIENT_B}" in printed

    with pytest.raises(typer.BadParameter, match="would unpin acme-project"):
        await _set(rt, "guild-onboard", pin_agent=[f"clientb-project={_CLIENT_B}"])
    assert (await pins())["acme-project"] == (_ACME,), "a refused replace changes nothing"

    await _set(rt, "guild-onboard", add_pin_agent=[f"acme-project={_ACME_2}"])
    await _set(rt, "guild-onboard", remove_pin_agent=[f"acme-project={_ACME}"])
    assert await pins() == {"acme-project": (_ACME_2,), "clientb-project": (_CLIENT_B,)}

    with pytest.raises(typer.BadParameter, match="last channel"):
        await _set(rt, "guild-onboard", remove_pin_agent=[f"acme-project={_ACME_2}"])
    assert (await pins())["acme-project"] == (_ACME_2,), "a last channel is never dropped by id"

    printed = await _set(rt, "guild-onboard", remove_pin_agent=["acme-project"])
    assert await pins() == {"clientb-project": (_CLIENT_B,)}, "the bare form unpins one agent"
    assert "acme-project is now UNPINNED (runs anywhere)" in printed

    printed = await _set(
        rt, "guild-onboard", pin_agent=[f"acme-project={_ACME}"], replace_pins=True
    )
    assert await pins() == {"acme-project": (_ACME,)}, "--replace-pins drops pins explicitly"
    assert "clientb-project is now UNPINNED (runs anywhere)" in printed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flags", "match"),
    [
        ({"remove_pin_agent": ["daimon-rx"]}, "not pinned"),
        ({"remove_pin_agent": [f"acme-project={_CLIENT_B}"]}, "not pinned to"),
        ({"remove_pin_agent": [f"acme-project={_ACME}"]}, "last channel"),
        (
            {"remove_pin_agent": ["acme-project", f"acme-project={_ACME}"]},
            "both bare",
        ),
        (
            {
                "add_pin_agent": [f"acme-project={_ACME_2}"],
                "remove_pin_agent": [f"acme-project={_ACME}"],
            },
            "in both --add-pin-agent and --remove-pin-agent",
        ),
        (
            {"pin_agent": [f"acme-project={_ACME}"], "add_pin_agent": [f"daimon-rx={_ACME_2}"]},
            "use it alone",
        ),
        ({"replace_pins": True, "add_pin_agent": [f"daimon-rx={_ACME_2}"]}, "only applies"),
        ({"clear": True, "add_pin_agent": [f"daimon-rx={_ACME_2}"]}, "can't be combined"),
        ({"clear": True}, "would drop every pin"),
        ({"add_pin_agent": [f"acme-projct={_ACME_2}"]}, "no agent named 'acme-projct'"),
        ({"add_pin_agent": [f"ACME-project={_ACME_2}"]}, "did you mean 'acme-project'"),
        ({"add_pin_agent": [f"\uff41cme-project={_ACME_2}"]}, "did you mean 'acme-project'"),
    ],
    ids=[
        "remove-unknown-agent",
        "remove-unknown-channel",
        "remove-last-channel",
        "mixed-remove-forms",
        "add-and-remove-same-agent",
        "replace-and-edit",
        "stray-replace",
        "clear-and-edit",
        "clear-drops-pins",
        "unknown-agent",
        "case-differs",
        "nfkc-look-alike",
    ],
)
async def test_pin_edits_refuse_ambiguous_mistyped_or_fail_open_changes(
    db_session_factory: async_sessionmaker[AsyncSession],
    flags: dict[str, object],
    match: str,
) -> None:
    rt = await _pin_runtime(db_session_factory, workspace_id="guild-pin-edit")
    await _set(rt, "guild-pin-edit", add_pin_agent=[f"acme-project={_ACME}"])

    with pytest.raises(typer.BadParameter, match=match):
        await _set(rt, "guild-pin-edit", **flags)
    assert (
        await _policy(db_session_factory, workspace_id="guild-pin-edit")
    ).agent_channel_pins == {"acme-project": (_ACME,)}, "a refused edit changes nothing"


@pytest.mark.asyncio
async def test_clear_with_replace_pins_drops_them_and_says_so(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = await _pin_runtime(db_session_factory, workspace_id="guild-pin-clear")
    await _set(rt, "guild-pin-clear", add_pin_agent=[f"acme-project={_ACME}"])

    printed = await _set(rt, "guild-pin-clear", clear=True, replace_pins=True)

    assert await _policy(db_session_factory, workspace_id="guild-pin-clear") == OPEN_ACCESS_POLICY
    assert "acme-project is now UNPINNED (runs anywhere)" in printed


@pytest.mark.asyncio
async def test_slack_pins_refuse_a_dm_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """A Slack D… id is a DM, never a channel an agent can be pinned to."""
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await provision_tenant(db_session_factory, platform="slack", workspace_id="T_PIN_DM")

    with pytest.raises(typer.BadParameter, match="invalid slack id 'D0123ABC'"):
        await tenants_access_policy_set(
            rt=rt,
            console=_make_console(),
            platform="slack",
            external_id="T_PIN_DM",
            add_pin_agent=["acme-project=D0123ABC"],
        )


@pytest.mark.asyncio
async def test_clear_on_an_unreadable_policy_still_needs_replace_pins(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An unreadable row may hold pins nobody can list; dropping them stays explicit."""
    rt = await _pin_runtime(db_session_factory, workspace_id="guild-pin-unreadable")
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id="guild-pin-unreadable")
    async with db_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, 'null'::jsonb)"
            ),
            {"t": tenant_id},
        )

    with pytest.raises(typer.BadParameter, match="can't be read"):
        await _set(rt, "guild-pin-unreadable", clear=True)
    await _set(rt, "guild-pin-unreadable", clear=True, replace_pins=True)
    assert await _policy(db_session_factory, workspace_id="guild-pin-unreadable") == (
        OPEN_ACCESS_POLICY
    )
