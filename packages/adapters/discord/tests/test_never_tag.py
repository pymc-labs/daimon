"""Discord honours never-tag: no completion ping, no sweep tag, plain names in routine posts."""

from __future__ import annotations

from typing import Any

import discord
from daimon.adapters.discord.routine_delivery import (
    _untagged,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.stores import tag_opt_outs as store
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_lifecycle import _make_lifecycle  # pyright: ignore[reportPrivateUsage]


async def _tenant_with_requester(session: AsyncSession, *, opted_out: bool) -> Any:
    tenant = await make_tenant(session, platform="discord", workspace_id="G_NT_1")
    account = await make_account(session, tenant=tenant)
    await make_platform_principal(
        session, platform="discord", external_id="123", tenant=tenant, account=account
    )
    if opted_out:
        await store.set_opt_out(session, account_id=account.id, tenant_id=tenant.id)
    await session.commit()
    return tenant


def _pinged(sends: list[dict[str, Any]]) -> bool:
    for kwargs in sends:
        allowed = kwargs.get("allowed_mentions")
        if "<@123>" in str(kwargs.get("content", "")) or (
            isinstance(allowed, discord.AllowedMentions) and allowed.users
        ):
            return True
    return False


async def test_completion_when_requester_opted_out_then_answer_posts_without_a_ping(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _tenant_with_requester(db_session, opted_out=True)
    lc, sends, _ = _make_lifecycle(
        notify_on_completion=True, sessionmaker=db_session_factory, tenant_id=tenant.id
    )
    await lc.post_initial()

    await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="All done.")]))

    assert not _pinged(sends), "no tag and no allowed user mention"


async def test_completion_when_requester_not_opted_out_then_answer_still_pings(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _tenant_with_requester(db_session, opted_out=False)
    lc, sends, _ = _make_lifecycle(
        notify_on_completion=True, sessionmaker=db_session_factory, tenant_id=tenant.id
    )
    await lc.post_initial()

    await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="All done.")]))

    assert _pinged(sends), "everyone else still gets it"


async def test_routine_text_when_it_tags_someone_opted_out_then_plain_name(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _tenant_with_requester(db_session, opted_out=True)
    row: Any = type("Row", (), {"tenant_id": tenant.id})()

    out = await _untagged(db_session_factory, row, "from <@123>, cc <@456>")

    assert out == "from @someone, cc <@456>", "only the person who opted out is named"


async def test_sweep_label_when_requester_opted_out_then_plain_name_else_none(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from unittest.mock import MagicMock

    from daimon.core.ma_identity import derive_tenant_uuid

    from .harness import make_bot

    guild_id = 777
    tenant = await make_tenant(db_session, platform="discord", workspace_id=str(guild_id))
    assert tenant.id == derive_tenant_uuid(platform="discord", workspace_id=str(guild_id))
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="discord", external_id="123", tenant=tenant, account=account
    )
    await store.set_opt_out(db_session, account_id=account.id, tenant_id=tenant.id)
    await db_session.commit()
    runtime = MagicMock()
    runtime.sessionmaker = db_session_factory
    bot = make_bot(runtime)
    thread = MagicMock()
    thread.guild.id = guild_id
    lifecycle = MagicMock(requester_id=123)
    other = MagicMock(requester_id=456)

    label = await bot._sweep_requester_label(thread, lifecycle)  # pyright: ignore[reportPrivateUsage]
    kept = await bot._sweep_requester_label(thread, other)  # pyright: ignore[reportPrivateUsage]

    assert label is not None and "<@" not in label, "a plain name, never a tag"
    assert kept is None, "everyone else is still named by tag"
