"""The outbound gate on every platform: render, count and refuse before anything posts."""

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta
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
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.outbound.mentions import ping_users
from daimon.core.outbound.reach import (
    NO_CARD,
    NO_PLAN,
    SHORT_CAPTION,
    TOO_LONG,
    ReachPlan,
    ReachTally,
    card_title,
)
from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.core.stores.platform_names import KnownName, upsert_user_names
from daimon.core.stores.reach_tallies import lock_tally, save_tally
from daimon.core.stores.tag_opt_outs import set_opt_out
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.turn_origins import bind_origin_session, create_origin
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
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
        kind=("live" if card else "outside") if interactive else "unattended",
        own_places=frozenset({"home"}),
        requester_id="42",
        origin=cast(Any, object()) if card else None,
        own_channels=frozenset({"home", "own"}),
    )


async def _tally(runtime: McpRuntime, auth: AuthIdentity, scope: TurnScope) -> ReachTally:
    async with runtime.session_factory() as session, session.begin():
        tally, _ = await lock_tally(session, tenant_id=auth.tenant_id, turn_key=scope.key)
    return tally


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
async def test_prepare_posts_when_plan_undercounts_then_actual_counts(sessionmaker, platform):
    """A plan below actual is never refused on its own: the gate takes the larger."""
    runtime, auth = await _setup(sessionmaker, platform)
    scope = _scope()
    plan = ReachPlan(threads=0, places=0, people=0)
    await prepare_posts(runtime, auth, _threads(1), plan=plan, scope=scope)
    assert await prepare_posts(runtime, auth, _threads(1), plan=plan, scope=scope), (
        f"{platform}: two threads are within the limit whatever the plan said"
    )
    with pytest.raises(ToolError, match="approve_reach"):
        await prepare_posts(runtime, auth, _threads(1), plan=plan, scope=scope)


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize("plan", [None, ReachPlan(threads=0, places=0, people=0)], ids=str)
async def test_prepare_posts_when_reach_call_has_no_plan_then_one_line_ask(
    sessionmaker, platform, plan
):
    """A zero plan is counted as actual; only a missing one is refused, and only for reach."""
    runtime, auth = await _setup(sessionmaker, platform)
    post = [OutgoingPost(text="hello", place="C1")]
    if plan is not None:
        assert await prepare_posts(runtime, auth, post, plan=plan, scope=_scope()), platform
        return
    with pytest.raises(ToolError) as refused:
        await prepare_posts(runtime, auth, post, plan=plan, scope=_scope())
    assert str(refused.value) == NO_PLAN, f"{platform}: one line asking for the totals"
    assert "\n" not in NO_PLAN, "the ask is a single line"


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


@pytest.mark.parametrize("platform", PLATFORMS)
@pytest.mark.parametrize(
    ("place", "card"),
    [("home", True), ("42", True), ("42", False)],
    ids=["own_thread", "dm_to_requester", "outside_client_own_dm"],
)
async def test_prepare_posts_when_long_or_bare_media_in_own_conversation_then_posts(
    sessionmaker, platform, place, card
):
    """By tool or by DM to oneself, and from an outside client to its own DM."""
    runtime, auth = await _setup(sessionmaker, platform)
    scope = _scope(card=card)
    for post in (
        OutgoingPost(text="x" * 5000, place=place),
        OutgoingPost(text="clip", place=place, media=True),
    ):
        [out] = await prepare_posts(runtime, auth, [post], plan=None, scope=scope)
        assert out.text == post.text, f"{platform}: no line in the requester's own conversation"
    assert await _tally(runtime, auth, scope) == ReachTally(), f"{platform}: nothing counted"


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_prepare_posts_when_untagged_threads_in_own_channel_then_no_card(
    sessionmaker, platform
):
    """Threads for oneself in the own channel are personal use, not reach."""
    runtime, auth = await _setup(sessionmaker, platform)
    scope = _scope()
    posts = [OutgoingPost(text=f"note {i}", place="own", new_thread=True) for i in range(3)]
    rendered = await prepare_posts(runtime, auth, posts, plan=None, scope=scope)
    assert [r.text for r in rendered] == [p.text for p in posts], f"{platform}: no line"
    assert await _tally(runtime, auth, scope) == ReachTally(), f"{platform}: no thread count"


