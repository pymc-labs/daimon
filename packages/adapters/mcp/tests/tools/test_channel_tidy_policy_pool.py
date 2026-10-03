"""Sol's pool probe: real tidy, audit and policy writers; scheduling barriers only."""

import asyncio
import dataclasses

import pytest
from daimon.adapters.mcp.tools import _tidy
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.stores import access_policy
from daimon.testing.db import build_test_engine
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from . import test_channel_tidy_discord as d


@pytest.mark.parametrize("capacity", [4, 15])
@pytest.mark.parametrize("contended", [False, True], ids=["quiet", "policy-writers"])
async def test_policy_waiters_leave_audit_headroom(
    committing_sessionmaker, db_engine, db_schema, monkeypatch, capacity, contended
):
    fake = d._FakeDiscord()
    d.patch_discord_http(monkeypatch, fake.handle)
    world = await d._world(committing_sessionmaker)
    auth, origin = await world.turn()
    mid = await d._post(world, auth)
    engine = build_test_engine(
        db_engine.url,
        db_schema,
        pool_size=min(5, capacity),
        max_overflow=max(0, capacity - 5),
        pool_timeout=2,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    runtime = dataclasses.replace(world.runtime, session_factory=factory)
    entered, release = asyncio.Event(), asyncio.Event()
    sleeping = set()
    original = _tidy.begin_action
    original_sleep = asyncio.sleep
    writers = []

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    async def observed_sleep(delay):
        if asyncio.current_task() in writers:
            sleeping.add(asyncio.current_task())
            await release.wait()
        await original_sleep(delay)

    monkeypatch.setattr(_tidy, "begin_action", paused)
    monkeypatch.setattr(asyncio, "sleep", observed_sleep)

    async def write():
        # Replay the same regression on 636e4f93, before the retry helper exists.
        transaction = getattr(access_policy, "policy_write_transaction", None)
        context = (
            transaction(factory, tenant_id=world.tenant_id) if transaction else factory.begin()
        )
        async with context as session:
            await access_policy.set_access_policy(
                session, tenant_id=world.tenant_id, policy=TenantAccessPolicy()
            )

    edit = asyncio.create_task(
        d._edit_message_impl(
            runtime,
            auth,
            channel_id=d._CHANNEL,
            message_id=mid,
            content="after",
            origin_context_id=origin,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if contended:
            writers = [asyncio.create_task(write()) for _ in range(capacity - 1)]
            async with asyncio.timeout(5):
                while True:
                    async with committing_sessionmaker() as observer:
                        waits = (
                            await observer.execute(
                                text(
                                    "SELECT count(*) FROM pg_stat_activity "
                                    "WHERE application_name=:schema "
                                    "AND cardinality(pg_blocking_pids(pid)) > 0 "
                                    "AND query LIKE '%pg_advisory_xact_lock%'"
                                ),
                                {"schema": db_schema},
                            )
                        ).scalar_one()
                    if waits == capacity - 1 or len(sleeping) == capacity - 1:
                        break
                    await original_sleep(0.01)
            if sleeping:
                assert engine.pool.checkedout() == 1, "retrying writers retained connections"
        release.set()
        results = await asyncio.wait_for(asyncio.gather(edit, *writers, return_exceptions=True), 8)
        failures = [r for r in results if isinstance(r, BaseException)]
        assert not failures, f"policy waiters starved tidy's pre-effect audit: {failures}"
        assert fake.messages[mid]["content"] == "after"
    finally:
        release.set()
        for task in [edit, *writers]:
            if not task.done():
                task.cancel()
        await asyncio.gather(edit, *writers, return_exceptions=True)
        await engine.dispose()


@pytest.mark.parametrize("caller", ["writer", "tidy"])
async def test_policy_acquisition_is_bounded_and_releases_resources(
    committing_sessionmaker, db_engine, db_schema, monkeypatch, caller
):
    from daimon.core import session_preparation_gate
    from daimon.core.errors import DaimonError
    from fastmcp.exceptions import ToolError

    fake = d._FakeDiscord()
    d.patch_discord_http(monkeypatch, fake.handle)
    world = await d._world(committing_sessionmaker)
    auth, origin = await world.turn()
    mid = await d._post(world, auth)
    engine = build_test_engine(db_engine.url, db_schema, pool_size=5, max_overflow=10)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    runtime = dataclasses.replace(world.runtime, session_factory=factory)
    backoff, resume = asyncio.Event(), asyncio.Event()
    original_sleep = asyncio.sleep
    waiter = None

    async def observed_sleep(delay):
        if asyncio.current_task() is waiter:
            backoff.set()
            await resume.wait()
        await original_sleep(delay)

    async def write():
        transaction = getattr(access_policy, "policy_write_transaction", None)
        context = (
            transaction(factory, tenant_id=world.tenant_id) if transaction else factory.begin()
        )
        async with context as session:
            await access_policy.set_access_policy(
                session, tenant_id=world.tenant_id, policy=TenantAccessPolicy()
            )

    monkeypatch.setattr(asyncio, "sleep", observed_sleep)
    try:
        async with committing_sessionmaker.begin() as holder:
            # Shared holder blocks a writer; exclusive holder blocks tidy.
            function = (
                "pg_advisory_xact_lock_shared" if caller == "writer" else "pg_advisory_xact_lock"
            )
            await holder.execute(
                text(f"SELECT {function}(hashtextextended(current_schema() || ':' || :key, 0))"),
                {
                    "key": access_policy._policy_write_key(world.tenant_id),
                },
            )
            waiter = asyncio.create_task(
                write()
                if caller == "writer"
                else d._edit_message_impl(
                    runtime,
                    auth,
                    channel_id=d._CHANNEL,
                    message_id=mid,
                    content="after",
                    origin_context_id=origin,
                )
            )
            await asyncio.wait_for(backoff.wait(), 2)
            assert not waiter.done()
            assert engine.pool.checkedout() == 0
            if caller == "tidy":
                assert session_preparation_gate._tidy_gate._value == 1
                assert session_preparation_gate._pool_gates[engine.pool]._value == 5
            # Exhaust the actual production deadline without sleeping 20 seconds.
            loop = asyncio.get_running_loop()
            original_time = loop.time
            bound = access_policy.POLICY_WRITE_TIMEOUT_S
            monkeypatch.setattr(loop, "time", lambda: original_time() + bound + 1)
            resume.set()
            with pytest.raises(
                DaimonError if caller == "writer" else ToolError, match="policy is busy, try again"
            ):
                await asyncio.wait_for(waiter, 2)
            assert engine.pool.checkedout() == 0
            assert fake.messages[mid]["content"] != "after"
        if caller == "tidy":
            assert any(
                r.outcome == "denied" and r.reason == "policy_changed" for r in await world.audit()
            )
    finally:
        resume.set()
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await engine.dispose()


@pytest.mark.parametrize("operation", ["set", "clear"])
async def test_caller_owned_store_transaction_fails_fast_on_busy(
    committing_sessionmaker, monkeypatch, operation
):
    from daimon.core.errors import DaimonError

    world = await d._world(committing_sessionmaker)
    async with committing_sessionmaker.begin() as holder:
        await holder.execute(
            text(
                "SELECT pg_advisory_xact_lock_shared(hashtextextended(current_schema() || ':' || :key, 0))"
            ),
            {
                "key": access_policy._policy_write_key(world.tenant_id),
            },
        )
        async with asyncio.timeout(1):
            with pytest.raises(DaimonError, match="policy is busy, try again"):
                async with committing_sessionmaker.begin() as session:
                    if operation == "set":
                        await access_policy.set_access_policy(
                            session, tenant_id=world.tenant_id, policy=TenantAccessPolicy()
                        )
                    else:
                        await access_policy.clear_access_policy(session, tenant_id=world.tenant_id)


@pytest.mark.parametrize("own", [True, False], ids=["keep", "release"])
async def test_channel_rule_writer_waits_for_tidy_effect(committing_sessionmaker, monkeypatch, own):
    from daimon.core.authz import Subject
    from daimon.core.channel_rules import set_channel_rule
    from daimon.core.scope import ChannelScopeRef, DeploymentDefault
    from daimon.core.stores.scoped_config_write import set_fields

    world = await d._world(committing_sessionmaker)
    auth, origin = await world.turn()
    fake = d._FakeDiscord()
    d.patch_discord_http(monkeypatch, fake.handle)
    mid = await d._post(world, auth)
    async with committing_sessionmaker.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=world.tenant_id, channel_id=d._CHANNEL),
            tenant_id=world.tenant_id,
            agent_name=d._AGENT,
            mode="agent",
        )
        if not own:
            await access_policy.set_access_policy(
                session,
                tenant_id=world.tenant_id,
                policy=TenantAccessPolicy(
                    channel_rules={d._CHANNEL: ChannelRule(readers="own", writers="own")},
                    agent_rules={d._AGENT: AgentRule(runs_in=(d._CHANNEL,))},
                ),
            )
    entered, release, backoff = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_handle, original_sleep = fake.handle, asyncio.sleep
    writer = None

    async def handle(route, kwargs):
        if route.method == "PATCH" and route.path.endswith("/{message_id}"):
            entered.set()
            await release.wait()
        return await original_handle(route, kwargs)

    async def observed_sleep(delay):
        if asyncio.current_task() is writer:
            backoff.set()
        await original_sleep(delay)

    d.patch_discord_http(monkeypatch, handle)
    monkeypatch.setattr(asyncio, "sleep", observed_sleep)
    edit = asyncio.create_task(
        d._edit_message_impl(
            world.runtime,
            auth,
            channel_id=d._CHANNEL,
            message_id=mid,
            content="after",
            origin_context_id=origin,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        writer = asyncio.create_task(
            set_channel_rule(
                world.runtime.client,
                committing_sessionmaker,
                tenant_id=world.tenant_id,
                platform="discord",
                channel_id=d._CHANNEL,
                readers="own" if own else "any",
                release_agents=not own,
                default=DeploymentDefault(agent_name="daimon"),
                actor_account_id=None,
                subject=Subject(is_admin=True),
            )
        )
        await asyncio.wait_for(backoff.wait(), 2)
        assert not writer.done(), "the rule committed during tidy's guarded effect"
        async with committing_sessionmaker() as observer:
            policy = await access_policy.load_access_policy(observer, tenant_id=world.tenant_id)
        assert (d._CHANNEL in policy.channel_rules) is not own
        release.set()
        await asyncio.wait_for(edit, 2)
        change = await asyncio.wait_for(writer, 5)
        assert (change.rule.readers == "own") is own
        assert change.changed
        assert fake.messages[mid]["content"] == "after"
        async with committing_sessionmaker() as observer:
            policy = await access_policy.load_access_policy(observer, tenant_id=world.tenant_id)
        assert (d._CHANNEL in policy.channel_rules) is own
    finally:
        release.set()
        for task in [edit, writer]:
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*[t for t in [edit, writer] if t is not None], return_exceptions=True)
