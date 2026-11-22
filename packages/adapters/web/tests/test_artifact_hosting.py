"""Where page code runs and what reaches the viewer: the content host, the error pages,
`me()`, the viewer's consent to asks, version thumbnails and the page pool."""

from __future__ import annotations

import html
import json
import re
import struct
import zlib
from pathlib import Path
from typing import Any

from daimon.core.artifact_pages.model import page_url
from daimon.core.stores.domain import ArtifactRow
from daimon.testing.turn_router import build_turn_router
from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN
from .artifact_support import HTML, page_client, publish, team
from .chat_support import build_turns, funded_workspace, one_session, sse_frames
from .conftest import web_settings

PAGES = "https://{id}.pages.example.net"


def _path(page: ArtifactRow, link: str | None = None) -> str:
    return page_url("", page.public_id, page.slug, link)


def _src(shell: Response) -> str:
    found = re.search(r'data-src="([^"]+)"', shell.text)
    assert found, shell.text
    return html.unescape(found.group(1))


def _bridge(shell: Response) -> dict[str, Any]:
    found = re.search(r"const bridge = (\{.*?\});\n", shell.text)
    assert found, "the shell carries its bridge"
    return json.loads(found.group(1))


def _card(page: Response) -> tuple[int, str, str]:
    title = re.search(r"<h1>(.*?)</h1>", page.text)
    text = re.search(r"<p>(.*?)</p>", page.text)
    assert title and text, page.text
    return page.status_code, html.unescape(title.group(1)), html.unescape(text.group(1))


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\0" + b"\xff" * 3 * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


async def test_a_content_host_gives_each_page_its_own_origin_and_serves_nothing_else(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner)
    other = await publish(db_session_factory, t.owner, slug="other")
    web = web_settings(artifact_content_url=PAGES)
    own = f"https://{page.public_id}.pages.example.net"

    async with page_client(db_session_factory, t.member, web=web) as client:
        shell = await client.get(_path(page))
        src = _src(shell)
        path = src.removeprefix(own)
        content = await client.get(path, headers={"Host": f"{page.public_id}.pages.example.net"})
        swapped = await client.get(path, headers={"Host": f"{other.public_id}.pages.example.net"})
        app_route = await client.get(
            "/api/me", headers={"Host": f"{page.public_id}.pages.example.net"}
        )
        on_app = await client.get(path)
        elsewhere = await client.get(_path(page) + "?v=1", headers={"Host": "old.example.org"})

    assert src.startswith(own + f"/a/{page.public_id}/_content?t="), src
    assert 'sandbox="allow-scripts allow-forms allow-modals allow-downloads allow-same-origin"' in (
        shell.text
    )
    assert (
        f"frame-src {own}/a/{page.public_id}/_content;" in shell.headers["content-security-policy"]
    )
    assert content.status_code == 200, content.text
    csp = content.headers["content-security-policy"]
    assert "allow-same-origin" in csp, csp
    assert (
        "frame-src https://www.youtube.com/embed/ https://www.youtube-nocookie.com/embed/ " in csp
    )
    assert "https://drive.google.com/file/;" in csp and "drive.google.com;" not in csp, csp
    assert "frame-ancestors https://web.example.com;" in csp and "connect-src 'none'" in csp, csp
    assert content.headers["referrer-policy"] == "strict-origin"
    assert "x-frame-options" not in content.headers, "frame-ancestors names the shell's origin"
    assert swapped.status_code == 404, "one page's host never serves another page"
    assert app_route.status_code == 404, "the content host serves page content only"
    assert on_app.status_code == 404, (
        "with a content host, page code never runs on the app's origin"
    )
    assert elsewhere.status_code == 308
    assert elsewhere.headers["location"] == f"https://web.example.com{_path(page)}?v=1"


async def test_the_opaque_sandbox_opens_no_popups_and_adds_the_operators_hosts(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner)
    web = web_settings(artifact_asset_hosts="https://cdn.example.org")

    async with page_client(db_session_factory, t.member, web=web) as client:
        shell = await client.get(_path(page))
        content = await client.get(_src(shell))

    csp = content.headers["content-security-policy"]
    assert "allow-popups" not in csp and "allow-same-origin" not in csp, csp
    assert "allow-popups" not in shell.text, "the frame opens nothing; the shell asks first"
    assert "frame-src 'none'" in csp and "webrtc 'block'" in csp, csp
    assert "script-src 'unsafe-inline' 'unsafe-eval'" in csp and "https://cdn.example.org" in csp
    assert "res.cloudinary.com" not in csp, "no host that fetches any URL for a page"
    assert content.headers["x-dns-prefetch-control"] == "off"
    # The frame can only ever hold this page's content: it cannot navigate itself away.
    pin = f"frame-src https://web.example.com/a/{page.public_id}/_content;"
    assert pin in shell.headers["content-security-policy"]


