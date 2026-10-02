"""Review probes: real stores/authz; fake MA and platform transports only."""

import asyncio
import contextlib
import dataclasses

import pytest
from aioresponses import CallbackResult, aioresponses
from daimon.adapters.mcp.tools._channel_policy import SealedChannelError, load_read_policy
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, AgentRef, Place, Subject, authorize
from daimon.core.channel_tidy import content_hash
from daimon.core.stores.access_policy import (
    load_access_policy,
    lock_access_policy,
    set_access_policy,
)
from daimon.core.stores.agent_posts import get_post
from daimon.testing import ma_agent
from daimon.testing.db import build_test_engine
from daimon.testing.ma import build_fake_anthropic, list_response
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import test_channel_tidy_discord as d
from . import test_channel_tidy_slack as s


@pytest.fixture
def fake(monkeypatch):
    state = d._FakeDiscord()
    d.patch_discord_http(monkeypatch, state.handle)
    return state


async def write_policy(world, policy):
    # Use the real policy writer's lock on a separate pooled connection.
    async with world.sessionmaker.begin() as session:
        await lock_access_policy(session, tenant_id=world.tenant_id)
        await set_access_policy(session, tenant_id=world.tenant_id, policy=policy)


def agent_payload(world, agent):
    return ma_agent(
        id=agent, name=agent, metadata={"daimon_tenant": str(world.tenant_id), "daimon_name": agent}
    ).model_dump(mode="json")


async def late_ma_hook(world, agent, hook):
    fired = []
    armed = [False]
    # Arm after the target is fetched/read, during the I/O preceding the final check.
    lookups = 0

    async def dispatch(request):
        nonlocal lookups
        lookups += 1
        assert request.url.path == "/v1/agents"
        # The audit is committed before run_action's final policy check.
        if not fired and armed[0] and lookups == 2:
            fired.append(True)
            await hook()
        return list_response([agent_payload(world, agent)])

    world.runtime = dataclasses.replace(world.runtime, client=build_fake_anthropic(dispatch))
    return fired, armed


