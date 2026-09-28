from __future__ import annotations

import json
from io import StringIO
from typing import cast

import pytest
import typer
from anthropic import AsyncAnthropic
from daimon.adapters.cli import main as main_mod
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
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
        invoker=["u1", "u2", "u1"],
        protected_channel=["c-client"],
    )
    await tenants_access_policy_set(
        rt=rt,
        console=_make_console(),
        platform="discord",
        external_id="guild-ap2",
        invoker=["u3"],
        sealed_channel=["c-vault"],
        dm_memory_read_only=True,
    )

    assert await _policy(db_session_factory, workspace_id="guild-ap2") == TenantAccessPolicy(
        invoker_user_ids=("u3",),
        protected_channel_ids=("c-client",),
        sealed_channel_ids=("c-vault",),
        dm_memory_read_only=True,
    ), "a given flag replaces its field; untouched fields keep their stored value"

    console = _make_console()
    await tenants_access_policy_get(
        rt=rt, console=console, platform="discord", external_id="guild-ap2", as_json=True
    )
    assert json.loads(_output(console))["sealed_channel_ids"] == ["c-vault"]


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
        protected_category=["cat-1"],
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
            invoker=["u1"],
        )

    await tenants_access_policy_set(
        rt=rt, console=_make_console(), platform="discord", external_id="guild-ap4", clear=True
    )
    await tenants_access_policy_set(
        rt=rt, console=_make_console(), platform="discord", external_id="guild-ap4", invoker=["u1"]
    )
    policy = await _policy(db_session_factory, workspace_id="guild-ap4")
    assert policy.invoker_user_ids == ("u1",), "after --clear the policy can be set again"


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"clear": True, "invoker": ["u1"]}, {"invoker": [" ", ""]}],
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
            invoker=["u1"],
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
