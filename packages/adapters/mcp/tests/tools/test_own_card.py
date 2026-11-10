"""One poster for Daimon's own cards: each platform's card lands in the origin's own thread."""

from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace

import pytest
from daimon.adapters.mcp.tools import _own_card
from daimon.adapters.mcp.tools.discord import _switch_card
from daimon.adapters.mcp.tools.slack import _credential_button
from daimon.adapters.mcp.tools.teams import _send
from daimon.core.posted_controls import PostedCard
from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.core.switch_cards import requested_switch_card
from fastmcp.exceptions import ToolError


def _origin(platform: str, thread_id: str = "thread-1") -> TurnOriginRow:
    now = dt.datetime.now(dt.UTC)
    return TurnOriginRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        platform=platform,
        parent_channel_id="channel-1",
        thread_id=thread_id,
        responder_ma_agent_id="agent_1",
        responder_name="Researcher",
        configuration_target_ma_agent_id=None,
        configuration_target_name=None,
        role=Role.USER,
        created_at=now,
        expires_at=now + dt.timedelta(hours=1),
    )


def _card() -> PostedCard:
    return requested_switch_card(
        uuid.uuid4(),
        title="Change the model provider?",
        facts=(),
        requester_platform_user_id="user-1",
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
    seen: list[tuple[str, dict[str, object]]] = []

    def poster(name: str):
        async def post(*_: object, **kwargs: object) -> str:
            seen.append((name, kwargs))
            return f"{name}-message"

        return post

    monkeypatch.setattr(_credential_button, "post_slack_control_card", poster("slack"))
    monkeypatch.setattr(_send, "post_teams_control_card", poster("teams"))
    monkeypatch.setattr(_switch_card, "post_switch_card", poster("discord"))
    return seen


@pytest.mark.parametrize(
    ("origin", "where"),
    [
        (_origin("slack", "1700.1"), {"channel_id": "channel-1", "thread_ts": "1700.1"}),
        (_origin("slack", "dm:abc"), {"channel_id": "channel-1", "thread_ts": None}),
        (_origin("teams", "19:chat@thread.v2"), {"thread_id": "19:chat@thread.v2"}),
        (_origin("discord"), {"channel_id": "thread-1"}),
    ],
)
async def test_the_card_goes_to_the_origins_thread_on_each_platform(
    calls: list[tuple[str, dict[str, object]]], origin: TurnOriginRow, where: dict[str, object]
) -> None:
    """The origin, not the tool, picks where the card lands; a DM scope posts at the top."""
    runtime = SimpleNamespace(teams_client=object())
    card = _card()

    got = await _own_card.post_own_card(runtime, SimpleNamespace(), origin=origin, card=card)  # pyright: ignore[reportArgumentType]

    ((name, kwargs),) = calls
    assert (name, got) == (origin.platform, f"{origin.platform}-message"), "its platform posts"
    assert {k: kwargs[k] for k in where} == where, "the card lands in the origin's thread"
    assert kwargs["card"] is card, "the card is posted as built"


async def test_an_unknown_platform_is_refused(calls: list[tuple[str, dict[str, object]]]) -> None:
    """A platform with no poster refuses rather than posting somewhere else."""
    with pytest.raises(ToolError):
        await _own_card.post_own_card(
            SimpleNamespace(),  # pyright: ignore[reportArgumentType]
            SimpleNamespace(),  # pyright: ignore[reportArgumentType]
            origin=_origin("cli"),
            card=_card(),
        )
    assert calls == [], "nothing was posted"


async def test_a_web_card_is_a_notice_to_the_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    """The web has no message API: the card goes to the chat as a notice, keyed by its id."""
    from contextlib import asynccontextmanager

    from daimon.core.stores import web_notices

    sent: list[object] = []

    async def send(_session: object, notice: object) -> None:
        sent.append(notice)

    @asynccontextmanager
    async def begin():
        yield object()

    monkeypatch.setattr(web_notices, "send_web_notice", send)
    card = _card()
    runtime = SimpleNamespace(session_factory=SimpleNamespace(begin=begin))

    got = await _own_card.post_own_card(
        runtime,  # pyright: ignore[reportArgumentType]
        SimpleNamespace(),  # pyright: ignore[reportArgumentType]
        origin=_origin("web", "web:t"),
        card=card,
    )

    (notice,) = sent
    assert isinstance(notice, web_notices.WebControlNotice), "a control notice is sent"
    assert (notice.thread_id, notice.title) == ("web:t", card.headline), "it names the chat"
    assert got == notice.card_id and got.startswith("bsw:"), "the card id is the message id"
