"""Watch category authority comes from the REST parent and the current DB rules."""

from unittest.mock import AsyncMock

import discord
import pytest
from daimon.adapters.discord import continuation_dispatch
from daimon.adapters.discord.watch_access import require_watch_thread_access
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores import watches
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.task_continuations import get_continuation
from daimon.core.turn.errors import AdmissionDenied

from .test_watch_access import PoisonCache, gateway_with_stale_owner
from .watch_fixtures import WatchDiscordRest, enrolled_page_watch


@pytest.mark.fresh_schema
@pytest.mark.parametrize(
    "mode",
    [
        "category",
        "parent",
        "thread",
        "category_moved",
        "policy_unreadable",
        "allowed",
        "notice",
        "notice_race",
    ],
)
async def test_watch_category_and_rules_are_live(db_session_factory, monkeypatch, mode):
    factory, _, tenant, _, _, watch, key = await enrolled_page_watch(
        db_session_factory, monkeypatch
    )
    async with factory() as db:
        row = await get_continuation(db, idempotency_key=key)
    client = gateway_with_stale_owner()
    rest = WatchDiscordRest(client)
    rest.category_id = "900"
    # The cached parent/category (and every other gateway authority) is unusable.
    # A successful REST-only check must still return its fresh category to admission.
    client._connection = PoisonCache()
    access = await require_watch_thread_access(factory, row, client)
    assert access.category_id == "900"

    policy = TenantAccessPolicy(category_rules={"900": {"writers": "none"}})
    if mode == "parent":
        policy = TenantAccessPolicy(channel_rules={"4241": {"writers": "none"}})
    elif mode == "thread":
        policy = TenantAccessPolicy(channel_rules={"4242": {"writers": "none"}})
    elif mode == "category_moved":
        # The previously authorized category stays open; REST now places the
        # parent in another category that the DB protects.
        rest.category_id = "901"
        policy = TenantAccessPolicy(category_rules={"901": {"writers": "none"}})
    elif mode in {"allowed", "notice_race"}:
        policy = TenantAccessPolicy(category_rules={"901": {"writers": "none"}})
    async with factory.begin() as db:
        await set_access_policy(db, tenant_id=tenant.id, policy=policy)
    if mode == "policy_unreadable":
        from daimon.core.turn import protection

        monkeypatch.setattr(
            protection, "load_access_policy", AsyncMock(side_effect=ValueError("unreadable"))
        )
    if mode == "allowed":
        access = await require_watch_thread_access(factory, row, client)
        assert access.category_id == "900"
        return
    if mode in {"notice", "notice_race"}:
        if mode == "notice_race":
            from daimon.core.agent_identity import AgentIdentity

            async def move_during_identity(*args, **kwargs):
                rest.category_id = "901"
                return AgentIdentity(name="target", avatar_url=None, builtin=True)

            monkeypatch.setattr(
                continuation_dispatch, "resolve_agent_identity", move_during_identity
            )

        # Notice delivery must not consult the old gateway-backed may_post hook.
        async def dispatch(*args, **kwargs):
            await kwargs["notice"](row, "Must not post", "skip_target_changed")

        monkeypatch.setattr(continuation_dispatch, "dispatch_core", dispatch)
        thread = AsyncMock(spec=discord.Thread)
        thread.id = 4242
        may_post = AsyncMock(side_effect=AssertionError("cached category consulted"))
        await continuation_dispatch.dispatch_pending_continuations(
            factory,
            AsyncMock(),
            tenant_id=tenant.id,
            thread=thread,
            run_follow_up=AsyncMock(),
            may_post=may_post,
            client=client,
        )
        thread.send.assert_not_awaited()
        may_post.assert_not_awaited()
    else:
        with pytest.raises(AdmissionDenied):
            await require_watch_thread_access(factory, row, client)
    async with factory() as db:
        paused = await watches.get_watch(db, watch.id)
    assert paused.status == "paused"
    assert "channel/category rules" in paused.source_state["reason"]
