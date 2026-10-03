"""`daimon tenants access-policy rules`: the policy as channel and agent rules."""

from __future__ import annotations

import json
from io import StringIO
from typing import cast

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands.tenants import tenants_access_policy_rules
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.access_policy import set_access_policy
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed


class _FakeCli:
    local_user = "testuser"


class _FakeSettings:
    cli = _FakeCli()


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, highlight=False, width=160)


def _output(console: Console) -> str:
    return cast(StringIO, console.file).getvalue()


async def test_rules_name_each_channel_by_preset_and_each_pin_by_where_it_runs(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """Sealed, protected, isolated and pinned read back as rules with their preset."""
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-rules")
    async with db_session_factory() as session:
        await set_access_policy(
            session,
            tenant_id=derive_tenant_uuid(platform="discord", workspace_id="guild-rules"),
            policy=TenantAccessPolicy(
                protected_channel_ids=("700", "800"),
                protected_category_ids=("900",),
                sealed_channel_ids=("600", "800"),
                isolated_channel_ids=("600",),
                agent_channel_pins={"client": ("600",), "parked": ()},
            ),
        )
        await session.commit()
    console = _console()

    await tenants_access_policy_rules(
        rt=rt, console=console, platform="discord", external_id="guild-rules", as_json=True
    )

    rows = {(row["kind"], row["id"]): row for row in json.loads(_output(console))}
    expected = {
        ("channel", "700"): ("protected", "any", "none", None, None, None),
        ("channel", "800"): ("mix", "inside", "none", None, None, None),
        ("channel", "600"): ("confidential", "own", "own", None, None, None),
        ("category", "900"): ("protected", "any", "none", None, None, None),
        ("agent", "client"): (None, None, None, ["600"], "own", "600"),
        ("agent", "parked"): (None, None, None, [], "pinned", None),
    }
    got = {
        key: (
            row["preset"],
            row["readers"],
            row["writers"],
            row["runs_in"],
            row["agent"],
            row["own_channel"],
        )
        for key, row in rows.items()
    }
    assert got == expected, f"rules differ: {got}"

    table = _console()
    await tenants_access_policy_rules(
        rt=rt, console=table, platform="discord", external_id="guild-rules", as_json=False
    )
    lines = _output(table).splitlines()
    parked = next(line for line in lines if "parked" in line)
    assert "nowhere" in parked, f"an empty pin reads as nowhere: {parked}"
    confidential = next(line for line in lines if " 600 " in line and "channel" in line)
    assert "confidential" in confidential, f"the isolated channel reads as confidential: {lines}"


async def test_rules_report_a_tenant_without_a_policy_as_open(
    db_session_factory: async_sessionmaker[AsyncSession],
    stub_anthropic: AsyncAnthropic,
) -> None:
    """No rule at all reads as every channel open."""
    rt = build_cli_runtime(db_session_factory, anthropic=stub_anthropic, settings=_FakeSettings())
    await provision_tenant(db_session_factory, platform="discord", workspace_id="guild-open")
    console = _console()

    await tenants_access_policy_rules(
        rt=rt, console=console, platform="discord", external_id="guild-open", as_json=False
    )

    assert "every channel open" in _output(console), _output(console)
