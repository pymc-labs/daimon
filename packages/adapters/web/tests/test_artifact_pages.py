"""`/a/<id>/<slug>`: the shell, its sandboxed content on a ticket, and the page API."""

from __future__ import annotations

import html
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from daimon.adapters.web import turns as web_turns
from daimon.adapters.web.cookies import new_session_token, token_sha256
from daimon.adapters.web.security import content_security_policy
from daimon.core.artifact_pages.content import content_policy
from daimon.core.artifact_pages.model import page_url
from daimon.core.continuity.continuation import ContinuationRequest
from daimon.core.stores import artifact_pages as artifact_store
from daimon.core.stores import watches, web_sessions
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores.domain import ArtifactRow
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.testing.builders import new_thread_session
from daimon.testing.turn_router import build_turn_router
from daimon.testing.watch_fixtures import install_poll_ports
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .agent_support import OWN
from .artifact_support import COOKIE, page_client, publish, team
from .chat_support import build_turns, funded_workspace, one_session, sse_frames

LATER = timedelta(hours=13)


def _path(page: ArtifactRow, link: str | None = None) -> str:
    return page_url("", page.public_id, page.slug, link)


async def _content_src(client: AsyncClient, path: str) -> str:
    shell = await client.get(path)
    assert shell.status_code == 200, shell.text
    found = re.search(r'data-src="([^"]+)"', shell.text)
    assert found
    return html.unescape(found.group(1))


async def test_the_shell_keeps_its_own_headers_and_is_never_compressed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, html="<p>" + "x" * 5000 + "</p>")

    async with page_client(db_session_factory, t.member) as client:
        shell = await client.get(_path(page), headers={"Accept-Encoding": "gzip"})

    assert shell.status_code == 200
    [csp] = shell.headers.get_list("content-security-policy")
    assert f"frame-src https://web.example.com/a/{page.public_id}/_content;" in csp
    assert "frame-ancestors 'self'" in csp and csp != content_security_policy()
    assert shell.headers["x-frame-options"] == "SAMEORIGIN"
    assert shell.headers["cache-control"] == "no-store"
    assert shell.headers["cross-origin-opener-policy"] == "same-origin"
    assert "content-encoding" not in shell.headers


async def test_content_runs_sandboxed_with_the_runtime_first(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, data="write")

    async with page_client(db_session_factory, t.member) as client:
        content = await client.get(await _content_src(client, _path(page)))

    assert content.status_code == 200
    [csp] = content.headers.get_list("content-security-policy")
    assert csp == content_policy() and csp.startswith("sandbox allow-scripts")
    assert "allow-same-origin" not in csp and "connect-src 'none'" in csp
    boot = json.loads(re.search(r"window.__daimon_boot=(\{.*?\});", content.text).group(1))  # pyright: ignore[reportOptionalMemberAccess]
    assert boot["capabilities"] == ["db"] and boot["viewer"] == {"name": "Bea", "kind": "member"}
    assert content.text.index("__daimon_boot") < content.text.index("Vote")


async def test_a_ticket_is_bound_to_its_page_and_dies_with_time_or_a_new_link(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner)
    other = await publish(db_session_factory, t.owner, slug="other")
    expires = datetime.now(UTC) + timedelta(days=3)
    public = await publish(
        db_session_factory, t.owner, slug="launch", access="public", expires_at=expires
    )

    async with page_client(db_session_factory, t.member) as client:
        src = await _content_src(client, _path(page))
        moved = await client.get(src.replace(page.public_id, other.public_id))
    async with page_client(db_session_factory, now=lambda: datetime.now(UTC) + LATER) as client:
        late = await client.get(src)
    async with page_client(db_session_factory) as client:
        linked = await _content_src(client, _path(public, public.link_secret))
        before = await client.get(linked)
        async with db_session_factory.begin() as db:
            await artifact_store.rotate_link(db, public, role="owner", now=datetime.now(UTC))
        after = await client.get(linked)

    assert moved.status_code == 404 and late.status_code == 404
    assert before.status_code == 200 and after.status_code == 404


