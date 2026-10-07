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
    tenants_access_policy_get,
    tenants_access_policy_rules,
    tenants_access_policy_set,
    tenants_cap,
    tenants_credit,
    tenants_delete,
    tenants_list,
    tenants_turn_cap,
)
from daimon.core.access_policy import (
    OPEN_ACCESS_POLICY,
    AgentRule,
    ChannelRule,
    TenantAccessPolicy,
)
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import TenantScopeRef
from daimon.core.stores import scoped_config_write, tenant_ledger, tenant_user_caps
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    load_access_policy,
    set_access_policy,
)
from daimon.core.stores.tenants import get_tenant, get_turn_cap
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


_RULES = TenantAccessPolicy(
    channel_rules={"444444444444444444": ChannelRule(writers="none")},
    category_rules={"666666666666666666": ChannelRule(writers="none")},
)


async def _store(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_id: str, policy: TenantAccessPolicy
) -> None:
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=workspace_id)
    async with sessionmaker.begin() as session:
        await set_access_policy(session, tenant_id=tenant_id, policy=policy)


@pytest.mark.asyncio
async def test_access_policy_set_writes_given_fields_and_keeps_the_rest(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-ap2")
    await _store(db_session_factory, "guild-ap2", _RULES)

    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="guild-ap2",
        invoker=["111111111111111111", "222222222222222222", "111111111111111111"],
    )
    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="guild-ap2",
        invoker=["333333333333333333"],
        dm_memory_read_only=True,
    )

    assert await _policy(db_session_factory, workspace_id="guild-ap2") == _RULES.model_copy(
        update={"invoker_user_ids": ("333333333333333333",), "dm_memory_read_only": True}
    ), "a given flag replaces its field; untouched fields and every rule keep their value"

    console = _make_console()
    await tenants_access_policy_get(
        rt=rt, console=console, platform="discord", external_id="guild-ap2", as_json=True
    )
    assert json.loads(_output(console))["channel_rules"] == {
        "444444444444444444": {"readers": "any", "writers": "none"}
    }