@pytest.mark.parametrize("platform", ("discord", "slack"))
async def test_prepare_posts_when_tagged_threads_in_own_channel_then_card_before_first_post(
    sessionmaker, platform
):
    """A thread that tags someone else reaches them, even in the own channel. Teams posts
    tag nobody, so there a thread in the own channel never reaches anyone."""
    runtime, auth = await _setup(sessionmaker, platform)
    posts = [
        OutgoingPost(text=f"{TAG[platform].format(7)} item {i}", place="own", new_thread=True)
        for i in range(10)
    ]
    with pytest.raises(ToolError, match="approve_reach"):
        await prepare_posts(
            runtime, auth, posts, plan=ReachPlan(threads=10, places=0, people=1), scope=_scope()
        )


@pytest.mark.parametrize("platform", ("discord", "slack"))
@pytest.mark.parametrize("requester", ["42", "43"], ids=["requester", "second_collaborator"])
async def test_prepare_posts_when_collaborators_tagged_in_own_thread_then_no_card(
    sessionmaker, platform, requester
):
    """Crediting or looping in six people here is collaboration, whoever in the thread asked."""
    runtime, auth = await _setup(sessionmaker, platform)
    scope = dataclasses.replace(_scope(), requester_id=requester)
    text = " ".join(TAG[platform].format(n) for n in range(1, 7)) + " thanks"
    [post] = await prepare_posts(
        runtime, auth, [OutgoingPost(text=text, place="home")], plan=None, scope=scope
    )
    assert len(post.user_ids) == 6, f"{platform}: all six are tagged"
    assert "requested by" not in post.text, f"{platform}: no line in the own thread"
    assert await _tally(runtime, auth, scope) == ReachTally(), f"{platform}: nobody counted"


@pytest.mark.parametrize("platform", ("discord", "slack"))
async def test_prepare_posts_when_person_opted_out_then_untagged_and_uncounted(
    sessionmaker, platform
):
    """The stored choice reaches the gate through the person's principal, not a stub."""
    runtime, auth = await _setup(sessionmaker, platform)
    opted = "U2" if platform == "slack" else "2"
    async with sessionmaker.begin() as session:
        tenant = await get_tenant(session, auth.tenant_id)
        assert tenant is not None, "the tenant _setup made"
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform=platform, external_id=opted, tenant=tenant, account=account
        )
        await set_opt_out(session, account_id=account.id, tenant_id=auth.tenant_id)
    text = " ".join(TAG[platform].format(n) for n in range(1, 7))
    [post] = await prepare_posts(
        runtime,
        auth,
        [OutgoingPost(text=text, place="C1")],
        plan=ReachPlan(threads=0, places=1, people=5),
        scope=_scope(),
    )
    assert opted not in post.user_ids, f"{platform}: an opted-out person is never tagged"
    assert len(post.user_ids) == 5, f"{platform}: so six names stay within five people"


async def test_prepare_posts_when_discord_then_attribution_never_pings(sessionmaker):
    runtime, auth = await _setup(sessionmaker, "discord")
    [post] = await prepare_posts(
        runtime,
        auth,
        [OutgoingPost(text="<@7> please look", place="C1")],
        plan=ReachPlan(threads=0, places=1, people=1),
        scope=_scope(),
    )
    assert post.text.endswith("-# requested by <@42>"), "the requester is named on the last line"
    assert ping_users.get() == frozenset({"7"}), "only the tagged person may be pinged"


