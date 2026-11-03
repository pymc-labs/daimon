"""The outbound gate on every platform: render, count and refuse before anything posts."""

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _outbound
from daimon.adapters.mcp.tools._outbound import (
    OutgoingPost,
    TurnScope,
    approve,
    prepare_posts,
    render_edit,
)
from daimon.core.config import OutboundSettings
from daimon.core.outbound.mentions import ping_users
from daimon.core.outbound.reach import NO_CARD, SHORT_CAPTION, TOO_LONG, ReachPlan
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

PLATFORMS = ("discord", "slack", "teams")
TAG = {"discord": "<@{}>", "slack": "<@U{}>", "teams": "<at>{}</at>"}


async def _setup(
    sessionmaker: async_sessionmaker[AsyncSession], platform: str
) -> tuple[McpRuntime, AuthIdentity]:
    async with sessionmaker() as session:
        tenant = await make_tenant(session, platform=cast(Any, platform))
        await session.commit()
    runtime = SimpleNamespace(
        session_factory=sessionmaker,
        settings=SimpleNamespace(outbound=OutboundSettings()),
        teams_client=None,
    )
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant.id, role=cast(Any, "member"), platform=platform
    )
    return cast(McpRuntime, runtime), auth


def _scope(*, card: bool = True, interactive: bool = True) -> TurnScope:
    return TurnScope(
        key=f"turn:{uuid.uuid4()}",
        interactive=interactive,
        own_places=frozenset({"home"}),
        requester_id="42",
        origin=cast(Any, object()) if card else None,
    )


def _threads(n: int) -> list[OutgoingPost]:
    return [OutgoingPost(text=f"item {i}", place="C1", new_thread=True) for i in range(n)]


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_prepare_posts_when_plan_over_budget_then_asks_before_first_post(
    sessionmaker, platform
):
    runtime, auth = await _setup(sessionmaker, platform)
    scope = _scope()
    with pytest.raises(ToolError, match="approve_reach"):
        await prepare_posts(
            runtime, auth, _threads(1), plan=ReachPlan(threads=10, places=0, people=0), scope=scope
        )
    rendered = await prepare_posts(
        runtime, auth, _threads(2), plan=ReachPlan(threads=2, places=0, people=0), scope=scope
    )
    assert len(rendered) == 2, f"{platform}: a refused call counts nothing, so two still fit"


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_prepare_posts_when_no_card_then_fails_closed(sessionmaker, platform):
    runtime, auth = await _setup(sessionmaker, platform)
    with pytest.raises(ToolError) as refused:
        await prepare_posts(
            runtime,
            auth,
            _threads(3),
            plan=ReachPlan(threads=3, places=0, people=0),
            scope=_scope(card=False),
        )
    assert str(refused.value) == NO_CARD, f"{platform}: an outside client cannot approve"


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_prepare_posts_when_approved_then_posts_until_past_plan(sessionmaker, platform):
    runtime, auth = await _setup(sessionmaker, platform)
    scope = _scope()
    plan = ReachPlan(threads=4, places=0, people=0)
    await approve(runtime, auth, scope, plan)
    assert len(await prepare_posts(runtime, auth, _threads(4), plan=plan, scope=scope)) == 4, (
        f"{platform}: the approved plan posts"
    )
    wider = ReachPlan(threads=5, places=0, people=0)
    with pytest.raises(ToolError, match="Call approve_reach"):
        await prepare_posts(runtime, auth, _threads(1), plan=wider, scope=scope)


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_prepare_posts_when_plan_undercounts_then_backstop_refuses(sessionmaker, platform):
    runtime, auth = await _setup(sessionmaker, platform)
    scope = _scope()
    plan = ReachPlan(threads=1, places=0, people=0)
    await prepare_posts(runtime, auth, _threads(1), plan=plan, scope=scope)
    with pytest.raises(ToolError, match="covers everything"):
        await prepare_posts(runtime, auth, _threads(1), plan=plan, scope=scope)


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_prepare_posts_when_too_long_or_bare_media_then_refused_if_attended(
    sessionmaker, platform
):
    runtime, auth = await _setup(sessionmaker, platform)
    plan = ReachPlan(threads=0, places=1, people=0)
    long_post = [OutgoingPost(text="x" * 1501, place="C1")]
    bare_media = [OutgoingPost(text="clip", place="C1", media=True)]
    with pytest.raises(ToolError, match=TOO_LONG):
        await prepare_posts(runtime, auth, long_post, plan=plan, scope=_scope())
    with pytest.raises(ToolError, match=SHORT_CAPTION):
        await prepare_posts(runtime, auth, bare_media, plan=plan, scope=_scope())
    unattended = _scope(interactive=False)
    assert await prepare_posts(runtime, auth, long_post, plan=plan, scope=unattended), (
        f"{platform}: nobody is there to rewrite it, so a routine post goes out"
    )


@pytest.mark.parametrize("platform", ("discord", "slack"))
async def test_prepare_posts_when_person_opted_out_then_untagged_and_uncounted(
    sessionmaker, platform, monkeypatch
):
    runtime, auth = await _setup(sessionmaker, platform)
    opted = "U2" if platform == "slack" else "2"

    async def opt_outs(*_args: object, **_kwargs: object) -> frozenset[str]:
        return frozenset({opted})

    monkeypatch.setattr(_outbound, "tag_opt_outs", opt_outs)
    text = " ".join(TAG[platform].format(n) for n in range(1, 7))
    [post] = await prepare_posts(
        runtime,
        auth,
        [OutgoingPost(text=text, place="home")],
        plan=ReachPlan(threads=0, places=0, people=5),
        scope=_scope(),
    )
    assert opted not in post.user_ids, f"{platform}: an opted-out person is never tagged"
    assert len(post.user_ids) == 5, f"{platform}: so six names stay within five people"


async def test_prepare_posts_when_discord_then_attribution_never_pings(sessionmaker):
    runtime, auth = await _setup(sessionmaker, "discord")
    [post] = await prepare_posts(
        runtime,
        auth,
        [OutgoingPost(text="<@7> please look", place="home")],
        plan=ReachPlan(threads=0, places=0, people=1),
        scope=_scope(),
    )
    assert post.text.endswith("-# requested by <@42>"), "the requester is named on the last line"
    assert ping_users.get() == frozenset({"7"}), "only the tagged person may be pinged"


async def test_render_edit_when_broadcast_then_neutralised_without_attribution(sessionmaker):
    runtime, auth = await _setup(sessionmaker, "slack")
    assert await render_edit(runtime, auth, "fixed <!here>") == "fixed @here", "edits too"
