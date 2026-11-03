"""Never-tag on every platform: the self-only tool, the gate that honours it, batch sends."""

from __future__ import annotations

import dataclasses
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import _outbound, channels
from daimon.adapters.mcp.tools._outbound import OutgoingPost, TurnScope, prepare_posts
from daimon.adapters.mcp.tools.never_tag import (
    _set_never_tag_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.slack._models import SlackMessageRow
from daimon.core.config import OutboundSettings
from daimon.core.outbound.mentions import Platform
from daimon.core.outbound.reach import ReachPlan
from daimon.core.stores import platform_names as names_store
from daimon.core.stores import tag_opt_outs as store
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import registered_account_token
from .test_channels_dispatch import _call_tool, _make_app  # pyright: ignore[reportPrivateUsage]

PLATFORMS: tuple[Platform, ...] = ("discord", "slack", "teams")
TAG = {"discord": "<@{}>", "slack": "<@U{}>", "teams": "<at>{}</at>"}


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    return cast(
        McpRuntime,
        SimpleNamespace(
            session_factory=sessionmaker,
            settings=SimpleNamespace(outbound=OutboundSettings()),
            teams_client=None,
        ),
    )


async def _person(
    sessionmaker: async_sessionmaker[AsyncSession], platform: Platform, user_id: str
) -> AuthIdentity:
    """A tenant with one person in it, as the identity a chat call runs as."""
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session, platform=platform, workspace_id=f"W_{platform}_nt")
        account = await make_account(session, tenant=tenant)
        await make_platform_principal(
            session, platform=platform, external_id=user_id, tenant=tenant, account=account
        )
    return AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=cast(Any, "member"),
        platform=platform,
        platform_user_id=user_id,
    )


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_set_never_tag_when_person_asks_then_stored_for_them_only_and_reversible(
    sessionmaker: async_sessionmaker[AsyncSession], platform: Platform
) -> None:
    """The tool acts on the caller's own account and can be undone."""
    auth = await _person(sessionmaker, platform, "5")
    runtime = _runtime(sessionmaker)

    on = await _set_never_tag_impl(runtime, auth, True)
    async with sessionmaker() as session:
        stored = await store.is_opted_out(session, account_id=auth.account_id)
    off = await _set_never_tag_impl(runtime, auth, False)
    async with sessionmaker() as session:
        cleared = not await store.is_opted_out(session, account_id=auth.account_id)

    assert on.never_tag and stored, f"{platform}: the choice is saved against the caller's account"
    assert not off.never_tag and cleared, f"{platform}: asking to be tagged again removes it"


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_set_never_tag_when_not_a_person_in_chat_then_refused_and_nothing_saved(
    sessionmaker: async_sessionmaker[AsyncSession], platform: Platform
) -> None:
    """A routine or an outside client has no person to speak for, so nothing is stored."""
    auth = await _person(sessionmaker, platform, "5")
    for caller in (
        _replace(auth, platform_user_id=None),
        _replace(auth, routine_id=uuid.uuid4()),
    ):
        with pytest.raises(ToolError, match="Only a person"):
            await _set_never_tag_impl(_runtime(sessionmaker), caller, True)
    async with sessionmaker() as session:
        saved = await store.is_opted_out(session, account_id=auth.account_id)
    assert not saved, f"{platform}: a refused call stores nothing"


def _replace(auth: AuthIdentity, **changes: Any) -> AuthIdentity:
    return dataclasses.replace(auth, **changes)


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_prepare_posts_when_person_opted_out_then_plain_name_never_pinged_or_counted(
    sessionmaker: async_sessionmaker[AsyncSession], platform: Platform
) -> None:
    """The stored choice reaches the gate: no ping, no people count, a readable name."""
    auth = await _person(sessionmaker, platform, "2" if platform == "discord" else "U2")
    async with sessionmaker.begin() as session:
        await store.set_opt_out(session, account_id=auth.account_id, tenant_id=auth.tenant_id)
        await names_store.upsert_user_names(
            session,
            tenant_id=auth.tenant_id,
            platform=platform,
            names={auth.platform_user_id or "": names_store.KnownName(display_name="Bo")},
        )
    other = TAG[platform].format(1)
    mine = TAG[platform].format(2)
    scope = TurnScope(
        key=f"turn:{uuid.uuid4()}", kind="unattended", own_places=frozenset(), requester_id="9"
    )

    [post] = await prepare_posts(
        _runtime(sessionmaker),
        auth,
        [OutgoingPost(text=f"{other} {mine} ready", place="C1")],
        plan=ReachPlan(threads=0, places=1, people=1),
        scope=scope,
    )

    assert post.user_ids == (
        () if platform == "teams" else ({"discord": "1", "slack": "U1"}[platform],)
    ), f"{platform}: the opted-out person is never pinged or counted"
    if platform != "teams":
        assert mine not in post.text, f"{platform}: the tag is gone"
        assert "@Bo" in post.text, f"{platform}: the person reads as a plain name"