@pytest.mark.parametrize(
    "platform,action",
    [("discord", x) for x in ("edit", "delete", "archive", "delete_thread")]
    + [("slack", x) for x in ("edit", "delete", "delete_thread")],
)
@pytest.mark.parametrize("change", ["protect", "pin", "seal", "isolation"])
async def test_final_agent_lookup_cannot_use_stale_policy(
    committing_sessionmaker, fake, platform, action, change
):
    h = d if platform == "discord" else s
    world = await h._world(committing_sessionmaker)
    if platform == "discord" and change == "seal":
        auth, origin = await world.turn(parent="ELSEWHERE", thread="ELSEWHERE")
    else:
        auth, origin = await world.turn(parent=h._CHANNEL if change != "seal" else "ELSEWHERE")
    channel = h._CHANNEL
    await write_policy(world, TenantAccessPolicy(agent_channel_pins={h._AGENT: (channel,)}))
    with aioresponses() as m:
        if platform == "discord":
            if action in ("archive", "delete_thread"):
                await d._create_thread_impl(
                    world.runtime, auth, channel_id=channel, name="tidy", content="root"
                )
            mid = await d._post(world, auth)
        else:
            s._channel_access(m)
            mid = await s._post(world, auth, m, "1700000005.000100")
            m.get(s._CONVERSATIONS_REPLIES, payload={"ok": True, "messages": [{"ts": mid}]})
            m.post(s._CHAT_UPDATE, payload={"ok": True, "ts": mid})
            m.post(s._CHAT_DELETE, payload={"ok": True}, repeat=True)
        policies = {
            "protect": TenantAccessPolicy(protected_channel_ids=(channel,)),
            "pin": TenantAccessPolicy(agent_channel_pins={h._AGENT: ("ELSEWHERE",)}),
            "seal": TenantAccessPolicy(sealed_channel_ids=(channel,)),
            "isolation": TenantAccessPolicy(
                sealed_channel_ids=(channel,), isolated_channel_ids=(channel,)
            ),
        }

        async def hook():
            await write_policy(world, policies[change])
            async with world.sessionmaker() as session:
                fresh = await load_access_policy(session, tenant_id=world.tenant_id)
            if change == "seal":
                read = await load_read_policy(world.runtime, auth, origin_context_id=origin)
                with pytest.raises(SealedChannelError):
                    read.require(channel)
            else:
                decision = authorize(
                    fresh,
                    subject=Subject(),
                    action=Action.POST,
                    agent=AgentRef.of(h._AGENT),
                    place=Place(channel_id=channel),
                )
                assert not decision
                print("fresh refusal", platform, action, change, decision.reason)

        fired, armed = await late_ma_hook(world, h._AGENT, hook)
        armed[0] = True
        refused = False
        try:
            if platform == "discord":
                if action == "edit":
                    await d._edit_message_impl(
                        world.runtime,
                        auth,
                        channel_id=channel,
                        message_id=mid,
                        content="late",
                        origin_context_id=origin,
                    )
                elif action == "delete":
                    await d._delete_message_impl(
                        world.runtime,
                        auth,
                        channel_id=channel,
                        message_id=mid,
                        origin_context_id=origin,
                    )
                elif action == "archive":
                    await d._archive_thread_impl(
                        world.runtime, auth, thread_id=d._THREAD, origin_context_id=origin
                    )
                else:
                    await d._delete_thread_impl(
                        world.runtime, auth, thread_id=d._THREAD, origin_context_id=origin
                    )
            elif action == "edit":
                await s._slack_edit_message_impl(
                    world.runtime,
                    auth,
                    channel_id=channel,
                    message_id=mid,
                    content="late",
                    origin_context_id=origin,
                )
            elif action == "delete":
                await s._slack_delete_message_impl(
                    world.runtime,
                    auth,
                    channel_id=channel,
                    message_id=mid,
                    origin_context_id=origin,
                )
            else:
                await s._slack_delete_thread_impl(
                    world.runtime, auth, thread_id=f"{channel}:{mid}", origin_context_id=origin
                )
        except ToolError:
            refused = True
        assert fired, "probe reached the final MA network lookup"
        print("stale action outcome", platform, action, change, "refused=", refused)
        assert refused, "effect used policy loaded before a committed policy-writer edit"


async def test_discord_human_reply_after_latest_history_survives(committing_sessionmaker, fake):
    world = await d._world(committing_sessionmaker)
    auth, origin = await world.turn()
    await d._create_thread_impl(
        world.runtime, auth, channel_id=d._CHANNEL, name="tidy", content="root"
    )
    await write_policy(world, TenantAccessPolicy(agent_channel_pins={d._AGENT: (d._CHANNEL,)}))
    humans = []

    async def hook():
        humans.append(fake.add(d._THREAD, author_id=d._CALLER, bot=False, content="keep my reply"))

    fired, armed = await late_ma_hook(world, d._AGENT, hook)
    armed[0] = True
    with contextlib.suppress(ToolError):
        await d._delete_thread_impl(
            world.runtime, auth, thread_id=d._THREAD, origin_context_id=origin
        )
    assert fired and humans
    assert all(mid in fake.messages for mid in humans)
    print(
        "human reply present at whole-thread DELETE", humans, "thread_deleted=", fake.thread_deleted
    )
    assert not fake.thread_deleted, (
        "whole-thread DELETE includes a human reply after the final history read"
    )


async def seed_slack_thread(world, auth, m, count=3):
    s._channel_access(m, times=count + 8)
    root = "1700000005.000100"
    m.post(s._CHAT_POST, payload={"ok": True, "ts": root})
    await s._slack_create_thread_impl(world.runtime, auth, channel_id=s._CHANNEL, content="root")
    ids = [root]
    for i in range(1, count):
        ids.append(
            await s._post(world, auth, m, f"1700000005.{100 + i:06d}", to=f"{s._CHANNEL}:{root}")
        )
    m.get(s._CONVERSATIONS_REPLIES, payload={"ok": True, "messages": [{"ts": x} for x in ids]})
    return ids


