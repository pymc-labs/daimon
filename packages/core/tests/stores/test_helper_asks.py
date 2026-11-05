"""Helper asks: one running ask per helper, finished once, stopped with the asking turn."""

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core.stores import file_uploads, helper_asks
from daimon.core.stores.domain import HelperAskRow, Role, TurnOriginRow, UploadedFile
from daimon.core.stores.turn_origins import create_origin, get_active_origin, mark_serving_helpers
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession


async def _origin(session: AsyncSession, helper_of: uuid.UUID | None = None) -> TurnOriginRow:
    tenant = await make_tenant(session)
    account = await make_account(session, tenant=tenant)
    now = datetime.now(UTC)
    return await create_origin(
        session,
        tenant_id=tenant.id,
        account_id=account.id,
        platform="slack",
        parent_channel_id="C1",
        thread_id="1.2",
        responder_ma_agent_id="agent_lead",
        responder_name="lead",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        expires_at=now + timedelta(minutes=10),
        now=now,
        helper_of=helper_of,
    )


async def _ask(session: AsyncSession, origin: TurnOriginRow, helper: str, at: int) -> HelperAskRow:
    return await helper_asks.create_ask(
        session,
        tenant_id=origin.tenant_id,
        origin_id=origin.id,
        helper_agent_id=helper,
        helper_name=helper,
        message="check it",
        now=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=at),
    )


async def test_claims_the_oldest_ask_of_each_idle_helper(db_session: AsyncSession) -> None:
    origin = await _origin(db_session)
    first = await _ask(db_session, origin, "agent_a", 0)
    await _ask(db_session, origin, "agent_a", 1)
    other = await _ask(db_session, origin, "agent_b", 2)
    await _ask(db_session, origin, "agent_c", 3)

    claimed = await helper_asks.claim_queued(
        db_session, origin_id=origin.id, skip_helpers=(), limit=2
    )

    assert [ask.id for ask in claimed] == [first.id, other.id]
    assert {ask.status for ask in claimed} == {"running"}
    busy = await helper_asks.claim_queued(
        db_session, origin_id=origin.id, skip_helpers={"agent_a", "agent_b"}, limit=3
    )
    assert [ask.helper_agent_id for ask in busy] == ["agent_c"], "a busy helper's next ask waits"


async def test_an_ask_finishes_once_and_the_turn_end_stops_the_rest(
    db_session: AsyncSession,
) -> None:
    origin = await _origin(db_session)
    done = await _ask(db_session, origin, "agent_a", 0)
    open_ = await _ask(db_session, origin, "agent_b", 1)
    now = datetime.now(UTC)

    await helper_asks.finish_ask(db_session, ask_id=done.id, status="done", reply="ok", now=now)
    await helper_asks.stop_open_asks(db_session, origin_id=origin.id, now=now)
    await helper_asks.finish_ask(db_session, ask_id=open_.id, status="done", reply="late", now=now)

    asks = await helper_asks.list_asks(db_session, tenant_id=origin.tenant_id, origin_id=origin.id)
    assert [(a.status, a.reply) for a in asks] == [("done", "ok"), ("stopped", None)]
    assert await helper_asks.count_asks(db_session, origin_id=origin.id) == 2


async def test_asks_are_read_only_by_their_own_turn(db_session: AsyncSession) -> None:
    mine = await _origin(db_session)
    theirs = await _origin(db_session)
    ask = await _ask(db_session, theirs, "agent_a", 0)

    assert (
        await helper_asks.list_asks(
            db_session, tenant_id=mine.tenant_id, origin_id=mine.id, ids=[ask.id]
        )
        == []
    )
    assert (
        await helper_asks.list_asks(
            db_session, tenant_id=mine.tenant_id, origin_id=theirs.id, ids=[ask.id]
        )
        == []
    ), "another tenant's turn is never read"


async def test_an_origin_records_helper_turns_and_serving_turns(db_session: AsyncSession) -> None:
    asking = await _origin(db_session)
    helper = await _origin(db_session, helper_of=asking.id)
    await mark_serving_helpers(db_session, origin_id=asking.id)

    read = await get_active_origin(
        db_session,
        origin_id=asking.id,
        tenant_id=asking.tenant_id,
        account_id=asking.account_id,
        platform="slack",
        now=datetime.now(UTC),
    )
    assert read is not None and read.serves_helpers and read.helper_of is None
    assert helper.helper_of == asking.id and not helper.serves_helpers


async def test_an_asks_files_token_finds_it_until_the_links_expire(
    db_session: AsyncSession,
) -> None:
    """Each ask has its own unguessable token, and its links stop working after FILES_TTL."""
    origin = await _origin(db_session)
    given = UploadedFile(id="h1", name="in.csv", size=3)
    created = datetime.now(UTC)
    ask = await helper_asks.create_ask(
        db_session,
        tenant_id=origin.tenant_id,
        origin_id=origin.id,
        helper_agent_id="agent_a",
        helper_name="a",
        message="look",
        now=created,
        files=[given],
    )
    other = await _ask(db_session, origin, "agent_b", 0)
    assert ask.files_token and other.files_token, "every ask gets a token"
    assert ask.files_token != other.files_token, "tokens are per ask"

    found = await helper_asks.get_ask_by_files_token(db_session, token=ask.files_token, now=created)
    late = await helper_asks.get_ask_by_files_token(
        db_session, token=ask.files_token, now=created + helper_asks.FILES_TTL
    )

    assert found is not None and found.id == ask.id, "the token finds its ask"
    assert found.files == [given], "the ask keeps the files it was given"
    assert late is None, "expired links find nothing"