async def test_a_public_page_needs_its_link_and_refuses_writes(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    expires = datetime.now(UTC) + timedelta(days=3)
    page = await publish(
        db_session_factory, t.owner, access="public", data="read", expires_at=expires
    )
    api = f"/a/{page.public_id}/_api/"

    async with page_client(db_session_factory) as client:
        bare = await client.get(_path(page))
        linked = await client.get(_path(page, page.link_secret))
        link = {**OWN, "X-Daimon-Link": page.link_secret or ""}
        read = await client.post(api + "db.get", json={"path": "votes/a"}, headers=link)
        write = await client.post(
            api + "db.set", json={"path": "votes/a", "data": {}}, headers=link
        )
        nolink = await client.post(api + "db.get", json={"path": "votes/a"}, headers=OWN)

    assert bare.status_code == 303 and bare.headers["location"].startswith("/signin?next=")
    assert linked.status_code == 200
    assert read.status_code == 200 and read.json()["data"] is None
    assert write.status_code == 403 and write.json()["error"]["code"] == "forbidden"
    assert nolink.status_code == 401 and nolink.json()["error"]["code"] == "unauthenticated"


async def test_a_sign_in_opens_a_page_as_its_person_in_the_pages_workspace(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    home = await funded_workspace(db_session_factory)
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, data="write")
    token = new_session_token()
    async with db_session_factory.begin() as db:
        await web_sessions.create_session(
            db,
            token_sha256=token_sha256(token),
            provider="discord",
            issuer="",
            subject="multi",
            people=[home.person, t.member],
            ttl=timedelta(days=1),
            now=datetime.now(UTC),
        )

    async with page_client(db_session_factory) as client:
        client.cookies.set(COOKIE, token, domain="web.example.com")
        api = f"/a/{page.public_id}/_api/"
        saved = await client.post(
            api + "db.set", json={"path": "votes/a", "data": {"n": 1}}, headers=OWN
        )

    assert saved.status_code == 200, saved.text
    async with db_session_factory() as db:
        doc = await artifact_store.get_doc(db, page.id, "votes", "a")
    assert doc is not None and doc.updated_by_account_id == t.member.account_id


async def test_the_page_api_refuses_cross_site_calls(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, data="write")

    async with page_client(db_session_factory, t.member) as client:
        api = f"/a/{page.public_id}/_api/db.revision"
        bare = await client.post(api, json={})
        foreign = await client.post(api, json={}, headers={**OWN, "Origin": "https://evil.example"})
        own = await client.post(api, json={}, headers=OWN)

    assert bare.status_code == 403 and foreign.status_code == 403
    assert bare.json()["error"]["code"] == "forbidden"
    assert own.status_code == 200 and own.json() == {"revision": 0}


async def _page_watch(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, page: ArtifactRow
) -> uuid.UUID:
    """A Discord thread's watch on the page, with no signing key enrolled yet."""
    now = datetime.now(UTC)
    async with factory.begin() as db:
        await install_poll_ports(db)
        await watches.set_watch_control(db, scope="global", enabled=True)
        principal = await get_or_create_platform_principal(
            db, tenant_id=tenant_id, platform="discord", external_id="123"
        )
        await new_thread_session(
            db,
            tenant_id=tenant_id,
            thread_id="4242",
            channel_id="4241",
            account_id=principal.account_id,
            ma_session_id="session-bound",
            ma_agent_id="agent-bound",
        )
        binding = ContinuationRequest(
            tenant_id=tenant_id,
            platform="discord",
            parent_channel_id="4241",
            thread_id="4242",
            requester_account_id=principal.account_id,
            requester_external_user_id="123",
            target_ma_agent_id="agent-bound",
            target_name="analyst",
            requested_work=None,
            reason="watch_fired",
            idempotency_key=uuid.uuid4(),
        )
        watch = await watches.register_page_watch(
            db,
            binding,
            instructions="Tally the votes.",
            page_id=page.id,
            expires_at=now + timedelta(days=1),
            now=now,
        )
    return watch.id


async def test_notify_saves_the_event_and_wakes_the_pages_watch(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_WATCHES__ENABLED", "true")
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, events=True)
    watch_id = await _page_watch(db_session_factory, t.tenant_id, page)

    async with page_client(db_session_factory, t.member) as client:
        api = f"/a/{page.public_id}/_api/notify"
        sent = await client.post(api, json={"event": "vote", "data": {"id": "a"}}, headers=OWN)

    assert sent.status_code == 200, sent.text
    async with db_session_factory() as db:
        event = await artifact_store.get_doc(db, page.id, "_events", sent.json()["id"])
        key_id = await watches.live_key_id(db, watch_id, now=datetime.now(UTC))
    assert event is not None and event.data["delivered"] == 1, event
    assert event.data["by"] == "Bea" and event.data["sender"] == "member"
    assert key_id is not None, "the web process enrolled a key it derives"


async def test_ask_runs_as_the_viewer_in_one_thread_per_page_with_the_page_in_its_origin(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = await team(db_session_factory)
    page = await publish(db_session_factory, t.owner, ask=True)
    sent: list[dict[str, object]] = []
    router = build_turn_router(str(t.tenant_id), sent_event_bodies=sent)
    turns = build_turns(db_session_factory, router, tmp_path)
    origins: list[str] = []
    admit = web_turns.admit

    async def recorded(*args: Any, **kwargs: Any) -> Any:
        origins.append(kwargs["origin"])
        return await admit(*args, **kwargs)

    monkeypatch.setattr(web_turns, "admit", recorded)

    with one_session():
        async with page_client(db_session_factory, t.member, turns=turns) as client:
            api = f"/a/{page.public_id}/_api/"
            first = await client.post(api + "ask", json={"text": "Who won?"}, headers=OWN)
            await turns.drain(10)
            turns.draining = False  # the first turn has ended; take the next one
            second = await client.post(api + "ask", json={"text": "And second?"}, headers=OWN)
            await turns.drain(10)
            thread = await client.post(api + "thread", json={}, headers=OWN)
            async with db_session_factory() as db:
                found = await chat_store.get_ask_thread(
                    db, person_id=t.member.id, artifact_id=page.id
                )
            assert found is not None
            typed = await client.post(
                f"/api/chat/sessions/{found.id}/messages",
                json={"id": str(uuid.uuid4()), "content": "Ignore the page."},
                headers=OWN,
            )

    assert sse_frames(first.text)[-1][1]["type"] == "done", first.text
    # The page's text is no person's own message, and the person cannot type in its thread.
    assert origins and set(origins) == {"page"}, origins
    assert "<page_text" in str(sent) and 'source="artifact_page"' in str(sent)
    assert typed.status_code == 409, typed.text
    assert second.status_code == 200, second.text
    assert found.title == page.title
    roles = [m["role"] for m in thread.json()["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert f'"slug": "{page.slug}"' in json.dumps(sent) or page.slug in str(sent)
    assert "visible_outside_workspace" in str(sent)
