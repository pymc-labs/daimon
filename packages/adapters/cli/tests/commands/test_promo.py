"""daimon promo create / list / revoke / redemptions."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from typing import cast

import pytest
import typer
from daimon.adapters.cli.commands import promo
from daimon.adapters.cli.commands.promo import (
    promo_create,
    promo_list,
    promo_redemptions,
    promo_revoke,
)
from daimon.adapters.cli.main import app
from daimon.core.errors import StoreError
from daimon.core.promo_credit import PromoRedeemed, redeem_promo_code
from daimon.core.stores import promo_codes as promo_store
from daimon.testing.factories import make_tenant
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed
Factory = async_sessionmaker[AsyncSession]


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, highlight=False, width=200)


def _out(console: Console) -> str:
    return cast(StringIO, console.file).getvalue()


async def test_cli_stores_every_create_flag_and_lists_it(
    db_nullpool_engine: AsyncEngine, db_clean: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``daimon promo create`` and ``list`` run end to end; each flag reaches the stored code."""
    sm = async_sessionmaker(db_nullpool_engine, expire_on_commit=False)

    @asynccontextmanager
    async def runtime(_settings: object) -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(sessionmaker=sm)

    monkeypatch.setattr(promo, "build_runtime", runtime)
    monkeypatch.setattr(promo, "load_settings", lambda: None)
    create = [
        *("promo", "create", "--amount", "7.50", "--timed", "--max-redemptions", "3"),
        *("--starts", "2026-06-01T09:00Z", "--ends", "2026-06-03T18:00Z"),
        *("--redeem-from", "2026-05-25T00:00Z", "--redeem-until", "2026-06-02T00:00Z"),
        *("--code", "launch-week-2026", "--note", "welcome"),
    ]
    # The CLI owns its own event loop; its database engine is NullPool.
    created = await asyncio.to_thread(CliRunner().invoke, app, create)
    listed = await asyncio.to_thread(CliRunner().invoke, app, ["promo", "list", "--json"])

    assert created.exit_code == 0, created.output
    assert "launch-week-2026" in created.stdout, "the chosen code should be printed once"
    [row] = json.loads(listed.stdout)
    assert (row["kind"], Decimal(row["amount_usd"]), row["max_redemptions"], row["note"]) == (
        "timed",
        Decimal("7.50"),
        3,
        "welcome",
    ), "kind, amount, limit and note should come from the flags"
    windows = [row[k][:10] for k in ("credit_starts_at", "credit_ends_at")]
    windows += [row[k][:10] for k in ("redeem_starts_at", "redeem_ends_at")]
    assert windows == ["2026-06-01", "2026-06-03", "2026-05-25", "2026-06-02"], (
        "both windows should come from the flags"
    )


async def test_created_code_is_printed_once_and_redeemable(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    """A generated code is printed at creation, redeems, and never appears again."""
    rt = build_cli_runtime(db_session_factory)
    console = _console()
    await promo_create(rt=rt, console=console, amount="15", max_redemptions=2, note="welcome")
    match = re.search(r"promo code: (\S+)\nid: (\S+)", _out(console))
    assert match is not None, "create should print the code and its id"
    code, code_id = match[1], uuid.UUID(match[2])

    tenant = await make_tenant(db_session)
    result = await redeem_promo_code(
        db_session_factory, tenant_id=tenant.id, account_id=None, code=code, now=datetime.now(UTC)
    )
    assert isinstance(result, PromoRedeemed) and result.promo_code_id == code_id, (
        "the printed code should redeem"
    )

    listed = _console()
    await promo_list(rt=rt, console=listed, as_json=True)
    [row] = json.loads(_out(listed))
    assert (row["id"], Decimal(row["amount_usd"]), row["redeemed_count"]) == (
        str(code_id),
        Decimal("15"),
        1,
    ), "list should show the code's id, amount and redemption count"
    assert code not in _out(listed) and "code_hash" not in row, "list should never show the code"

    redemptions = _console()
    await promo_redemptions(rt=rt, console=redemptions, promo_code_id=code_id, as_json=True)
    assert [r["tenant_id"] for r in json.loads(_out(redemptions))] == [str(tenant.id)], (
        "redemptions should list the redeeming tenant"
    )


async def test_custom_codes_are_unique_case_insensitively(db_session_factory: Factory) -> None:
    """A chosen code that differs only in case or dashes is a duplicate."""
    rt = build_cli_runtime(db_session_factory)
    await promo_create(rt=rt, console=_console(), amount="5", code="Launch-Week-2026")
    with pytest.raises(StoreError, match="already exists"):
        await promo_create(rt=rt, console=_console(), amount="5", code="LAUNCHWEEK2026")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"amount": "ten"}, "dollar amount"),
        ({"amount": "0"}, "positive"),
        ({"amount": "5", "timed": True, "ends": "2026-06-01"}, "both"),
        ({"amount": "5", "timed": True, "starts": "soon", "ends": "2026-06-01"}, "ISO 8601"),
        ({"amount": "5", "starts": "2026-06-01"}, "timed code"),
        ({"amount": "5", "code": "no"}, "12-64"),
        ({"amount": "5", "code": "SHORT-CODE1"}, "12-64"),
        ({"amount": "1000000"}, "at most"),
    ],
)
async def test_create_rejects_bad_input(
    db_session_factory: Factory, kwargs: dict[str, object], message: str
) -> None:
    """Bad flags are refused as a usage error before anything is stored."""
    rt = build_cli_runtime(db_session_factory)
    with pytest.raises(typer.BadParameter, match=message):
        await promo_create(rt=rt, console=_console(), **kwargs)  # type: ignore[arg-type]
    async with db_session_factory() as session:
        assert await promo_store.list_promo_codes(session) == [], f"{kwargs} should store nothing"