async def test_prepare_posts_when_discord_requester_opted_out_then_credited_by_name(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The attribution line must not tag a person who asked never to be tagged."""
    auth = await _person(sessionmaker, "discord", "42")
    async with sessionmaker.begin() as session:
        await store.set_opt_out(session, account_id=auth.account_id, tenant_id=auth.tenant_id)
        await names_store.upsert_user_names(
            session,
            tenant_id=auth.tenant_id,
            platform="discord",
            names={"42": names_store.KnownName(display_name="Ann")},
        )
    scope = TurnScope(
        key=f"turn:{uuid.uuid4()}", kind="unattended", own_places=frozenset(), requester_id="42"
    )

    [post] = await prepare_posts(
        _runtime(sessionmaker),
        auth,
        [OutgoingPost(text="done", place="C1")],
        plan=None,
        scope=scope,
    )

    assert post.text.endswith("-# requested by Ann"), "named, not tagged"
    assert "<@42>" not in post.text, "no mention of the opted-out requester"


async def test_requester_name_when_slack_asked_twice_then_looked_up_once(
    sessionmaker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Attribution names the same person on many posts; Slack is asked once."""
    calls: list[str] = []

    class Client:
        async def users_info(self, *, user: str) -> dict[str, Any]:
            calls.append(user)
            return {"user": {"real_name": "Ann Example", "profile": {"display_name": ""}}}

    async def web_client(*_a: object, **_k: object) -> Client:
        return Client()

    monkeypatch.setattr("daimon.adapters.mcp.tools.slack._client.slack_web_client", web_client)
    _outbound._slack_names.clear()  # pyright: ignore[reportPrivateUsage]
    auth = await _person(sessionmaker, "slack", "U7")
    auth = _replace(auth, external_id="T_CACHE")
    runtime = _runtime(sessionmaker)

    first = await _outbound._requester_name(runtime, auth, "C1", "U7")  # pyright: ignore[reportPrivateUsage]
    second = await _outbound._requester_name(runtime, auth, "C2", "U7")  # pyright: ignore[reportPrivateUsage]

    assert (first, second) == ("Ann Example", "Ann Example"), "same name both times"
    assert calls == ["U7"], "one users.info call serves both posts"


async def test_send_message_when_batch_then_every_message_goes_out_and_counts_together(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """send_message takes the same batch form as create_thread: one result per message."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="slack-batch-send")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform="slack", external_id="U_CALLER", tenant=tenant, account=account
    )
    await db_session.commit()
    sent: list[tuple[str, str, Any]] = []

    async def send_one(
        _runtime: object, _auth: object, *, channel_id: str, content: str, files: Any
    ) -> SlackMessageRow:
        sent.append((channel_id, content, files))
        return SlackMessageRow(ts=f"{len(sent)}.0", text=content)

    monkeypatch.setattr(channels, "_send_one", send_one)
    token = await registered_account_token(sessionmaker, account_id=account.id, secret=b"a" * 32)

    ok = await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="send_message",
        arguments={
            "channel_id": "C1",
            "content": "one",
            "plan": {"threads": 0, "places": 2, "people": 0},
            "batch": [{"channel_id": "C2", "content": "two"}],
        },
    )
    refused = await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="send_message",
        arguments={
            "channel_id": "C1",
            "content": "one",
            "plan": {"threads": 0, "places": 1, "people": 0},
            "batch": [
                {"channel_id": "C2", "content": "two"},
                {"channel_id": "C3", "content": "three"},
            ],
        },
    )

    assert [(c, t) for c, t, _ in sent] == [("C1", "one"), ("C2", "two")], "each to its own place"
    assert not ok.get("isError"), f"a batch within the plan is sent; got {ok!r}"
    assert refused.get("isError") is True, "three places past a plan of one are refused whole"
    assert len(sent) == 2, "a refused batch posts nothing"


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_refuse_dm_to_opted_out_when_someone_else_asks_then_refused_but_self_allowed(
    sessionmaker: async_sessionmaker[AsyncSession], platform: Platform
) -> None:
    """A DM is a notification: not sent for another person, still fine for oneself."""
    person = await _person(sessionmaker, platform, "7")
    async with sessionmaker.begin() as session:
        await store.set_opt_out(session, account_id=person.account_id, tenant_id=person.tenant_id)
    runtime = _runtime(sessionmaker)
    stranger = _replace(person, platform_user_id="8")

    await _outbound.refuse_dm_to_opted_out(runtime, person, "7")
    with pytest.raises(ToolError, match="asked not to be tagged"):
        await _outbound.refuse_dm_to_opted_out(runtime, stranger, "7")
