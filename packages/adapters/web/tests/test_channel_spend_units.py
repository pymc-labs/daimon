"""Channels and Credits show the same billed dollars for one channel and window."""

from __future__ import annotations

from pathlib import Path

from daimon.core.stores import tenant_ledger
from daimon.core.tenant_balance import debit_amount
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import WebClientFactory
from .test_credits_routes import Rig, _settings, _spend  # pyright: ignore[reportPrivateUsage]


async def test_channels_and_credits_agree_on_billed_spend_for_one_channel(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A $5 call at markup 2 is $10 everywhere, and the budget bar counts the same $10."""
    rig = Rig(db_session_factory, tmp_path, web_client)
    async with rig.open(settings=_settings("2")) as (client, ws, _):
        await _spend(db_session_factory, ws.tenant_id, user="u1", n=1, channel="c1")
        async with db_session_factory.begin() as db:
            await tenant_ledger.insert_entry(
                db,
                tenant_id=ws.tenant_id,
                delta_usd=-debit_amount(5.0, markup=_settings("2").billing.markup),
                reason="turn_debit",
                idempotency_key="t:1",
                channel_id="c1",
            )
        credits = (await client.get("/api/credits/spend?by=channel&period=30d")).json()
        listed = (await client.get("/api/channels")).json()["channels"]
        settings = (await client.get("/api/channels/c1/settings")).json()["spend"]

    billed = next(c["usd"] for c in credits if c["id"] == "discord:c1")
    row = next(c for c in listed if c["id"] == "c1")["spend"]
    assert billed == 10.0, "Credits bills cost times markup"
    assert (row["month_usd"], row["last_30_days_usd"]) == (billed, billed), "the directory row"
    for rows in (settings["by_day"], settings["by_agent"], settings["by_person"]):
        assert sum(r["usd"] for r in rows) == billed, "each breakdown of the channel page"