@pytest.mark.parametrize("change", ["protect", "pin", "seal"])
async def test_slack_bulk_delete_rechecks_each_effect(committing_sessionmaker, change):
    world = await s._world(committing_sessionmaker)
    auth, origin = await world.turn(parent="ELSEWHERE" if change == "seal" else s._CHANNEL)
    effects = []
    writers = []
    with aioresponses() as m:
        ids = await seed_slack_thread(world, auth, m)

        async def delete_callback(url, **kwargs):
            effects.append(str(kwargs["params"]["ts"]))
            if len(effects) == 1:
                policy = {
                    "protect": TenantAccessPolicy(protected_channel_ids=(s._CHANNEL,)),
                    "pin": TenantAccessPolicy(agent_channel_pins={s._AGENT: ("ELSEWHERE",)}),
                    "seal": TenantAccessPolicy(sealed_channel_ids=(s._CHANNEL,)),
                }[change]
                writers.append(asyncio.create_task(write_policy(world, policy)))
                await asyncio.sleep(0.02)
            return CallbackResult(payload={"ok": True})

        m.post(s._CHAT_DELETE, callback=delete_callback, repeat=True)
        with contextlib.suppress(ToolError):
            await s._slack_delete_thread_impl(
                world.runtime, auth, thread_id=f"{s._CHANNEL}:{ids[0]}", origin_context_id=origin
            )
    await asyncio.gather(*writers)
    print("bulk deletes after first deletion changed policy", change, effects)
    assert len(effects) == 1, "remaining Slack deletes ran after a committed policy refusal"


@pytest.mark.parametrize("check", ["audit", "volume"])
async def test_slack_bulk_delete_accounts_for_every_message(committing_sessionmaker, check):
    world = await s._world(committing_sessionmaker)
    auth, origin = await world.turn()
    with aioresponses() as m:
        ids = await seed_slack_thread(world, auth, m, count=11)
        m.post(s._CHAT_DELETE, payload={"ok": True}, repeat=True)
        with contextlib.suppress(ToolError):
            await s._slack_delete_thread_impl(
                world.runtime, auth, thread_id=f"{s._CHANNEL}:{ids[0]}", origin_context_id=origin
            )
        effects = [x["ts"] for x in s._deletes(m)]
    rows = [r for r in await world.audit() if r.outcome == "allowed"]
    print(
        "bulk volume",
        len(effects),
        "audit rows",
        [(r.target_message_id, r.content_hmac) for r in rows],
    )
    if check == "audit":
        assert set(effects) == {r.target_message_id for r in rows}, (
            "deleted replies have no per-message audit entry"
        )
        assert all(
            r.content_hmac
            == content_hash(
                "root" if r.target_message_id == ids[0] else "first draft", world.content_key
            )
            for r in rows
        )
    else:
        assert len(effects) <= 10, (
            "11 message deletes consume only one of 10 per-turn edits/deletes"
        )


