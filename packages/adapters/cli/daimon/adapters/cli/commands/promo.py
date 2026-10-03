"""daimon promo ... sub-app: operator-issued codes that grant tenant credit."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION
from daimon.adapters.cli.output import emit_rows
from daimon.adapters.cli.runtime import CliRuntime, build_runtime
from daimon.core.config import load_settings
from daimon.core.errors import StoreError
from daimon.core.promo_codes import (
    PromoCodeError,
    build_promo_code_terms,
    generate_promo_code,
    hash_promo_code,
    normalize_chosen_promo_code,
    parse_utc_timestamp,
)
from daimon.core.stores import promo_codes as promo_store
from rich.console import Console

promo_app = typer.Typer(help="Promo codes: create, list, revoke, redemptions.")

_ISO = "ISO 8601; UTC when no offset is given"


def _run(command: Callable[[CliRuntime, Console], Awaitable[None]]) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _with_runtime() -> None:
        async with build_runtime(settings) as rt:
            await command(rt, console)

    run_cli(_with_runtime(), console=console)


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_utc_timestamp(value)
    except PromoCodeError as exc:
        raise typer.BadParameter(str(exc)) from exc


@promo_app.command("create")
def promo_create_command(
    amount: Annotated[str, typer.Option("--amount", help="Credit per redemption, in USD.")],
    timed: Annotated[
        bool, typer.Option("--timed", help="Credit that only exists between --starts and --ends.")
    ] = False,
    starts: Annotated[str | None, typer.Option("--starts", help=f"Credit start ({_ISO}).")] = None,
    ends: Annotated[str | None, typer.Option("--ends", help=f"Credit end ({_ISO}).")] = None,
    redeem_from: Annotated[
        str | None, typer.Option("--redeem-from", help=f"First moment to redeem ({_ISO}).")
    ] = None,
    redeem_until: Annotated[
        str | None,
        typer.Option("--redeem-until", help=f"Redemption end ({_ISO}); timed default: --ends."),
    ] = None,
    max_redemptions: Annotated[
        int | None, typer.Option("--max-redemptions", help="Tenants that may redeem it.")
    ] = None,
    code: Annotated[
        str | None,
        typer.Option("--code", help="Use this code (12+ characters) instead of a generated one."),
    ] = None,
    note: Annotated[str | None, typer.Option("--note", help="Operator note.")] = None,
) -> None:
    async def _go(rt: CliRuntime, console: Console) -> None:
        await promo_create(
            rt=rt,
            console=console,
            amount=amount,
            timed=timed,
            starts=starts,
            ends=ends,
            redeem_from=redeem_from,
            redeem_until=redeem_until,
            max_redemptions=max_redemptions,
            code=code,
            note=note,
        )

    _run(_go)


async def promo_create(
    *,
    rt: CliRuntime,
    console: Console,
    amount: str,
    timed: bool = False,
    starts: str | None = None,
    ends: str | None = None,
    redeem_from: str | None = None,
    redeem_until: str | None = None,
    max_redemptions: int | None = None,
    code: str | None = None,
    note: str | None = None,
) -> None:
    try:
        amount_usd = Decimal(amount)
    except (InvalidOperation, ValueError) as exc:
        raise typer.BadParameter("amount must be a dollar amount") from exc
    try:
        terms = build_promo_code_terms(
            amount_usd=amount_usd,
            timed=timed,
            credit_starts_at=_timestamp(starts),
            credit_ends_at=_timestamp(ends),
            redeem_starts_at=_timestamp(redeem_from),
            redeem_ends_at=_timestamp(redeem_until),
            max_redemptions=max_redemptions,
            note=note,
        )
        shown = code.strip() if code is not None else generate_promo_code()
        normalized = normalize_chosen_promo_code(shown)
    except PromoCodeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    async with rt.sessionmaker() as session, session.begin():
        row = await promo_store.insert_promo_code(
            session, code_hash=hash_promo_code(normalized), terms=terms
        )
    if row is None:
        raise StoreError("a promo code with this code already exists")
    console.print(f"promo code: {shown}")
    console.print(f"id: {row.id}")
    console.print("Only a hash is stored, so this is the one time the code is shown.")


@promo_app.command("list")
def promo_list_command(as_json: Annotated[bool, JSON_OPTION] = False) -> None:
    async def _go(rt: CliRuntime, console: Console) -> None:
        await promo_list(rt=rt, console=console, as_json=as_json)

    _run(_go)


async def promo_list(*, rt: CliRuntime, console: Console, as_json: bool) -> None:
    async with rt.sessionmaker() as session:
        rows = await promo_store.list_promo_codes(session)
    emit_rows(
        console,
        rows,
        columns=(
            "id",
            "kind",
            "amount_usd",
            "redeemed_count",
            "max_redemptions",
            "credit_starts_at",
            "credit_ends_at",
            "redeem_starts_at",
            "redeem_ends_at",
            "revoked_at",
            "note",
        ),
        as_json=as_json,
    )


@promo_app.command("revoke")
def promo_revoke_command(promo_code_id: uuid.UUID) -> None:
    async def _go(rt: CliRuntime, console: Console) -> None:
        await promo_revoke(rt=rt, console=console, promo_code_id=promo_code_id)

    _run(_go)


async def promo_revoke(*, rt: CliRuntime, console: Console, promo_code_id: uuid.UUID) -> None:
    async with rt.sessionmaker() as session, session.begin():
        row = await promo_store.revoke_promo_code(
            session, promo_code_id=promo_code_id, now=datetime.now(UTC)
        )
    if row is None:
        raise StoreError(f"no promo code {promo_code_id}")
    console.print(f"revoked promo code {row.id}: no new redemptions")
    console.print("Credit already redeemed, including timed credit not yet started, stays.")


@promo_app.command("redemptions")
def promo_redemptions_command(
    promo_code_id: uuid.UUID, as_json: Annotated[bool, JSON_OPTION] = False
) -> None:
    async def _go(rt: CliRuntime, console: Console) -> None:
        await promo_redemptions(
            rt=rt, console=console, promo_code_id=promo_code_id, as_json=as_json
        )

    _run(_go)


async def promo_redemptions(
    *, rt: CliRuntime, console: Console, promo_code_id: uuid.UUID, as_json: bool
) -> None:
    async with rt.sessionmaker() as session:
        if await promo_store.get_promo_code(session, promo_code_id) is None:
            raise StoreError(f"no promo code {promo_code_id}")
        rows = await promo_store.list_redemptions(session, promo_code_id=promo_code_id)
    emit_rows(
        console,
        rows,
        columns=(
            "tenant_platform",
            "tenant_external_id",
            "redeemed_at",
            "granted_at",
            "expired_at",
            "expired_usd",
        ),
        as_json=as_json,
    )