@pytest.mark.parametrize("platform", ["web", "agent"])
async def test_prepare_posts_when_web_or_agent_chat_then_plain_attribution(sessionmaker, platform):
    """A chat outside Discord has no Discord id to tag, so the line names the asker in text."""
    runtime, auth = await _setup(sessionmaker, "discord")
    auth = dataclasses.replace(auth, platform=platform)
    [post] = await prepare_posts(
        runtime,
        auth,
        [OutgoingPost(text="hello", place="C1")],
        plan=ReachPlan(threads=0, places=1, people=0),
        scope=_scope(),
    )
    assert post.text == "hello\n-# requested by someone", f"{platform}: never <@id>"


@pytest.mark.parametrize("platform", ("discord", "slack"))
async def test_prepare_posts_when_opt_out_lookup_fails_then_reach_post_sends_uncounted(
    sessionmaker, platform, monkeypatch
):
    """A broken lookup never loses a post elsewhere; it just pings and counts nobody."""

    async def broken(*_: object, **__: object) -> frozenset[str]:
        raise RuntimeError("lookup down")

    runtime, auth = await _setup(sessionmaker, platform)
    monkeypatch.setattr(_outbound, "tag_opt_outs", broken)
    scope = _scope()
    text = f"{TAG[platform].format(7)} look"
    [post] = await prepare_posts(
        runtime,
        auth,
        [OutgoingPost(text=text, place="C1")],
        plan=ReachPlan(threads=0, places=1, people=1),
        scope=scope,
    )
    assert post.text.startswith(text if platform == "discord" else "@someone look"), platform
    assert ping_users.get() == frozenset(), f"{platform}: nobody unchecked is pinged"
    assert (await _tally(runtime, auth, scope)).people == frozenset(), "nobody counted"
    with pytest.raises(ToolError, match="could not check"):
        await _outbound.refuse_dm_to_opted_out(runtime, auth, "7")


async def test_render_edit_when_broadcast_then_neutralised_without_attribution(sessionmaker):
    runtime, auth = await _setup(sessionmaker, "slack")
    assert await render_edit(runtime, auth, "fixed <!here>", place="C1") == "fixed @here", (
        "edits too"
    )


async def test_render_edit_when_too_long_elsewhere_then_refused_but_own_place_allowed(
    sessionmaker, monkeypatch
):
    """An edit can grow a short post into a wall, so the length rule meets it too."""
    runtime, auth = await _setup(sessionmaker, "slack")

    async def scope_of(*_args: object, **_kwargs: object) -> TurnScope:
        return _scope()

    monkeypatch.setattr(_outbound, "turn_scope", scope_of)
    long = "x" * 2000
    with pytest.raises(ToolError, match="three-line summary"):
        await render_edit(runtime, auth, long, place="C_OTHER")
    assert await render_edit(runtime, auth, long, place="home") == long, (
        "the requester's own conversation takes long edits"
    )


async def test_approve_when_card_built_from_same_args_then_card_equals_stored_plan(sessionmaker):
    runtime, auth = await _setup(sessionmaker, "discord")
    scope = _scope()
    raw = {"threads": "4", "places": 3.0, "people": 6, "where": "#x"}
    call = ToolCall(
        tool_use_id="reach", server_name=DAIMON_SERVER_NAME, tool_name="approve_reach", input=raw
    )
    card = prompt_for_tool_call(call, requester_platform_user_id="42", now=datetime.now(UTC))
    await approve(runtime, auth, scope, ReachPlan.from_args(raw))
    async with sessionmaker() as session, session.begin():
        _, stored = await lock_tally(session, tenant_id=auth.tenant_id, turn_key=scope.key)
    assert stored == ReachPlan(threads=4, places=3, people=6), "approve stores the validated plan"
    assert card.title == card_title(stored, "#x"), "the card shows exactly what was stored"


