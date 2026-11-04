"""Outbound etiquette on Discord, Slack and Teams, with each deliberate asymmetry.

- Teams tool posts carry no mention entities, so nothing in them pings and no
  person counts toward the people budget there.
- Discord names the requester as ``<@id>`` (sends allow only tagged people to
  ping); Slack and Teams have no silent mention, so they use the name.
- Discord role tags stay as text: every send turns role pings off. Slack and
  Teams group pings become plain words.
- A turn's own conversation is its thread and its direct chat; the parent channel is
  other people's on every platform, so a top-level post there is counted.
- When the never-tag lookup fails, the post still goes out: Discord keeps the
  mention text and pings nobody; Slack names everyone unchecked, as its replies
  do; Teams posts tag no one either way.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import _outbound
from daimon.adapters.mcp.tools._outbound import OutgoingPost, TurnScope, prepare_posts
from daimon.core.config import OutboundSettings
from daimon.core.outbound.mentions import (
    attribution_line,
    mentioned_user_ids,
    neutralise_broadcasts,
    ping_users,
    render_post,
)
from daimon.core.outbound.opt_out import tag_opt_outs, untag_opted_out
from daimon.core.outbound.reach import TOO_LONG
from daimon.core.stores import tag_opt_outs as tag_opt_outs_store
from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from fastmcp.exceptions import ToolError

PLATFORMS = ("discord", "slack", "teams")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_broadcast_when_any_platform_then_never_live(platform):
    live = {"discord": "@everyone", "slack": "<!channel>", "teams": "<at>Everyone</at>"}
    out = neutralise_broadcasts(f"hi {live[platform]}", platform)
    assert live[platform] not in out, f"{platform}: a broadcast never posts live"


def test_people_count_when_teams_then_zero_because_nothing_pings():
    counts = {
        "discord": mentioned_user_ids("<@1>", "discord"),
        "slack": mentioned_user_ids("<@U1>", "slack"),
        "teams": mentioned_user_ids("<at>Ann</at>", "teams"),
    }
    assert counts == {"discord": ("1",), "slack": ("U1",), "teams": ()}, "Teams counts nobody"


def test_attribution_when_requester_known_then_discord_id_else_name():
    lines = {
        p: attribution_line(p, requester_id="42" if p == "discord" else None, requester_name="Ann")
        for p in PLATFORMS
    }
    assert lines == {
        "discord": "-# requested by <@42>",
        "slack": "requested by Ann",
        "teams": "requested by Ann",
    }, "only Discord has a mention that can be sent without a ping"


def test_discord_role_tag_when_posted_then_kept_as_text():
    assert render_post("<@&9> ready", "discord").text == "<@&9> ready", "role pings are off"


_IDS = {"discord": "123", "slack": "U123", "teams": "aad-123"}


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_never_tag_when_person_opted_out_then_every_platform_skips_them(
    platform, db_session, db_session_factory
):
    tenant = await make_tenant(db_session, platform=platform, workspace_id="W_NT_1")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform=platform, external_id=_IDS[platform], tenant=tenant, account=account
    )
    await tag_opt_outs_store.set_opt_out(db_session, account_id=account.id, tenant_id=tenant.id)
    await db_session.commit()

    skipped = await tag_opt_outs(
        db_session_factory,
        tenant_id=tenant.id,
        platform=platform,
        user_ids=[_IDS[platform], "someone-else"],
    )

    assert skipped == {_IDS[platform]}, f"{platform}: only the opted-out person is skipped"


@pytest.mark.parametrize("platform", ("discord", "slack"))
async def test_never_tag_when_text_mentions_opted_out_then_plain_name(
    platform, db_session, db_session_factory
):
    tenant = await make_tenant(db_session, platform=platform, workspace_id="W_NT_1")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform=platform, external_id=_IDS[platform], tenant=tenant, account=account
    )
    await tag_opt_outs_store.set_opt_out(db_session, account_id=account.id, tenant_id=tenant.id)
    await db_session.commit()
    other = {"discord": "<@456>", "slack": "<@U456>"}[platform]

    out = await untag_opted_out(
        db_session_factory,
        tenant_id=tenant.id,
        platform=platform,
        text=f"{render_post_mention(platform)} and {other}",
    )

    assert "123" not in out.replace("456", ""), f"{platform}: the opted-out id is gone"
    assert other in out, f"{platform}: others are still tagged"


def render_post_mention(platform):
    return {"discord": "<@123>", "slack": "<@U123>"}[platform]


_TAG = {"discord": "<@7>", "slack": "<@U7>", "teams": "<at>Ann</at>"}
_ON_FAILURE = {"discord": "<@7> look", "slack": "@someone look", "teams": "@Ann look"}


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_opt_out_lookup_when_it_fails_then_post_sends_without_pings(platform, monkeypatch):
    async def broken(*_: object, **__: object) -> frozenset[str]:
        raise RuntimeError("lookup down")

    monkeypatch.setattr(_outbound, "tag_opt_outs", broken)
    runtime = SimpleNamespace(
        session_factory=None, settings=SimpleNamespace(outbound=OutboundSettings())
    )
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=cast(Any, "member"), platform=platform
    )
    scope = TurnScope(key="turn:x", kind="live", own_places=frozenset({"home"}), requester_id="42")
    [post] = await prepare_posts(
        cast(Any, runtime),
        auth,
        [OutgoingPost(text=f"{_TAG[platform]} look", place="home")],
        plan=None,
        scope=scope,
    )
    assert post.text == _ON_FAILURE[platform], f"{platform}: the post still goes out"
    assert ping_users.get() == frozenset(), f"{platform}: and pings nobody unchecked"


_THREAD_TURN = {
    "discord": ("C1", "T9", "C1", "T9", "C1"),
    "slack": ("C1", "T9", "C1", "C1:T9", "C1"),
    "teams": ("C1", "C1;messageid=9", "C1;messageid=9", "C1;MessageID=9", "C1"),
}


@pytest.mark.parametrize("platform", PLATFORMS)
async def test_thread_turn_when_posting_top_level_then_reach_on_every_platform(
    platform, db_session, db_session_factory
):
    """A reply in the own thread is the conversation; the channel around it is other people."""
    parent, thread, channel, reply, top_level = _THREAD_TURN[platform]
    tenant = await make_tenant(db_session, platform=platform, workspace_id="W_NT_2")
    await db_session.commit()
    now = datetime.now(UTC)
    origin = TurnOriginRow(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        account_id=uuid.uuid4(),
        platform=platform,
        parent_channel_id=parent,
        thread_id=thread,
        platform_channel_id=channel,
        responder_ma_agent_id="agent_x",
        responder_name="daimon",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        created_at=now,
        expires_at=now,
    )
    places = _outbound._own_places(origin)
    scope = TurnScope(
        key=f"turn:{uuid.uuid4()}",
        kind="live",
        own_places=places,
        requester_id="42",
        origin=origin,
        own_channels=places | {parent},
    )
    runtime = SimpleNamespace(
        session_factory=db_session_factory,
        settings=SimpleNamespace(outbound=OutboundSettings()),
        teams_client=None,
    )
    auth = AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=tenant.id, role=cast(Any, "member"), platform=platform
    )
    long_text = "x" * 5000

    [own] = await prepare_posts(
        cast(Any, runtime),
        auth,
        [OutgoingPost(text=long_text, place=reply)],
        plan=None,
        scope=scope,
    )
    assert own.text == long_text, f"{platform}: a reply in the own thread has no limit or line"
    with pytest.raises(ToolError, match=TOO_LONG):
        await prepare_posts(
            cast(Any, runtime),
            auth,
            [OutgoingPost(text=long_text, place=top_level)],
            plan=None,
            scope=scope,
        )