@pytest.mark.asyncio
async def test_access_policy_rules_lists_channel_category_and_agent_rules(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-rules")
    args = {"rt": rt, "platform": "discord", "external_id": "guild-rules"}
    console = _make_console()
    await tenants_access_policy_rules(**args, console=console, as_json=False)  # type: ignore[arg-type]
    assert "no rules" in _output(console)
    own = ChannelRule(readers="own", writers="own")
    await _store(
        db_session_factory,
        "guild-rules",
        _RULES.model_copy(
            update={
                "channel_rules": {**_RULES.channel_rules, "555555555555555555": own},
                "agent_rules": {"acme": AgentRule(runs_in=("555555555555555555",))},
            }
        ),
    )

    console = _make_console()
    await tenants_access_policy_rules(**args, console=console, as_json=True)  # type: ignore[arg-type]
    rows = {(row["kind"], row["id"]): row for row in json.loads(_output(console))}
    assert rows[("channel", "555555555555555555")]["readers"] == "own"
    assert rows[("category", "666666666666666666")]["writers"] == "none"
    assert rows[("agent", "acme")] == {
        "kind": "agent",
        "id": "acme",
        "readers": None,
        "writers": None,
        "runs_in": ["555555555555555555"],
        "home": "555555555555555555",
    }, "an agent whose rule names a channel kept to its own agents alone has it as home"


@pytest.mark.asyncio
async def test_access_policy_set_clear_returns_the_tenant_to_open(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-ap3")
    await _store(db_session_factory, "guild-ap3", _RULES)

    await tenants_access_policy_set(
        rt=rt, console=_make_console(), platform="discord", external_id="guild-ap3", clear=True
    )

    assert await _policy(db_session_factory, workspace_id="guild-ap3") == OPEN_ACCESS_POLICY


@pytest.mark.asyncio
async def test_clear_drops_agent_rules_only_when_asked_and_says_so(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-ap6")
    ruled = TenantAccessPolicy(agent_rules={"acme": AgentRule(runs_in=("444444444444444444",))})
    await _store(db_session_factory, "guild-ap6", ruled)
    args = {"rt": rt, "platform": "discord", "external_id": "guild-ap6"}

    with pytest.raises(typer.BadParameter, match="leave acme running anywhere"):
        await tenants_access_policy_set(**args, console=_make_console(), clear=True)  # type: ignore[arg-type]
    assert await _policy(db_session_factory, workspace_id="guild-ap6") == ruled
    console = _make_console()
    await tenants_access_policy_set(
        **args,  # type: ignore[arg-type]
        console=console,
        clear=True,
        drop_agent_rules=True,
    )
    assert await _policy(db_session_factory, workspace_id="guild-ap6") == OPEN_ACCESS_POLICY
    assert "acme has no rule now (runs anywhere)" in _output(console)


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
    args = {"rt": rt, "platform": "discord", "external_id": "guild-ap4"}

    with pytest.raises(AccessPolicyUnreadable):
        await tenants_access_policy_set(
            **args,  # type: ignore[arg-type]
            console=_make_console(),
            invoker=["111111111111111111"],
        )
    with pytest.raises(typer.BadParameter, match="can't be read"):
        await tenants_access_policy_set(**args, console=_make_console(), clear=True)  # type: ignore[arg-type]

    await tenants_access_policy_set(
        **args,  # type: ignore[arg-type]
        console=_make_console(),
        clear=True,
        drop_agent_rules=True,
    )
    await tenants_access_policy_set(
        **args,  # type: ignore[arg-type]
        console=_make_console(),
        invoker=["111111111111111111"],
    )
    policy = await _policy(db_session_factory, workspace_id="guild-ap4")
    assert policy.invoker_user_ids == ("111111111111111111",), (
        "after --clear the policy can be set again"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"clear": True, "invoker": ["111111111111111111"]},
        {"invoker": [" ", ""]},
        {"drop_agent_rules": True, "invoker": ["111111111111111111"]},
    ],
    ids=["nothing-to-set", "clear-with-flags", "blank-ids", "stray-drop-agent-rules"],
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
    assert flags >= {
        "--invoker",
        "--dm-memory-read-only",
        "--add-member-guest",
        "--remove-member-guest",
        "--drop-agent-rules",
        "--clear",
    }, flags


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
    "platform,value",
    [
        ("discord", "<@111111111111111111>"),
        ("discord", "alice"),
        ("slack", "u123ABC"),
        ("slack", "C123ABC"),
        ("teams", "U123ABC"),
        ("teams", str(uuid.UUID(int=0xABCDEF)).upper()),
        ("cli", " "),
        ("discord", ""),
    ],
)
async def test_access_policy_rejects_each_bad_id_without_writing(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
    platform: str,
    value: str,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    tenant_id = derive_tenant_uuid(platform=platform, workspace_id="invalid-ids")  # type: ignore[arg-type]
    async with db_session_factory() as session, session.begin():
        await make_tenant(session, platform=platform, workspace_id="invalid-ids")  # type: ignore[arg-type]
    valid = (
        "111111111111111111"
        if platform == "discord"
        else str(uuid.UUID(int=7))
        if platform == "teams"
        else "U123ABC"
    )
    with pytest.raises(typer.BadParameter) as error:
        await tenants_access_policy_set(
            rt=rt,
            console=_make_console(),
            platform=platform,
            external_id="invalid-ids",
            invoker=[valid, value],
        )
    assert "invoker" in str(error.value)
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
        await _store(factory, "race-policy", _RULES)
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
            invoker=["444444444444444444"],
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
                dm_memory_read_only=True,
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
    assert policy.invoker_user_ids == ("444444444444444444",)
    assert policy.dm_memory_read_only
    assert policy.channel_rules == (_RULES.channel_rules if existing else {})
    assert await _policy(factory, workspace_id="untouched-policy") == OPEN_ACCESS_POLICY
    await tenants_access_policy_set(
        rt=rt, console=_make_console(), platform="discord", external_id="race-policy", clear=True
    )
    assert await _policy(factory, workspace_id="untouched-policy") == OPEN_ACCESS_POLICY


@pytest.mark.parametrize(
    "platform,users",
    [
        ("discord", ["1" * 15, "2" * 21]),
        ("slack", ["U123ABC", "W456DEF"]),
        ("cli", ["local-user"]),
        ("teams", [str(uuid.UUID(int=7))]),
    ],
)
async def test_access_policy_accepts_platform_ids(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
    platform: str,
    users: list[str],
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform=platform, workspace_id="valid-ids")  # type: ignore[arg-type]
    await tenants_access_policy_set(
        rt=rt, console=_make_console(), platform=platform, external_id="valid-ids", invoker=users
    )
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert policy.invoker_user_ids == tuple(users)


async def test_access_policy_adds_and_removes_teams_member_guests_in_place(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    async with db_session_factory() as session, session.begin():
        tenant = await make_tenant(session, platform="teams", workspace_id="guests")
    first, second = str(uuid.UUID(int=1)), str(uuid.UUID(int=2))

    async def edit(**flags: list[str]) -> TenantAccessPolicy:
        await tenants_access_policy_set(
            rt=rt, console=_make_console(), platform="teams", external_id="guests", **flags
        )
        async with db_session_factory() as session:
            return await load_access_policy(session, tenant_id=tenant.id)

    added = await edit(add_member_guest=[first.upper(), second], invoker=[first])
    assert added.member_guest_ids == (first, second), "lower-cased, in order"
    removed = await edit(remove_member_guest=[first])
    assert removed.member_guest_ids == (second,) and removed.invoker_user_ids == (first,)
    for flags in ({"remove_member_guest": [first]}, {"add_member_guest": ["not-an-id"]}):
        with pytest.raises(typer.BadParameter, match="member_guest_ids"):
            await edit(**flags)
    console = _make_console()
    await tenants_access_policy_get(
        rt=rt, console=console, platform="teams", external_id="guests", as_json=False
    )
    assert f"member_guest_ids: {second}" in _output(console), "shown with the policy"
    with pytest.raises(typer.BadParameter, match="only Teams"):
        await tenants_access_policy_set(
            rt=rt,
            console=_make_console(),
            platform="slack",
            external_id="guests",
            add_member_guest=[first],
        )