async def _session_turn(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    attended: bool = True,
    turns: int = 1,
    session_id: str | None = "chat-1",
    platform: str = "discord",
) -> tuple[McpRuntime, AuthIdentity, list[TurnOriginRow]]:
    """A caller in a chat session with `turns` running origins of its own agent."""
    now = datetime.now(UTC)
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform=cast(Any, platform))
        account = await make_account(session, tenant=tenant)
        origins = [
            await create_origin(
                session,
                tenant_id=tenant.id,
                account_id=account.id,
                platform=cast(Any, platform),
                parent_channel_id="C1",
                thread_id=f"T{i}",
                responder_ma_agent_id="agent_x",
                responder_name="daimon",
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                role=Role.USER,
                sender_foreign=False,
                shared_channel=False,
                expires_at=now + timedelta(minutes=10),
                now=now + timedelta(seconds=i),
                attended=attended,
                chat_session_id=session_id,
            )
            for i in range(turns)
        ]
    runtime = SimpleNamespace(
        session_factory=sessionmaker,
        settings=SimpleNamespace(outbound=OutboundSettings()),
        teams_client=None,
    )
    auth = AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=cast(Any, "member"),
        platform=platform,
        chat_agent_id=derive_agent_uuid(tenant_id=tenant.id, ma_agent_id="agent_x"),
        chat_session_id="chat-1",
        token_jti="jti-1",
    )
    return cast(McpRuntime, runtime), auth, origins