async def test_reply_files_are_added_only_while_running_and_up_to_the_cap(
    db_session: AsyncSession,
) -> None:
    """A helper adds files to a running ask until the cap; an ended ask takes none."""
    origin = await _origin(db_session)
    ask = await _ask(db_session, origin, "agent_a", 0)

    def file(n: int) -> UploadedFile:
        return UploadedFile(id=f"h{n}", name=f"f{n}.txt", size=n)

    async def add(n: int, *, max_files: int = 2) -> bool:
        return await helper_asks.add_reply_file(
            db_session, ask_id=ask.id, file=file(n), max_files=max_files, max_bytes=1000
        )

    queued = await add(0)
    await helper_asks.claim_queued(db_session, origin_id=origin.id, skip_helpers=(), limit=1)
    added = [await add(n) for n in (1, 2, 3)]
    now = datetime.now(UTC)
    await helper_asks.finish_ask(db_session, ask_id=ask.id, status="done", reply="ok", now=now)
    after = await add(4, max_files=9)

    [read] = await helper_asks.list_asks(
        db_session, tenant_id=origin.tenant_id, origin_id=origin.id
    )
    assert not queued, "a queued ask takes no reply files"
    assert added == [True, True, False], "the cap holds"
    assert not after, "a finished ask takes no more files"
    assert read.reply_files == [file(1), file(2)], "the reply keeps the files in order"


async def test_reply_files_stop_at_the_total_bytes_cap(db_session: AsyncSession) -> None:
    """The byte cap counts what the reply already holds, so the last file that fits is the
    one that fits exactly; the files cap and the bytes cap are one check."""
    origin = await _origin(db_session)
    ask = await _ask(db_session, origin, "agent_a", 0)
    await helper_asks.claim_queued(db_session, origin_id=origin.id, skip_helpers=(), limit=1)

    async def add(n: int, size: int) -> bool:
        return await helper_asks.add_reply_file(
            db_session,
            ask_id=ask.id,
            file=UploadedFile(id=f"h{n}", name=f"f{n}", size=size),
            max_files=10,
            max_bytes=100,
        )

    results = [await add(0, 60), await add(1, 41), await add(2, 40), await add(3, 1)]

    assert results == [True, False, True, False], "60 + 40 fills the cap; one more byte is over"
    [read] = await helper_asks.list_asks(
        db_session, tenant_id=origin.tenant_id, origin_id=origin.id
    )
    assert [f.id for f in read.reply_files] == ["h0", "h2"], "a refused file is not recorded"


async def test_reply_uploads_go_once_their_asks_links_expire(db_session: AsyncSession) -> None:
    """The sweep deletes the files helpers sent back on an expired ask, and only those."""
    origin = await _origin(db_session)
    now = datetime.now(UTC)

    async def land(name: str) -> UploadedFile:
        row, token = await file_uploads.create_upload(
            db_session,
            tenant_id=origin.tenant_id,
            title=name,
            display_filename=name,
            content_type="text/plain",
            now=now,
        )
        await file_uploads.store_upload_content(db_session, upload_token=token, data=b"x", now=now)
        return UploadedFile(id=row.id, name=name, size=1)

    async def ask_with_reply(name: str, created: datetime) -> UploadedFile:
        ask = await helper_asks.create_ask(
            db_session,
            tenant_id=origin.tenant_id,
            origin_id=origin.id,
            helper_agent_id=f"agent_{name}",
            helper_name=name,
            message="m",
            now=created,
        )
        await helper_asks.claim_queued(db_session, origin_id=origin.id, skip_helpers=(), limit=9)
        file = await land(name)
        assert await helper_asks.add_reply_file(
            db_session, ask_id=ask.id, file=file, max_files=10, max_bytes=100
        ), "the reply takes the file"
        return file

    old = await ask_with_reply("old", now - helper_asks.FILES_TTL - timedelta(minutes=1))
    passed_on = await ask_with_reply("passed", now - helper_asks.FILES_TTL - timedelta(minutes=1))
    await helper_asks.create_ask(
        db_session,
        tenant_id=origin.tenant_id,
        origin_id=origin.id,
        helper_agent_id="agent_next",
        helper_name="next",
        message="m",
        now=now,
        files=[passed_on],
    )
    fresh = await ask_with_reply("fresh", now)
    unrelated = await land("unrelated")

    removed = await helper_asks.delete_expired_reply_uploads(db_session, now=now)

    kept = await file_uploads.list_landed_uploads(
        db_session,
        tenant_id=origin.tenant_id,
        handle_ids=[old.id, passed_on.id, fresh.id, unrelated.id],
    )
    assert removed == 1, "one expired reply file"
    assert {f.id for f in kept} == {passed_on.id, fresh.id, unrelated.id}, (
        "a live ask's file, one passed on to a live ask, and others stay"
    )