async def test_error_pages_say_why_without_telling_missing_from_unshared(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    home = await funded_workspace(db_session_factory)
    page = await publish(db_session_factory, t.owner, slug="board")

    async with page_client(db_session_factory) as client:
        signed_out = await client.get(_path(page))
        bad_link = await client.get(_path(page, "not-the-link"))
    async with page_client(db_session_factory, home.person) as client:
        unshared = await client.get(_path(page))
        missing = await client.get("/a/zzzzzzzzzz/board")

    assert signed_out.status_code == 401
    assert f'const next = "/a/{page.public_id}/board";' in signed_out.text, "sign-in keeps the page"
    assert "location.hash" in signed_out.text, "and the #section the server never sees"
    assert _card(bad_link)[1] == "This link doesn't work"
    assert _card(unshared) == (404, "No access", "ada@example.com can't open this page.")
    assert _card(missing) == _card(unshared), "a missing page answers as an unshared one"
    assert 'id="switch"' in unshared.text and "/auth/logout" in unshared.text


async def test_me_tells_a_signed_in_viewer_their_email(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, data="read")

    async with page_client(db_session_factory, t.member) as client:
        content = await client.get(_src(await client.get(_path(page))))

    boot = re.search(r"window.__daimon_boot=(\{.*?\});", content.text)
    assert boot, content.text
    viewer = json.loads(boot.group(1))["viewer"]
    assert viewer == {"name": "Bea", "email": "bea@example.com", "kind": "member"}, viewer


class _Spy:
    """The page pool, counting the sessions page routes open on it."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory, self.opened = factory, 0

    def __call__(self) -> AsyncSession:
        self.opened += 1
        return self.factory()

    def begin(self) -> Any:
        self.opened += 1
        return self.factory.begin()


async def test_an_ask_needs_the_viewers_consent_for_the_current_version(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, ask=True)
    router = build_turn_router(str(t.tenant_id))
    turns = build_turns(db_session_factory, router, tmp_path)
    pool = _Spy(db_session_factory)
    api = f"/a/{page.public_id}/_api/"

    async def ask(client: AsyncClient) -> Response:
        answer = await client.post(api + "ask", json={"text": "Who won?"}, headers=OWN)
        await turns.drain(10)
        turns.draining = False
        return answer

    with one_session():
        async with page_client(
            db_session_factory, t.member, turns=turns, page_sessionmaker=pool
        ) as client:
            unasked = await ask(client)
            allowed = await client.post(api + "consent", json={}, headers=OWN)
            asked = await ask(client)
            state = await client.get(f"/api/artifacts/{page.id}/consent")
            await publish(db_session_factory, t.owner, html="<p>New</p>", ask=True)
            republished = await ask(client)
            await client.post(api + "consent", json={}, headers=OWN)
            revoked = await client.delete(f"/api/artifacts/{page.id}/consent", headers=OWN)
            after = await ask(client)

    assert unasked.status_code == 403, unasked.text
    assert unasked.json()["error"]["consent"], "the shell names the agent on its sheet"
    assert allowed.status_code == 200 and asked.status_code == 200, asked.text
    assert sse_frames(asked.text)[-1][1]["type"] == "done", asked.text
    assert state.json() == {"allowed": True}
    assert republished.status_code == 403, "a new version asks again"
    assert revoked.status_code == 204 and after.status_code == 403, "revoked in the web app"
    assert pool.opened >= 5, "page routes use the page pool"


async def test_a_managers_preview_sets_a_thumbnail_everyone_who_opens_the_page_sees(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner)
    url = f"/api/artifacts/{page.id}/versions/1/thumbnail"
    headers = {**OWN, "Content-Type": "image/png"}

    async with page_client(db_session_factory, t.owner) as client:
        before = _bridge(await client.get(_path(page)))["thumbnail"]
        bad = await client.put(url, content=b"<svg/>", headers=headers)
        huge = await client.put(url, content=_png(4000, 16), headers=headers)
        saved = await client.put(url, content=_png(64, 40), headers=headers)
        after = _bridge(await client.get(_path(page)))["thumbnail"]
    async with page_client(db_session_factory, t.member) as client:
        viewer_bridge = _bridge(await client.get(_path(page)))["thumbnail"]
        refused = await client.put(url, content=_png(64, 40), headers=headers)
        shown = await client.get(url)
        none = await client.get(f"/api/artifacts/{page.id}/versions/2/thumbnail")
    await publish(db_session_factory, t.owner, html=HTML + "<!-- v2 -->")
    async with page_client(db_session_factory, t.member) as client:
        old_for_viewer = await client.get(url)
    async with page_client(db_session_factory, t.owner) as client:
        old_for_manager = await client.get(url)

    assert before == url and after is None, "captured once, by a manager's preview"
    assert bad.status_code == 422 and huge.status_code == 422, "only a thumbnail-sized PNG"
    assert saved.status_code == 204, saved.text
    assert viewer_bridge is None and refused.status_code == 403, "viewers never set one"
    assert shown.status_code == 200 and shown.content == _png(64, 40)
    assert shown.headers["content-type"] == "image/png"
    assert shown.headers["x-content-type-options"] == "nosniff"
    assert none.status_code == 404
    assert old_for_viewer.status_code == 404, "older versions are for those who manage the page"
    assert old_for_manager.status_code == 200


async def test_a_page_with_data_or_asks_is_never_captured(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    async with page_client(db_session_factory, t.owner) as client:
        for slug, change in (("data", {"data": "read"}), ("asks", {"ask": True})):
            page = await publish(db_session_factory, t.owner, slug=slug, **change)
            thumbnail = _bridge(await client.get(_path(page)))["thumbnail"]
            assert thumbnail is None, f"a page with {slug} shows data the picture would keep"