async def test_timed_code_stays_redeemable_until_its_credit_ends(
    db_session_factory: Factory,
) -> None:
    """Without --redeem-until a timed code is redeemable until its credit ends."""
    rt = build_cli_runtime(db_session_factory)
    await promo_create(
        rt=rt,
        console=_console(),
        amount="20",
        timed=True,
        starts="2026-06-01T09:00",
        ends="2026-06-03T18:00+02:00",
    )
    async with db_session_factory() as session:
        [row] = await promo_store.list_promo_codes(session)
    assert row.kind == "timed", "--timed should make a timed code"
    assert row.credit_starts_at == datetime(2026, 6, 1, 9, tzinfo=UTC), "naive times are UTC"
    assert row.redeem_ends_at == row.credit_ends_at == datetime(2026, 6, 3, 16, tzinfo=UTC), (
        "redemption should end with the credit, offsets converted to UTC"
    )


async def test_revoke(db_session_factory: Factory) -> None:
    """Revoke stamps the code; unknown ids are store errors."""
    rt = build_cli_runtime(db_session_factory)
    await promo_create(rt=rt, console=_console(), amount="5", code="REVOKE-ME-PLEASE")
    async with db_session_factory() as session:
        [row] = await promo_store.list_promo_codes(session)
    console = _console()
    await promo_revoke(rt=rt, console=console, promo_code_id=row.id)
    assert "no new redemptions" in _out(console), "revoke should say what it stops"
    async with db_session_factory() as session:
        revoked = await promo_store.get_promo_code(session, row.id)
    assert revoked is not None and revoked.revoked_at is not None, "the code should be revoked"
    with pytest.raises(StoreError, match="no promo code"):
        await promo_revoke(rt=rt, console=_console(), promo_code_id=uuid.uuid4())
    with pytest.raises(StoreError, match="no promo code"):
        await promo_redemptions(
            rt=rt, console=_console(), promo_code_id=uuid.uuid4(), as_json=False
        )


async def test_list_table_shows_the_redemption_window(db_session_factory: Factory) -> None:
    """Both ends of the redemption window appear in the table, not only in --json."""
    rt = build_cli_runtime(db_session_factory)
    await promo_create(
        rt=rt,
        console=_console(),
        amount="5",
        redeem_from="2026-06-01T09:00",
        redeem_until="2026-06-02T09:00",
    )
    table = _console()
    await promo_list(rt=rt, console=table, as_json=False)
    out = _out(table)
    assert "redeem_starts_at" in out, "the table should have a redemption start column"
    assert "2026-06-01" in out and "2026-06-02" in out, "both window ends should be listed"


async def test_create_a_channel_budget_code(db_session_factory: Factory) -> None:
    rt = build_cli_runtime(db_session_factory)
    await promo_create(rt=rt, console=_console(), amount="5", channel_budget=True)
    with pytest.raises(typer.BadParameter, match="not both"):
        await promo_create(
            rt=rt,
            console=_console(),
            amount="5",
            timed=True,
            channel_budget=True,
            starts="2026-06-01",
            ends="2026-06-02",
        )
    async with db_session_factory() as session:
        [row] = await promo_store.list_promo_codes(session)
    assert (row.kind, row.credit_starts_at) == ("channel_budget", None), "no credit window"
