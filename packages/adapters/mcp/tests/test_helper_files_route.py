"""`/helper-files/{token}/{name}`: one ask's files, readable and writable only through its token."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from daimon.adapters.mcp.uploads import build_helper_files_route
from daimon.core.stores import helper_asks
from daimon.core.stores.domain import HelperAskRow, Role, UploadedFile
from daimon.core.stores.file_uploads import MAX_UPLOAD_BYTES, create_upload, store_upload_content
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route


async def _ask(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    running: bool = True,
    at: datetime | None = None,
) -> HelperAskRow:
    """An ask carrying one landed input file, claimed by its helper when `running`."""
    now = at or datetime.now(UTC)
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
        origin = await create_origin(
            session,
            tenant_id=tenant.id,
            account_id=account.id,
            platform="slack",
            parent_channel_id="C1",
            thread_id="1.2",
            responder_ma_agent_id="ag_lead",
            responder_name="lead",
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.USER,
            sender_foreign=False,
            shared_channel=False,
            expires_at=now + timedelta(minutes=10),
            now=now,
        )
        row, token = await create_upload(
            session,
            tenant_id=tenant.id,
            title="in",
            display_filename="in.csv",
            content_type="text/csv",
            now=now,
        )
        await store_upload_content(session, upload_token=token, data=b"a,b", now=now)
        ask = await helper_asks.create_ask(
            session,
            tenant_id=tenant.id,
            origin_id=origin.id,
            helper_agent_id="ag_argon",
            helper_name="argon",
            message="look",
            now=now,
            files=[UploadedFile(id=row.id, name="in.csv", size=3)],
        )
        if running:
            await helper_asks.claim_queued(session, origin_id=origin.id, skip_helpers=(), limit=1)
    return ask


def _client(sessionmaker: async_sessionmaker[AsyncSession]) -> httpx.AsyncClient:
    route = build_helper_files_route(sessionmaker)
    app = Starlette(routes=[Route("/helper-files/{token}/{name}", route, methods=["GET", "PUT"])])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _read(sessionmaker: async_sessionmaker[AsyncSession], ask: HelperAskRow) -> HelperAskRow:
    async with sessionmaker() as session:
        [read] = await helper_asks.list_asks(
            session, tenant_id=ask.tenant_id, origin_id=ask.origin_id, ids=[ask.id]
        )
    return read


async def test_a_helper_uploads_a_reply_file_and_the_asker_downloads_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The PUT lands in the ask's reply; the same token downloads input and reply files."""
    ask = await _ask(committing_sessionmaker)
    base = f"/helper-files/{ask.files_token}"

    async with _client(committing_sessionmaker) as client:
        put = await client.put(f"{base}/chart (1).png", content=b"PNG")
        read = await _read(committing_sessionmaker, ask)
        [sent] = read.reply_files
        got = await client.get(f"{base}/{sent.id}")
        given = await client.get(f"{base}/{ask.files[0].id}")

    assert put.status_code == 201, f"the upload lands: {put.text}"
    assert (sent.name, sent.size) == ("chart_1_.png", 3), "the filename is made safe"
    assert got.status_code == 200 and got.content == b"PNG", "the reply file downloads"
    assert got.headers["content-disposition"] == 'attachment; filename="chart_1_.png"', (
        "it downloads as a file, never rendered"
    )
    assert given.content == b"a,b", "the file the asker gave downloads too"


async def test_a_token_reads_only_its_own_asks_files(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Another ask's token, an unknown token or an expired ask find nothing."""
    mine = await _ask(committing_sessionmaker)
    theirs = await _ask(committing_sessionmaker)
    old = await _ask(committing_sessionmaker, at=datetime.now(UTC) - helper_asks.FILES_TTL)
    handle = mine.files[0].id

    async with _client(committing_sessionmaker) as client:
        crossed = await client.get(f"/helper-files/{theirs.files_token}/{handle}")
        unknown = await client.get(f"/helper-files/{uuid.uuid4().hex}/{handle}")
        expired = await client.get(f"/helper-files/{old.files_token}/{old.files[0].id}")

    assert crossed.status_code == 404, "a token reads only its ask's files"
    assert unknown.status_code == 404, "an unknown token reads nothing"
    assert expired.status_code == 404, "links expire"


async def test_uploads_are_refused_unless_the_helper_is_running_and_within_limits(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A queued or finished ask takes no files; the size and count caps hold."""
    queued = await _ask(committing_sessionmaker, running=False)
    full = await _ask(committing_sessionmaker)
    async with committing_sessionmaker.begin() as session:
        for n in range(helper_asks.MAX_FILES):
            await helper_asks.add_reply_file(
                session,
                ask_id=full.id,
                file=UploadedFile(id=f"h{n}", name="f", size=1),
                max_files=helper_asks.MAX_FILES,
            )

    async with _client(committing_sessionmaker) as client:
        early = await client.put(f"/helper-files/{queued.files_token}/a.txt", content=b"x")
        over = await client.put(f"/helper-files/{full.files_token}/a.txt", content=b"x")
        huge = await client.put(
            f"/helper-files/{full.files_token}/a.txt",
            content=b"x",
            headers={"content-length": str(MAX_UPLOAD_BYTES + 1)},
        )

    assert early.status_code == 404, "a queued ask takes no files"
    assert over.status_code == 409, "the reply holds at most MAX_FILES files"
    assert huge.status_code == 413, "the size cap is checked before the body is read"
    assert len((await _read(committing_sessionmaker, full)).reply_files) == helper_asks.MAX_FILES, (
        "a refused upload adds nothing"
    )
