"""The fleet login question's Discord card: view, durable buttons, click, poller (PR B)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import discord
import pytest
from daimon.adapters.discord.posted_controls import fleet_login
from daimon.adapters.discord.posted_controls.fleet_login import (
    LoginButton,
    build_login_view,
    login_custom_id,
    poll_login_cards_once,
)
from daimon.core.fleet_login_decision import REFUSAL_TEXT, LoginAnswer, LoginCard, LoginCardRow
from daimon.core.ma_identity import derive_tenant_uuid

NOW = datetime(2026, 10, 23, 14, tzinfo=UTC)
TENANT = derive_tenant_uuid(platform="discord", workspace_id="60")
RUN = UUID(int=42)
DIGEST = "ab" * 32


def card(**changes: object) -> LoginCard:
    base = LoginCard(
        tenant_id=TENANT,
        run_id=RUN,
        platform="discord",
        channel_id="70",
        thread_id="71",
        requester_platform_user_id="9",
        rows=(
            LoginCardRow(index=0, label="GITHUB_TOKEN", choice=None),
            LoginCardRow(index=1, label="Linear", choice=None),
        ),
        without=("Notion: can't be shared with workers yet",),
        expires_at=NOW + timedelta(seconds=60),
        digest=DIGEST,
        state="open",
        card_message_id=None,
        card_settled=False,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def _texts(view: discord.ui.LayoutView) -> str:
    return "\n".join(
        item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )


def _buttons(view: discord.ui.LayoutView) -> list[discord.ui.Button[discord.ui.LayoutView]]:
    return [item for item in view.walk_children() if isinstance(item, discord.ui.Button)]


def test_an_open_card_has_three_buttons_per_login_and_skip_all():
    view = build_login_view(card())
    buttons = _buttons(view)
    assert [b.label for b in buttons] == [
        *("Share", "Share this time", "Skip"),
        *("Share", "Share this time", "Skip"),
        "Skip all",
    ]
    assert buttons[0].custom_id == f"flogin:{RUN.hex}:0:s:{DIGEST[:12]}"
    assert buttons[4].custom_id == f"flogin:{RUN.hex}:1:o:{DIGEST[:12]}"
    assert buttons[-1].custom_id == f"flogin:{RUN.hex}:all:k:{DIGEST[:12]}"
    assert all(len(b.custom_id or "") <= 100 for b in buttons)
    text = _texts(view)
    assert "GITHUB_TOKEN" in text and "Linear" in text
    assert "Workers run without: Notion: can't be shared with workers yet" in text
    assert f"<t:{int((NOW + timedelta(seconds=60)).timestamp())}:T>" in text
    assert "Only <@9> can answer." in text


def test_an_answered_row_shows_its_answer_and_no_buttons_and_skip_all_needs_two_left():
    view = build_login_view(
        card(rows=(LoginCardRow(0, "GITHUB_TOKEN", "share"), LoginCardRow(1, "Linear", None)))
    )
    assert [b.label for b in _buttons(view)] == ["Share", "Share this time", "Skip"]
    assert "GITHUB_TOKEN: shared" in _texts(view)


@pytest.mark.parametrize("state", ["decided", "expired", "run_ended"])
def test_a_final_card_has_no_buttons_only_its_final_words(state):
    rows = (LoginCardRow(0, "GITHUB_TOKEN", "share"), LoginCardRow(1, "Linear", "no_answer"))
    view = build_login_view(card(state=state, rows=rows))
    assert _buttons(view) == []
    text = _texts(view)
    assert "Shared with workers: GITHUB_TOKEN" in text
    assert "Linear" in text


@pytest.mark.parametrize(
    ("row", "choice", "letter"),
    [(0, "share", "s"), (3, "share_once", "o"), (1, "skip", "k"), ("all", "skip", "k")],
)
def test_a_button_is_rebuilt_from_its_custom_id(row, choice, letter):
    custom_id = login_custom_id(RUN, row, choice, DIGEST)
    assert custom_id == f"flogin:{RUN.hex}:{row}:{letter}:{DIGEST[:12]}"
    item = LoginButton(discord.ui.Button(custom_id=custom_id))
    assert (item.run_id, item.login_row, item.choice, item.digest) == (
        RUN,
        row,
        choice,
        DIGEST[:12],
    )


@pytest.mark.parametrize(
    "custom_id",
    [
        f"flogin:{RUN.hex}:0:x:{DIGEST[:12]}",
        f"flogin:{RUN.hex}:-1:s:{DIGEST[:12]}",
        f"flogin:{RUN.hex}:0:s:{DIGEST[:11]}",
        f"fleet:{RUN.hex}:0:s:{DIGEST[:12]}",
    ],
)
def test_a_malformed_custom_id_is_refused(custom_id):
    with pytest.raises(ValueError):
        LoginButton(discord.ui.Button(custom_id=custom_id))


def _interaction(message_id: int = 500):
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = SimpleNamespace(id=60)
    interaction.user = SimpleNamespace(id=9)
    interaction.message = SimpleNamespace(id=message_id, edit=AsyncMock())
    interaction.response = MagicMock(defer=AsyncMock(), send_message=AsyncMock())
    interaction.followup = MagicMock(send=AsyncMock())
    interaction.client = SimpleNamespace(
        runtime=SimpleNamespace(turn_deps=object(), sessionmaker=object()),
        login_click_limiter=object(),
    )
    return interaction


async def _click(monkeypatch, answer: LoginAnswer, *, custom_id: str | None = None):
    from daimon.core import fleet_login_decision

    calls = AsyncMock(return_value=answer)
    settle = AsyncMock(return_value=True)
    monkeypatch.setattr(fleet_login_decision, "answer_login_click", calls)
    monkeypatch.setattr(fleet_login_decision, "settle_login_card", settle)
    interaction = _interaction()
    item = LoginButton(
        discord.ui.Button(custom_id=custom_id or login_custom_id(RUN, 0, "share", DIGEST))
    )
    await item.callback(interaction)
    return interaction, calls, settle


async def test_a_click_calls_core_once_as_the_clicker_and_redraws_from_the_db(monkeypatch):
    answered = card(
        card_message_id="500",
        rows=(LoginCardRow(0, "GITHUB_TOKEN", "share"), LoginCardRow(1, "Linear", None)),
    )
    interaction, calls, settle = await _click(monkeypatch, LoginAnswer(answered))
    kwargs = calls.await_args.kwargs
    assert kwargs["tenant_id"] == TENANT and kwargs["platform"] == "discord"
    assert (kwargs["run_id"], kwargs["row"], kwargs["choice"]) == (RUN, 0, "share")
    assert kwargs["digest"] == DIGEST[:12] and kwargs["clicker_platform_user_id"] == "9"
    assert callable(kwargs["live_admin"])
    view = interaction.message.edit.await_args.kwargs["view"]
    assert "GITHUB_TOKEN: shared" in _texts(view)
    interaction.followup.send.assert_not_awaited()
    settle.assert_not_awaited()


async def test_a_deciding_click_settles_the_card(monkeypatch):
    decided = card(
        card_message_id="500",
        state="decided",
        rows=(LoginCardRow(0, "GITHUB_TOKEN", "skip"), LoginCardRow(1, "Linear", "skip")),
    )
    interaction, _, settle = await _click(
        monkeypatch,
        LoginAnswer(decided, decided=True),
        custom_id=login_custom_id(RUN, "all", "skip", DIGEST),
    )
    assert _buttons(interaction.message.edit.await_args.kwargs["view"]) == []
    settle.assert_awaited_once()


@pytest.mark.parametrize("refusal", sorted(REFUSAL_TEXT))
async def test_a_refused_click_says_why_privately_in_plain_words(monkeypatch, refusal):
    interaction, _, _ = await _click(monkeypatch, LoginAnswer(None, refusal))
    interaction.followup.send.assert_awaited_once_with(REFUSAL_TEXT[refusal], ephemeral=True)
    interaction.message.edit.assert_not_awaited()


async def test_a_click_on_an_old_copy_never_redraws_it_as_the_live_card(monkeypatch):
    interaction, _, _ = await _click(monkeypatch, LoginAnswer(card(card_message_id="999")))
    interaction.message.edit.assert_not_awaited()


class _Core:
    """Stand-ins for the core poller calls, recording what the poller asked for."""

    def __init__(self, monkeypatch, cards, *, claimed=True, expired=None):
        from daimon.core import fleet_login_decision
        from daimon.core.stores import fleet

        self.settled: list[UUID] = []
        self.expire = AsyncMock(return_value=expired)
        self.record = AsyncMock(return_value=claimed)

        async def settle(_sm, *, tenant_id, run_id):
            self.settled.append(run_id)
            return True

        monkeypatch.setattr(fleet_login_decision, "login_cards", AsyncMock(return_value=cards))
        monkeypatch.setattr(fleet_login_decision, "expire_question", self.expire)
        monkeypatch.setattr(fleet_login_decision, "settle_login_card", settle)
        monkeypatch.setattr(fleet, "record_login_card", self.record)


async def _poll(post=None, fetch=None, now=NOW):
    post = post or AsyncMock(return_value=SimpleNamespace(id=500, delete=AsyncMock()))
    fetch = fetch or AsyncMock(return_value=MagicMock(edit=AsyncMock()))
    await poll_login_cards_once(object(), post=post, fetch=fetch, now=lambda: now)  # type: ignore[arg-type]
    return post, fetch


async def test_the_poller_posts_an_open_card_once_and_claims_it(monkeypatch):
    core = _Core(monkeypatch, (card(),))
    post, _ = await _poll()
    post.assert_awaited_once()
    assert core.record.await_args.kwargs["message_id"] == "500"
    post.return_value.delete.assert_not_awaited()
    assert core.settled == []


async def test_a_poster_that_loses_the_claim_deletes_its_copy(monkeypatch):
    _Core(monkeypatch, (card(),), claimed=False)
    post, _ = await _poll()
    post.return_value.delete.assert_awaited_once()


async def test_a_posted_open_card_is_left_alone_until_its_deadline(monkeypatch):
    core = _Core(monkeypatch, (card(card_message_id="500"),))
    post, fetch = await _poll()
    post.assert_not_awaited()
    fetch.assert_not_awaited()
    core.expire.assert_not_awaited()


async def test_at_its_deadline_the_card_expires_shows_its_final_words_and_settles(monkeypatch):
    rows = (LoginCardRow(0, "GITHUB_TOKEN", "no_answer"), LoginCardRow(1, "Linear", "no_answer"))
    expired = card(card_message_id="500", state="expired", rows=rows)
    core = _Core(monkeypatch, (card(card_message_id="500"),), expired=expired)
    _, fetch = await _poll(now=NOW + timedelta(seconds=61))
    core.expire.assert_awaited_once()
    view = fetch.return_value.edit.await_args.kwargs["view"]
    assert "No answer, so workers ran without: GITHUB_TOKEN, Linear" in _texts(view)
    assert _buttons(view) == []
    assert core.settled == [RUN]


async def test_after_a_restart_an_unsettled_final_card_is_redrawn_and_settled(monkeypatch):
    core = _Core(monkeypatch, (card(card_message_id="500", state="decided"),))
    _, fetch = await _poll()
    fetch.return_value.edit.assert_awaited_once()
    assert core.settled == [RUN]


async def test_a_retryable_discord_error_retries_and_a_gone_thread_stops_a_final_card(
    monkeypatch,
):
    def http(status: int) -> discord.HTTPException:
        return discord.HTTPException(MagicMock(status=status, reason="x"), "x")

    core = _Core(monkeypatch, (card(card_message_id="500", state="decided"),))
    await _poll(fetch=AsyncMock(side_effect=http(503)))
    assert core.settled == []
    await _poll(fetch=AsyncMock(side_effect=http(403)))
    assert core.settled == [RUN]


def test_the_poller_is_fast_enough_for_the_spawn_wait():
    from daimon.core.fleet_login_decision import LOGIN_WAIT_S

    assert fleet_login.POLL_INTERVAL_S * 5 <= LOGIN_WAIT_S