async def test_turn_scope_when_attended_origin_then_live_with_card(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime, auth, [origin] = await _session_turn(sessionmaker)

    scope = await _outbound.turn_scope(runtime, auth)

    assert scope.kind == "live", "a person's running turn can show a card"
    assert scope.origin is not None and scope.origin.id == origin.id, "the turn's own origin"
    assert scope.key == f"turn:{origin.id}", "the tally is the turn's"


@pytest.mark.parametrize("place", ["T0", "C1:T0"], ids=["thread_ts", "channel_thread_ts"])
async def test_prepare_posts_when_slack_own_thread_then_no_line_or_limits(
    sessionmaker: async_sessionmaker[AsyncSession], place: str
) -> None:
    """Slack tools name a thread ``channel:thread_ts``; that is still the own thread."""
    runtime, auth, _ = await _session_turn(sessionmaker, platform="slack")
    scope = await _outbound.turn_scope(runtime, auth)
    doc = OutgoingPost(text="x" * 5000, place=place)
    video = OutgoingPost(text="clip", place=place, media=True)
    rendered = await prepare_posts(runtime, auth, [doc, video], plan=None, scope=scope)
    assert [r.text for r in rendered] == [doc.text, video.text], "no line, no refusal"
    assert await _tally(runtime, auth, scope) == ReachTally(), "and no count"


async def test_turn_scope_when_server_started_origin_then_unattended(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime, auth, _ = await _session_turn(sessionmaker, attended=False)

    scope = await _outbound.turn_scope(runtime, auth)

    assert scope.kind == "unattended", "a watch, timer or relay origin is the server's own turn"
    assert not scope.interactive, "nobody is there to answer"


async def test_turn_scope_when_routine_then_unattended(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime, auth, _ = await _session_turn(sessionmaker, turns=0)
    auth = dataclasses.replace(auth, routine_id=uuid.uuid4(), execution_id=uuid.uuid4())

    scope = await _outbound.turn_scope(runtime, auth)

    assert scope.kind == "unattended", "a routine run is the server's own turn"
    assert scope.key == f"routine:{auth.execution_id}", "one tally per run"


@pytest.mark.parametrize("session_id", ["chat-1", None], ids=["no-origin", "no-session"])
async def test_turn_scope_when_no_running_origin_then_outside_client(
    sessionmaker: async_sessionmaker[AsyncSession], session_id: str | None
) -> None:
    runtime, auth, _ = await _session_turn(sessionmaker, turns=0)
    auth = dataclasses.replace(auth, chat_session_id=session_id)

    scope = await _outbound.turn_scope(runtime, auth)

    assert scope.kind == "outside", "nothing proves the server started it, so it is a client"
    assert scope.interactive, "outside clients get the length and caption rules"
    assert scope.origin is None, "and no card, so over budget fails closed"
    assert scope.key.startswith("client:jti-1:"), "tallied per credential"


async def test_turn_scope_when_two_turns_share_a_session_then_neither_borrows_the_other(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime, auth, _ = await _session_turn(sessionmaker, turns=2)

    scope = await _outbound.turn_scope(runtime, auth)

    assert scope.kind == "outside", "an ambiguous turn gets no card"
    assert not scope.key.startswith("turn:"), "and no other turn's tally or approval"
    doc = OutgoingPost(text="x" * 5000, place="T1")
    [out] = await prepare_posts(runtime, auth, [doc], plan=None, scope=scope)
    assert out.text == doc.text, "every running turn's thread is still the caller's own"
    with pytest.raises(ToolError) as refused:
        await prepare_posts(
            runtime,
            auth,
            [OutgoingPost(text=f"t{i}", place="C9", new_thread=True) for i in range(3)],
            plan=ReachPlan(threads=3, places=0, people=0),
            scope=scope,
        )
    assert str(refused.value) == NO_CARD, "a truly outside place fails closed"


async def test_turn_scope_when_grant_names_its_origin_then_that_turn_only(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    runtime, auth, origins = await _session_turn(sessionmaker, turns=2, session_id=None)
    async with sessionmaker.begin() as session:
        for origin in origins:
            await bind_origin_session(session, origin_id=origin.id, chat_session_id=str(origin.id))

    scopes = [
        await _outbound.turn_scope(
            runtime, dataclasses.replace(auth, chat_session_id=str(origin.id))
        )
        for origin in origins
    ]

    assert [s.key for s in scopes] == [f"turn:{o.id}" for o in origins], (
        "each concurrent turn keeps its own tally"
    )


@pytest.mark.parametrize(("stored", "expected"), [("Ann", "Ann"), (None, "someone")])
async def test_prepare_posts_when_live_name_unknown_then_stored_name_else_someone(
    sessionmaker, stored, expected
):
    """Who asked is always said: the live name, else the stored one, else "someone"."""
    runtime, auth = await _setup(sessionmaker, "slack")
    if stored:
        async with sessionmaker.begin() as session:
            tenant = await get_tenant(session, auth.tenant_id)
            assert tenant is not None, "the tenant _setup made"
            await make_platform_principal(
                session, platform="slack", external_id="42", tenant=tenant
            )
            await upsert_user_names(
                session,
                tenant_id=auth.tenant_id,
                platform="slack",
                names={"42": KnownName(display_name=stored)},
            )
    [post] = await prepare_posts(
        runtime,
        auth,
        [OutgoingPost(text="hello", place="C1")],
        plan=ReachPlan(threads=0, places=1, people=0),
        scope=_scope(),
    )
    assert post.text == f"hello\nrequested by {expected}", "the attribution line is never dropped"


async def test_prepare_posts_when_another_call_holds_the_tally_then_it_waits_and_sees_its_posts(
    committing_sessionmaker,
):
    """Two MCP instances serve one turn: a call must wait for the other's tally, not read it stale."""
    runtime, auth = await _setup(committing_sessionmaker, "discord")
    scope = _scope()
    plan = ReachPlan(threads=1, places=0, people=0)
    async with committing_sessionmaker() as session, session.begin():
        await lock_tally(session, tenant_id=auth.tenant_id, turn_key=scope.key)

    async with committing_sessionmaker() as session, session.begin():
        await lock_tally(session, tenant_id=auth.tenant_id, turn_key=scope.key)
        racer = asyncio.ensure_future(
            prepare_posts(runtime, auth, _threads(1), plan=plan, scope=scope)
        )
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(racer), 1.0)
        await save_tally(
            session,
            tenant_id=auth.tenant_id,
            turn_key=scope.key,
            tally=ReachTally(threads=2),
        )

    with pytest.raises(ToolError, match="approve_reach"):
        await racer
