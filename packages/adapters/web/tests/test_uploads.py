"""Shared files reach the agent as short-lived links; files a turn makes come back as files."""

from __future__ import annotations

import dataclasses
import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet
from daimon.adapters.web.files import file_kind, open_attachment, seal_attachment
from daimon.core.github_credentials import build_multifernet
from daimon.core.output_delivery import DeliverableFile
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from daimon.testing.turn_router import AGENT_ID, build_turn_router
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import build_turns, funded_workspace, one_session, signed_in, sse_frames
from .conftest import PUBLIC_URL, WebClientFactory, web_settings

_LINK = re.compile(rf"{re.escape(PUBLIC_URL)}(/api/chat/attachments/\S+)")
_HEADERS = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}


def _tool_state() -> TurnState:
    tool = ToolUseBlock(
        kind="tool_use",
        id="t1",
        type="agent.tool_use",
        name="bash",
        input={},
        status="complete",
    )
    return TurnState(content=[tool, TextBlock(kind="text", text="Here it is.")])


async def _chat(
    web_client: WebClientFactory,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    script: Any,
) -> tuple[Any, list[str]]:
    ws = await funded_workspace(factory)
    turns = build_turns(factory, build_turn_router(str(ws.tenant_id)), tmp_path)
    turns.deps = dataclasses.replace(
        turns.deps, fernet=build_multifernet((Fernet.generate_key().decode(),))
    )
    token = await signed_in(factory, ws.person)
    asked: list[str] = []

    async def turn(*_args: object, **kw: Any) -> SimpleNamespace:
        asked.append(kw["user_message"])
        state = _tool_state()
        await kw["lifecycle"].on_render(state)
        return SimpleNamespace(state=state, ma_session_id="sesn_web")

    async def sweep(*_args: object, post: Any, **_kw: Any) -> int:
        await post(
            DeliverableFile(
                file_id="file_1",
                filename="report.csv",
                mime_type="text/csv",
                size_bytes=4,
                content=b"a,b\n",
            )
        )
        return 1

    with (
        one_session(),
        patch("daimon.adapters.web.turns.run_prepared_turn", turn),
        patch("daimon.adapters.web.turns.sweep_session_outputs", sweep),
    ):
        async with web_client(web_settings(), sessionmaker=factory, turns=turns) as client:
            client.cookies.set("__Host-daimon_web", token, domain="web.example.com")
            client.headers.update(_HEADERS)
            sid = (await client.post("/api/chat/sessions", json={"agent_id": AGENT_ID})).json()[
                "id"
            ]
            result = await script(client, sid, asked)
            await turns.drain(10)
    return result, asked


async def test_a_shared_file_reaches_the_agent_as_a_link_anyone_with_it_can_fetch(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async def script(client: httpx.AsyncClient, sid: str, asked: list[str]) -> Any:
        upload = (
            await client.post(
                "/api/chat/uploads", files={"file": ("notes.txt", b"hello", "text/html")}
            )
        ).json()
        await client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"id": str(uuid.uuid4()), "content": "read it", "attachments": [upload]},
        )
        return upload

    upload, asked = await _chat(web_client, db_session_factory, tmp_path, script)

    assert upload["name"] == "notes.txt", "the upload is named as sent"
    found = _LINK.search(asked[0])
    assert found is not None, f"the agent gets a link: {asked[0]}"
    assert "[attachment] `notes.txt`" in asked[0], "the line names the file"
    async with web_client(web_settings(), sessionmaker=db_session_factory) as bare:
        missing = await bare.get(found.group(1))
    assert missing.status_code == 404, "without the turn's key the link opens nothing"


