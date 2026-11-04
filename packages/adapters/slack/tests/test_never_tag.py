"""Slack honours never-tag: no completion ping, no sweep ping, plain names in routine posts."""

from __future__ import annotations

import json
from typing import Any

from daimon.adapters.slack.output_delivery import (
    _SweepMention,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.outbound.opt_out import untag_opted_out
from daimon.core.stores import tag_opt_outs as store
from daimon.core.turn.state import TextBlock, TurnState
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_lifecycle import (  # pyright: ignore[reportPrivateUsage]
    _POST_URL,
    _make_lifecycle,
    _post_count,
)


async def _tenant_with_author(
    session: AsyncSession, *, opted_out: bool, user_id: str = "U_AUTHOR"
) -> Any:
    tenant = await make_tenant(session, platform="slack", workspace_id="T_NT_1")
    account = await make_account(session, tenant=tenant)
    await make_platform_principal(
        session, platform="slack", external_id=user_id, tenant=tenant, account=account
    )
    if opted_out:
        await store.set_opt_out(session, account_id=account.id, tenant_id=tenant.id)
    await session.commit()
    return tenant


def _posted_text(fake: Any) -> str:
    calls = fake.mock.requests.get(("POST", _POST_URL), [])
    return json.dumps([c.kwargs["json"] for c in calls])


async def test_completion_when_author_opted_out_then_answer_posts_without_a_ping(
    fake_slack_web_client: Any,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _tenant_with_author(db_session, opted_out=True)
    lc, *_ = _make_lifecycle(
        fake_slack_web_client,
        notify_on_completion=True,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
    )
    await lc.post_initial()

    await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="All done.")]))

    assert "<@U_AUTHOR>" not in _posted_text(fake_slack_web_client), "no tag in any new post"
    assert _post_count(fake_slack_web_client) == 1, "the answer edits the card, no fresh post"


async def test_completion_when_author_not_opted_out_then_answer_still_pings(
    fake_slack_web_client: Any,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await _tenant_with_author(db_session, opted_out=False)
    lc, *_ = _make_lifecycle(
        fake_slack_web_client,
        notify_on_completion=True,
        sessionmaker=db_session_factory,
        tenant_id=tenant.id,
    )
    await lc.post_initial()

    await lc.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="All done.")]))

    assert "<@U_AUTHOR>" in _posted_text(fake_slack_web_client), "everyone else still gets it"


def test_sweep_mention_when_label_given_then_it_replaces_the_tag() -> None:
    assert _SweepMention("U1").pending == "<@U1>"
    assert _SweepMention("U1", "@Ann").pending == "@Ann", "a plain name never pings"
    assert _SweepMention(None, "@Ann").pending is None, "no requester, no mention"


async def test_untag_opted_out_when_slack_text_names_them_then_plain_name(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await _tenant_with_author(db_session, opted_out=True, user_id="UAUTHOR")

    out = await untag_opted_out(
        db_session_factory,
        tenant_id=tenant.id,
        platform="slack",
        text="cc <@UAUTHOR|ann> and <@UOTHER>",
    )

    assert out == "cc @ann and <@UOTHER>", "only the person who opted out is named"
