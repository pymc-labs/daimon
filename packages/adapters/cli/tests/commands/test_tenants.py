from __future__ import annotations

import asyncio
import json
import uuid
from io import StringIO
from typing import cast

import pytest
import typer
from anthropic import AsyncAnthropic
from daimon.adapters.cli import main as main_mod
from daimon.adapters.cli.commands import tenants as tenants_mod
from daimon.adapters.cli.commands.tenants import (
    tenants_access_policy_get,
    tenants_access_policy_set,
    tenants_delete,
    tenants_list,
)
from daimon.core.access_policy import OPEN_ACCESS_POLICY, TenantAccessPolicy
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import StoreError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.scope import TenantScopeRef
from daimon.core.stores import scoped_config_write
from daimon.core.stores.access_policy import (
    AccessPolicyUnreadable,
    load_access_policy,
)
from daimon.core.stores.tenants import get_tenant
from daimon.testing.factories import make_tenant
from rich.console import Console
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed


class _FakeCli:
    local_user = "testuser"


class _FakeSettings:
    cli = _FakeCli()


def _make_console() -> Console:
    """Console that writes to a StringIO for test output capture."""
    return Console(file=StringIO(), force_terminal=False, highlight=False, width=120)


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

    with pytest.raises(typer.BadParameter, match="discord, cli, slack"):
        await tenants_list(rt=rt, console=console, platform="teams", as_json=True)


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
        rt=rt, console=_make_console(), platform="discord", external_id="guild-ap4", clear=True
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
    result = CliRunner().invoke(main_mod.app, ["tenants", "access-policy", "set", "--help"])

    assert result.exit_code == 0, result.stdout
    for flag in (
        "--invoker",
        "--protected-channel",
        "--protected-category",
        "--sealed-channel",
        "--dm-memory-read-only",
        "--clear",
    ):
        assert flag in result.stdout, f"{flag} missing from help"


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
    original_lock = tenants_mod.lock_access_policy
    calls = 0

    async def held_load(session: AsyncSession, *, tenant_id: uuid.UUID) -> TenantAccessPolicy:
        nonlocal calls
        policy = await original_load(session, tenant_id=tenant_id)
        calls += 1
        if calls == 1:
            loaded.set()
            await release.wait()
        return policy

    async def observed_lock(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
        if loaded.is_set():
            second_lock_started.set()
        await original_lock(session, tenant_id=tenant_id)

    monkeypatch.setattr(tenants_mod, "load_access_policy", held_load)
    monkeypatch.setattr(tenants_mod, "lock_access_policy", observed_lock)
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
        protected_category=channels,
        sealed_channel=channels,
    )
    async with db_session_factory() as session:
        policy = await load_access_policy(session, tenant_id=tenant.id)
    assert policy.invoker_user_ids == tuple(users)
    assert policy.protected_channel_ids == tuple(channels)
    assert policy.protected_category_ids == tuple(channels)
    assert policy.sealed_channel_ids == tuple(channels)