async def test_the_link_serves_the_bytes_as_a_download_until_it_expires() -> None:
    fernet = build_multifernet((Fernet.generate_key().decode(),))
    tenant_id = uuid.uuid4()
    sealed = seal_attachment(fernet, tenant_id=tenant_id, handle="h1")

    assert open_attachment(fernet, sealed) == (tenant_id, "h1"), "a fresh link opens"
    assert open_attachment(fernet, sealed, ttl_s=-1) is None, "an expired link opens nothing"
    assert open_attachment(fernet, sealed[:-4] + "AAAA") is None, "a forged link opens nothing"
    other = build_multifernet((Fernet.generate_key().decode(),))
    assert open_attachment(other, sealed) is None, "another key opens nothing"


async def test_an_upload_link_downloads_without_a_sign_in(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async def script(client: httpx.AsyncClient, sid: str, asked: list[str]) -> Any:
        upload = (
            await client.post(
                "/api/chat/uploads", files={"file": ("page.html", b"<script>", "text/html")}
            )
        ).json()
        await client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"id": str(uuid.uuid4()), "content": "read", "attachments": [upload]},
        )
        found = _LINK.search(asked[0])
        assert found is not None, f"the agent gets a link: {asked[0]}"
        client.cookies.clear()
        return await client.get(found.group(1))

    got, _ = await _chat(web_client, db_session_factory, tmp_path, script)

    assert got.status_code == 200 and got.content == b"<script>", "the link alone fetches it"
    assert got.headers["content-type"] == "application/octet-stream", "never rendered here"
    policies = got.headers.get_list("content-security-policy")
    assert "sandbox" in policies, f"and sandboxed if opened: {policies}"


async def test_a_turns_files_come_back_on_its_reply_for_its_owner_only(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async def script(client: httpx.AsyncClient, sid: str, asked: list[str]) -> Any:
        sent = await client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"id": str(uuid.uuid4()), "content": "make a csv"},
        )
        files = sse_frames(sent.text)[-1][1]["message"]["files"]
        got = await client.get(f"/api/chat/files/{files[0]['id']}")
        unknown = await client.get("/api/chat/files/nope")
        detail = (await client.get(f"/api/chat/sessions/{sid}")).json()
        return files, got, unknown, detail

    (files, got, unknown, detail), _ = await _chat(web_client, db_session_factory, tmp_path, script)

    assert [(f["name"], f["size"], f["kind"]) for f in files] == [("report.csv", 4, "csv")], files
    assert got.status_code == 200 and got.content == b"a,b\n", "the owner downloads the file"
    assert got.headers["content-type"] == "application/octet-stream", "never rendered here"
    assert got.headers["content-disposition"].startswith("attachment;"), "always a download"
    assert unknown.status_code == 404, "a file no reply carries is not served"
    reply = [m for m in detail["messages"] if m["role"] == "assistant"][-1]
    assert reply["files"] == files, "a reload shows the same files"


async def test_another_person_cannot_fetch_a_turns_file(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async def script(client: httpx.AsyncClient, sid: str, asked: list[str]) -> Any:
        sent = await client.post(
            f"/api/chat/sessions/{sid}/messages",
            json={"id": str(uuid.uuid4()), "content": "make a csv"},
        )
        return sse_frames(sent.text)[-1][1]["message"]["files"][0]["id"]

    file_id, _ = await _chat(web_client, db_session_factory, tmp_path, script)
    other = await funded_workspace(db_session_factory)
    token = await signed_in(db_session_factory, other.person)
    async with web_client(web_settings(), sessionmaker=db_session_factory) as c:
        c.cookies.set("__Host-daimon_web", token, domain="web.example.com")
        c.headers.update(_HEADERS)
        refused = await c.get(f"/api/chat/files/{file_id}")
    assert refused.status_code == 404, "files are served to the reply's owner only"


def test_file_kinds_pick_an_icon() -> None:
    kinds = [file_kind(n) for n in ("a.csv", "b.XLSX", "c.png", "d.pdf", "e.docx", "f")]
    assert kinds == ["csv", "xlsx", "png", "pdf", "pdf", "pdf"], kinds
