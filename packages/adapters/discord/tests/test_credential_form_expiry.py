"""Acknowledged expiry edits and opt-in lifecycle; real Discord serialization offline."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.post_transport import DiscordPostTransport
from daimon.adapters.discord.posted_controls import build_card_view, edit_expired_posted_card
from daimon.core.credential_form_expiry import poll_credential_form_expiries_once
from daimon.core.credential_requests import mint_request_token
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.posted_controls import card_for_request
from daimon.core.stores import credential_requests as store
from daimon.core.stores.domain import CredentialRequestRow
from daimon.testing.discord_surface import DiscordSurfaceHarness
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

_NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


def pending(**overrides: Any) -> CredentialRequestRow:
    fields = dict(
        token=mint_request_token(),
        kind="env",
        tenant_id=derive_tenant_uuid(platform="discord", workspace_id="100"),
        account_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        target="ACME_API_KEY",
        mcp_server_url=None,
        requester_platform_user_id="103",
        channel_id="101",
        platform="discord",
        parent_channel_id="101",
        origin_thread_id="102",
        posted_message_id="444",
        idempotency_key=uuid.uuid4(),
        created_at=_NOW - timedelta(minutes=30),
        expires_at=_NOW,
        used_at=_NOW,
        outcome="expired_card_pending",
        target_name="Daimon",
        responder_name="Daimon",
    )
    fields.update(overrides)
    return CredentialRequestRow(**fields)


def client_card():
    client = MagicMock(spec=discord.Client)
    client.user = MagicMock(spec=discord.ClientUser)
    client.user.id = 104
    client.application_id = 7
    channel = MagicMock(spec=discord.Thread)
    channel.id = 102
    channel.parent_id = 101
    channel.guild.id = 100
    channel.parent = MagicMock(spec=discord.TextChannel)
    channel.parent.id = 101
    client.get_channel.return_value = channel
    client.fetch_channel = AsyncMock(return_value=channel)
    message = MagicMock(spec=discord.Message)
    message.id = 444
    message.channel = channel
    message.author.id = 104
    message.author.name = "Daimon"
    message.webhook_id = None
    channel.get_partial_message.return_value.fetch = AsyncMock(return_value=message)
    message.edit = AsyncMock(return_value=message)
    return client, channel, message


async def test_expiry_ack_is_the_original_card_and_never_mentions_or_has_buttons() -> None:
    client, channel, message = client_card()
    assert await edit_expired_posted_card(client, row=pending()) is True
    channel.get_partial_message.assert_called_once_with(444)
    message.edit.assert_awaited_once()
    kwargs = message.edit.call_args.kwargs
    assert kwargs["allowed_mentions"].users is False
    assert kwargs["allowed_mentions"].roles is False
    assert kwargs["allowed_mentions"].everyone is False
    view = kwargs["view"]
    assert isinstance(view, discord.ui.LayoutView)
    assert not any(isinstance(item, discord.ui.Button) for item in view.walk_children())
    text = "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    assert "expired" in text and "ACME_API_KEY" in text and "Ask Daimon" in text
    channel.send.assert_not_called()


@pytest.mark.parametrize(
    "change", ["foreign_tenant", "parent", "channel", "message", "author", "none_ack", "wrong_ack"]
)
async def test_wrong_identity_or_missing_ack_is_never_success(change) -> None:
    client, channel, message = client_card()
    row = pending()
    if change == "foreign_tenant":
        row = pending(tenant_id=uuid.uuid4())
    if change == "parent":
        row = pending(parent_channel_id="999")
    if change == "channel":
        channel.id = 999
    if change == "message":
        message.id = 999
    if change == "author":
        message.author.id = 999
    if change == "none_ack":
        message.edit.return_value = None
    if change == "wrong_ack":
        replacement = MagicMock(spec=discord.Message)
        replacement.id = 999
        replacement.channel = channel
        message.edit.return_value = replacement
    assert await edit_expired_posted_card(client, row=row) is False
    channel.send.assert_not_called()
    if change not in ("none_ack", "wrong_ack"):
        message.edit.assert_not_awaited()


@pytest.mark.parametrize(
    "fields",
    [
        {"platform": "slack"},
        {"origin_thread_id": None},
        {"posted_message_id": None},
        {"origin_thread_id": "bad-id"},
        {"used_at": None},
        {"outcome": "applied"},
        {"used_at": _NOW - timedelta(seconds=1)},
    ],
)
async def test_unretired_or_incomplete_rows_do_not_touch_discord(fields) -> None:
    client, _, _ = client_card()
    assert await edit_expired_posted_card(client, row=pending(**fields)) is False
    client.get_channel.assert_not_called()
    client.fetch_channel.assert_not_awaited()


@pytest.mark.parametrize("failure", ["permission", "deleted", "archived", "lost_ack"])
async def test_failed_card_edit_never_acknowledges_or_creates_a_post(failure) -> None:
    client, channel, message = client_card()
    response = MagicMock()
    response.status = 403 if failure in ("permission", "archived") else 404
    message.edit.side_effect = (
        OSError("lost ack") if failure == "lost_ack" else discord.HTTPException(response, "refused")
    )
    assert await edit_expired_posted_card(client, row=pending()) is False
    channel.send.assert_not_called()


@pytest.mark.parametrize("available", [False, True])
async def test_webhook_expiry_uses_existing_transport_without_replacement(
    monkeypatch, available
) -> None:
    client, channel, message = client_card()
    message.webhook_id = 500
    message.application_id = client.application_id
    hook = MagicMock(spec=discord.Webhook)
    hook.edit_message = AsyncMock(return_value=message)
    monkeypatch.setattr(
        DiscordPostTransport, "_webhook", AsyncMock(return_value=hook if available else None)
    )
    send = AsyncMock(side_effect=AssertionError("must never replace a credential card"))
    monkeypatch.setattr(DiscordPostTransport, "send", send)
    assert await edit_expired_posted_card(client, row=pending()) is available
    send.assert_not_awaited()
    if available:
        hook.edit_message.assert_awaited_once()
        assert hook.edit_message.call_args.args == (444,)
        assert hook.edit_message.call_args.kwargs["thread"] is channel
        assert hook.edit_message.call_args.kwargs["allowed_mentions"].users is False
    else:
        hook.edit_message.assert_not_awaited()


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_real_persisted_unclicked_form_is_edited_after_30_minutes(
    db_engine: AsyncEngine, db_clean: None, monkeypatch, lost_ack
) -> None:
    clock = [0.0]
    h = DiscordSurfaceHarness(
        evidence_id="expiry:component",
        session_id="component",
        root_turn_id="component",
        clock=lambda: clock[0],
    )
    # The accepting offline HTTP gateway posts as104; authenticate that bot in the client cache.
    state = h.client._connection  # pyright: ignore[reportPrivateUsage]
    state.user = discord.ClientUser(
        state=state,
        data={
            "id": "104",
            "username": "Daimon",
            "discriminator": "0000",
            "avatar": None,
            "bot": True,
        },
    )
    # The existing gateway omits Discord's required author.avatar member. Repair
    # only that fixture metadata on fetch; card content/events stay untouched.
    original_fetch = h.gateway.get_message

    async def fetch(channel_id, message_id):
        payload = await original_fetch(channel_id, message_id)
        payload["author"]["avatar"] = None
        return payload

    monkeypatch.setattr(h.gateway, "get_message", fetch)
    original_patch = h.gateway.edit_message
    lost = False

    async def patch(channel_id, message_id, *, params):
        nonlocal lost
        payload = await original_patch(channel_id, message_id, params=params)
        if lost_ack and not lost:
            lost = True
            raise OSError("offline acknowledgement lost after accepted PATCH")
        return payload

    monkeypatch.setattr(h.gateway, "edit_message", patch)
    sm = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sm.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="100")
        account = await make_account(session, tenant=tenant)
        row = await store.create_credential_request(
            session,
            token=mint_request_token(),
            kind="env",
            tenant_id=tenant.id,
            account_id=account.id,
            agent_id=uuid.uuid4(),
            target="ACME_API_KEY",
            mcp_server_url=None,
            requester_platform_user_id="103",
            channel_id="101",
            platform="discord",
            parent_channel_id="101",
            origin_thread_id="102",
            idempotency_key=uuid.uuid4(),
            target_ma_agent_id="ag_test",
            target_name="Daimon",
            responder_name="Daimon",
            requested_work="read the project",
            expires_at=_NOW + timedelta(minutes=30),
        )
    message = await h.thread.send(view=build_card_view(card_for_request(row, state="requested")))
    async with sm.begin() as session:
        await store.update_credential_request_message(
            session, token=row.token, posted_message_id=str(message.id)
        )

    async def edit(retired: CredentialRequestRow) -> bool:
        return await edit_expired_posted_card(h.client, row=retired)

    clock[0] = 1799
    before = await poll_credential_form_expiries_once(
        sm, now=_NOW + timedelta(seconds=clock[0]), tenant_id=tenant.id, edit=edit
    )
    assert before.attempted == 0
    assert len(h.gateway.events) == 1 and h.gateway.events[0].message.component_labels
    clock[0] = 1920
    receipt = await poll_credential_form_expiries_once(
        sm, now=_NOW + timedelta(seconds=clock[0]), tenant_id=tenant.id, edit=edit
    )
    assert (receipt.retired, receipt.attempted, receipt.acknowledged) == (1, 1, int(not lost_ack))
    assert [event.operation for event in h.gateway.events] == ["send", "edit"]
    final = h.gateway.surviving_messages()[0]
    assert final.message_id == str(message.id) and final.observed_s == 1920
    assert "expired" in final.payload_json and "Ask Daimon" in final.payload_json
    assert final.component_labels == () and "received. saving" not in final.payload_json.lower()
    assert json.loads(h.gateway.events[-1].request_json)["allowed_mentions"] == {"parse": []}
    async with sm() as session:
        after = await store.peek_credential_request(session, token=row.token)
        assert after is not None
        assert after.outcome == ("expired_card_pending" if lost_ack else "expired_card_updated")
        assert after.requested_work == "read the project" and after.expires_at == row.expires_at
    # A fresh worker/sessionmaker retries the same idempotent PATCH after a lost
    # acknowledgement, never a replacement post or credential submission.
    restarted = async_sessionmaker(db_engine, expire_on_commit=False)
    again = await poll_credential_form_expiries_once(
        restarted, now=_NOW + timedelta(seconds=1920), tenant_id=tenant.id, edit=edit
    )
    assert again.retired == 0
    assert again.attempted == again.acknowledged == int(lost_ack)
    assert [event.operation for event in h.gateway.events] == (
        ["send", "edit", "edit"] if lost_ack else ["send", "edit"]
    )
    assert len(h.gateway.surviving_messages()) == 1
    async with restarted() as session:
        after = await store.peek_credential_request(session, token=row.token)
        assert after is not None and after.outcome == "expired_card_updated"
