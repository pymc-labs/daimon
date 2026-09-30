"""daimon promo create / list / revoke / redemptions."""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from typing import cast

import pytest
import typer
from click import Group, Option
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.main import get_command

from ..harness import build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed
Factory = async_sessionmaker[AsyncSession]


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, highlight=False, width=200)


def _out(console: Console) -> str:
    return cast(StringIO, console.file).getvalue()


def test_promo_commands_and_flags_are_registered() -> None:
    root = get_command(app)
    assert isinstance(root, Group)
    promo = root.commands["promo"]
    assert isinstance(promo, Group)
    assert set(promo.commands) == {"create", "list", "revoke", "redemptions"}
    flags = {
        flag
        for param in promo.commands["create"].params
        if isinstance(param, Option)
        for flag in param.opts
    }
    assert flags >= {
        "--amount",
        "--timed",
        "--starts",
        "--ends",
        "--redeem-from",
        "--redeem-until",
        "--max-redemptions",
        "--code",
        "--note",
    }


async def test_created_code_is_printed_once_and_redeemable(
    db_session: AsyncSession, db_session_factory: Factory
) -> None:
    rt = build_cli_runtime(db_session_factory)
    console = _console()
    await promo_create(rt=rt, console=console, amount="15", max_redemptions=2, note="welcome")
    match = re.search(r"promo code: (\S+)\nid: (\S+)", _out(console))
    assert match is not None
    code, code_id = match[1], uuid.UUID(match[2])

    tenant = await make_tenant(db_session)
    result = await redeem_promo_code(
        db_session_factory, tenant_id=tenant.id, account_id=None, code=code, now=datetime.now(UTC)
    )
    assert isinstance(result, PromoRedeemed) and result.promo_code_id == code_id

    listed = _console()
    await promo_list(rt=rt, console=listed, as_json=True)
    [row] = json.loads(_out(listed))
    assert (row["id"], Decimal(row["amount_usd"]), row["redeemed_count"]) == (
        str(code_id),
        Decimal("15"),
        1,
    )
    assert code not in _out(listed) and "code_hash" not in row

    redemptions = _console()
    await promo_redemptions(rt=rt, console=redemptions, promo_code_id=code_id, as_json=True)
    assert [r["tenant_id"] for r in json.loads(_out(redemptions))] == [str(tenant.id)]


async def test_custom_codes_are_unique_case_insensitively(db_session_factory: Factory) -> None:
    rt = build_cli_runtime(db_session_factory)
    await promo_create(rt=rt, console=_console(), amount="5", code="Launch-Week")
    with pytest.raises(StoreError, match="already exists"):
        await promo_create(rt=rt, console=_console(), amount="5", code="LAUNCHWEEK")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"amount": "ten"}, "dollar amount"),
        ({"amount": "0"}, "positive"),
        ({"amount": "5", "timed": True, "ends": "2026-06-01"}, "both"),
        ({"amount": "5", "timed": True, "starts": "soon", "ends": "2026-06-01"}, "ISO 8601"),
        ({"amount": "5", "starts": "2026-06-01"}, "timed code"),
        ({"amount": "5", "code": "no"}, "6-64"),
    ],
)
async def test_create_rejects_bad_input(
    db_session_factory: Factory, kwargs: dict[str, object], message: str
) -> None:
    rt = build_cli_runtime(db_session_factory)
    with pytest.raises(typer.BadParameter, match=message):
        await promo_create(rt=rt, console=_console(), **kwargs)  # type: ignore[arg-type]
    async with db_session_factory() as session:
        assert await promo_store.list_promo_codes(session) == []


async def test_timed_code_stays_redeemable_until_its_credit_ends(
    db_session_factory: Factory,
) -> None:
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
    assert row.kind == "timed"
    assert row.credit_starts_at == datetime(2026, 6, 1, 9, tzinfo=UTC)
    assert row.redeem_ends_at == row.credit_ends_at == datetime(2026, 6, 3, 16, tzinfo=UTC)


async def test_revoke(db_session_factory: Factory) -> None:
    rt = build_cli_runtime(db_session_factory)
    await promo_create(rt=rt, console=_console(), amount="5", code="REVOKE-ME")
    async with db_session_factory() as session:
        [row] = await promo_store.list_promo_codes(session)
    console = _console()
    await promo_revoke(rt=rt, console=console, promo_code_id=row.id)
    assert "no new redemptions" in _out(console)
    async with db_session_factory() as session:
        revoked = await promo_store.get_promo_code(session, row.id)
    assert revoked is not None and revoked.revoked_at is not None
    with pytest.raises(StoreError, match="no promo code"):
        await promo_revoke(rt=rt, console=_console(), promo_code_id=uuid.uuid4())
    with pytest.raises(StoreError, match="no promo code"):
        await promo_redemptions(
            rt=rt, console=_console(), promo_code_id=uuid.uuid4(), as_json=False
        )