async def test_concurrent_discord_edits_keep_correct_replaced_hash(
    committing_sessionmaker, fake, monkeypatch, db_engine, db_schema
):
    world = await d._world(committing_sessionmaker)
    auth, origin = await world.turn()
    mid = await d._post(world, auth)
    first_applied, release_first = asyncio.Event(), asyncio.Event()
    original = fake.handle

    async def handle(route, kwargs):
        response = await original(route, kwargs)
        if (
            route.method == "PATCH"
            and route.path.endswith("/{message_id}")
            and kwargs["json"]["content"] == "A"
        ):
            first_applied.set()
            await asyncio.wait_for(release_first.wait(), 5)
        return response

    d.patch_discord_http(monkeypatch, handle)

    other_engine = build_test_engine(db_engine.url, db_schema, pool_size=2, max_overflow=0)
    other_runtime = dataclasses.replace(
        world.runtime, session_factory=async_sessionmaker(other_engine, expire_on_commit=False)
    )

    async def edit(text):
        return await d._edit_message_impl(
            other_runtime if text == "B" else world.runtime,
            auth,
            channel_id=d._CHANNEL,
            message_id=mid,
            content=text,
            origin_context_id=origin,
        )

    a = asyncio.create_task(edit("A"))
    await asyncio.wait_for(first_applied.wait(), 5)
    b = asyncio.create_task(edit("B"))
    await asyncio.sleep(0.15)
    release_first.set()
    try:
        await asyncio.gather(a, b)
    finally:
        await other_engine.dispose()
    async with world.sessionmaker() as session:
        post = await get_post(
            session,
            tenant_id=world.tenant_id,
            platform="discord",
            channel_id=d._CHANNEL,
            message_id=mid,
        )
    rows = [r for r in await world.audit() if r.outcome == "allowed"]
    print(
        "concurrent edits",
        fake.messages[mid]["content"],
        post.content_hmac,
        "audit hashes",
        [r.content_hmac for r in rows],
    )
    assert fake.messages[mid]["content"] == "B"
    assert post.content_hmac == d._hmac("B"), (
        "late finish_edit overwrote the hash of the newer edit"
    )
    assert rows[1].content_hmac == d._hmac("A"), (
        "second edit audited the initial text, not what it replaced"
    )


@pytest.mark.parametrize("existing_policy", [False, True])
async def test_policy_writer_waits_for_platform_effect(
    committing_sessionmaker, fake, monkeypatch, existing_policy
):
    world = await d._world(committing_sessionmaker)
    auth, origin = await world.turn()
    mid = await d._post(world, auth)
    if existing_policy:
        await write_policy(world, TenantAccessPolicy())
    entered, release = asyncio.Event(), asyncio.Event()
    original = fake.handle

    async def handle(route, kwargs):
        if route.method == "PATCH" and route.path.endswith("/{message_id}"):
            entered.set()
            await release.wait()
        return await original(route, kwargs)

    d.patch_discord_http(monkeypatch, handle)
    edit = asyncio.create_task(
        d._edit_message_impl(
            world.runtime,
            auth,
            channel_id=d._CHANNEL,
            message_id=mid,
            content="updated",
            origin_context_id=origin,
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    writer = asyncio.create_task(
        write_policy(world, TenantAccessPolicy(protected_channel_ids=(d._CHANNEL,)))
    )
    try:
        await asyncio.sleep(0.1)
        assert not writer.done(), "policy writer committed while an authorized effect was in flight"
    finally:
        release.set()
        await asyncio.gather(edit, writer)
    with pytest.raises(ToolError, match="protected"):
        await d._delete_message_impl(
            world.runtime,
            auth,
            channel_id=d._CHANNEL,
            message_id=mid,
            origin_context_id=origin,
        )


async def test_platform_timeout_releases_policy_lock_and_refuses_uncertain_retry(
    committing_sessionmaker, fake, monkeypatch
):
    from daimon.adapters.mcp.tools import _tidy

    world = await d._world(committing_sessionmaker)
    auth, origin = await world.turn()
    mid = await d._post(world, auth)
    entered = asyncio.Event()
    original = fake.handle

    async def handle(route, kwargs):
        if route.method == "PATCH" and route.path.endswith("/{message_id}"):
            entered.set()
            await asyncio.Event().wait()
        return await original(route, kwargs)

    d.patch_discord_http(monkeypatch, handle)
    monkeypatch.setattr(_tidy, "PLATFORM_WRITE_TIMEOUT", 0.1, raising=False)
    edit = asyncio.create_task(
        d._edit_message_impl(
            world.runtime,
            auth,
            channel_id=d._CHANNEL,
            message_id=mid,
            content="unknown",
            origin_context_id=origin,
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    writer = asyncio.create_task(write_policy(world, TenantAccessPolicy()))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(edit, 2)
    await asyncio.wait_for(writer, 2)
    with pytest.raises(ToolError, match="not posted by you"):
        await d._delete_message_impl(
            world.runtime,
            auth,
            channel_id=d._CHANNEL,
            message_id=mid,
            origin_context_id=origin,
        )
    assert [r.outcome for r in await world.audit()] == ["allowed", "error", "denied"]
