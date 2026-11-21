"""/share-secret keeps no share without a posted card, Reveal shows the value only once,
and a held share is sent only by its sender."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from daimon.adapters.discord.commands import share_secret as cmd
from daimon.core import secret_share_flow as flow
from daimon.core import secret_shares as words
from daimon.core.stores import secret_shares as store
from daimon.core.stores.secret_shares import Reveal, ShareView

SHARE = ShareView(uuid.uuid4(), "1", "2", "Prod DB", "9", "8", datetime.now(UTC))
TENANT = uuid.uuid4()


class _Tx:
    """A session whose transaction records how it ended."""

    def __init__(self) -> None:
        self.failed: list[BaseException | None] = []

    def __call__(self) -> _Tx:
        return self

    def begin(self) -> _Tx:
        return self

    async def __aenter__(self) -> _Tx:
        return self

    async def __aexit__(self, kind: Any, exc: BaseException | None, tb: Any) -> bool:
        self.failed.append(exc)
        return False


def _runtime(tx: _Tx) -> Any:
    return MagicMock(sessionmaker=tx)


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flow, "sharing_refusal", AsyncMock(return_value=None))


async def _share(tx: _Tx, recipient: Any) -> tuple[str, bool]:
    return await cmd.share(
        _runtime(tx), uuid.uuid4(), sender="1", recipient=recipient, label="Prod DB", value="s3cret"
    )


@pytest.mark.usefixtures("on")
async def test_a_closed_dm_deletes_the_share(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "create_share", AsyncMock(return_value=SHARE))
    set_card, delete = AsyncMock(), AsyncMock()
    monkeypatch.setattr(store, "set_share_card", set_card)
    monkeypatch.setattr(store, "delete_share", delete)
    forbidden = discord.Forbidden(MagicMock(status=403, reason="x"), "closed")
    recipient = NS(id=2, send=AsyncMock(side_effect=forbidden))
    tx = _Tx()
    assert await _share(tx, recipient) == (words.dm_closed_text("2"), False)
    assert tx.failed[0] is None, "the share was committed before the send"
    assert delete.await_args is not None and delete.await_args.args[1] == SHARE.id
    set_card.assert_not_awaited()


@pytest.mark.usefixtures("on")
async def test_the_card_names_the_secret_but_never_holds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store, "create_share", AsyncMock(return_value=SHARE))
    set_card = AsyncMock()
    monkeypatch.setattr(store, "set_share_card", set_card)
    recipient = NS(id=2, send=AsyncMock(return_value=NS(id=8, channel=NS(id=9))))
    text, ok = await _share(_Tx(), recipient)
    assert ok and text == words.shared_text(recipient="2", label="Prod DB")
    embed = recipient.send.await_args.kwargs["embed"]
    assert "Prod DB" in embed.description and "s3cret" not in str(embed.to_dict())
    assert set_card.await_args is not None and set_card.await_args.args[1] == SHARE.id
    assert set_card.await_args.kwargs == {"channel_id": "9", "message_id": "8"}


async def test_sharing_off_stores_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flow, "sharing_refusal", AsyncMock(return_value=words.OFF))
    create = AsyncMock()
    monkeypatch.setattr(store, "create_share", create)
    assert await _share(_Tx(), NS(id=2)) == (words.OFF, False)
    create.assert_not_awaited()


def _click(user_id: int) -> Any:
    interaction = MagicMock()
    interaction.client.runtime = _runtime(_Tx())
    interaction.user.id = user_id
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.message.edit = AsyncMock()
    return interaction


def _reply(interaction: Any) -> dict[str, Any]:
    return interaction.followup.send.await_args.kwargs


@pytest.fixture
def found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "get_share", AsyncMock(return_value=SHARE))


@pytest.mark.usefixtures("on", "found")
async def test_reveal_shows_the_value_privately_and_leaves_only_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store, "reveal_share", AsyncMock(return_value=Reveal(SHARE, "s3cret")))
    interaction = _click(2)
    await cmd.reveal(interaction, TENANT, SHARE.id)
    reply = _reply(interaction)
    assert reply["ephemeral"] is True and "s3cret" in reply["embed"].description
    card = interaction.message.edit.await_args.kwargs
    assert card["view"] is None and "s3cret" not in card["embed"].description
    assert "was revealed by <@2>" in card["embed"].description


@pytest.mark.usefixtures("on", "found")
async def test_anyone_else_is_refused_and_the_card_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    refused = Reveal(SHARE, refusal="not_recipient")
    monkeypatch.setattr(store, "reveal_share", AsyncMock(return_value=refused))
    interaction = _click(3)
    await cmd.reveal(interaction, TENANT, SHARE.id)
    assert _reply(interaction)["embed"].description == words.REVEAL_REFUSALS["not_recipient"]
    interaction.message.edit.assert_not_awaited()


@pytest.mark.usefixtures("found")
async def test_a_reveal_rechecks_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flow, "sharing_refusal", AsyncMock(return_value=words.DMS_OFF))
    reveal = AsyncMock()
    monkeypatch.setattr(store, "reveal_share", reveal)
    interaction = _click(2)
    await cmd.reveal(interaction, TENANT, SHARE.id)
    assert _reply(interaction)["embed"].description == words.DMS_OFF
    reveal.assert_not_awaited()


async def test_the_click_defers_before_anything_reads_the_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    interaction = _click(2)
    interaction.response.defer = AsyncMock(side_effect=lambda **_: order.append("defer"))
    monkeypatch.setattr(cmd, "reveal", AsyncMock(side_effect=lambda *_: order.append("reveal")))
    button = cmd.SecretShareRevealButton(tenant_id=str(TENANT), share_id=str(SHARE.id))
    await button.callback(interaction)
    assert order == ["defer", "reveal"]
    assert interaction.response.defer.await_args.kwargs["ephemeral"] is True


@pytest.mark.usefixtures("on", "found")
async def test_only_the_sender_sends_a_held_share(monkeypatch: pytest.MonkeyPatch) -> None:
    confirm = AsyncMock(return_value=Reveal(SHARE))
    monkeypatch.setattr(store, "confirm_share", confirm)
    post = AsyncMock()
    monkeypatch.setattr(flow, "post_card", post)
    interaction = _click(1)
    await cmd.confirm(interaction, TENANT, SHARE.id)
    assert confirm.await_args is not None and confirm.await_args.kwargs["sender"] == "1"
    post.assert_awaited_once()
    assert "<@2>" in _reply(interaction)["embed"].description

    confirm.return_value = Reveal(SHARE, refusal="not_sender")
    post.reset_mock()
    other = _click(3)
    await cmd.confirm(other, TENANT, SHARE.id)
    assert _reply(other)["embed"].description == words.REVEAL_REFUSALS["not_sender"]
    post.assert_not_awaited()
