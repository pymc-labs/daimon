"""Turn-card lookup against discord.py's fetched-message component shape."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import discord
import pytest
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.turn_card_recovery import (
    TurnCardSearchState,
    find_turn_card_message,
    post_initial_turn_card,
    retire_terminal_turn_card,
    turn_card_custom_id,
    turn_card_ids_from_message,
)
from daimon.adapters.discord.views import CancelView
from daimon.core.stores.turn_card_intents import list_recoverable_turn_card_intents
from daimon.core.turn.state import TurnState
from daimon.testing.factories import make_tenant
from sqlalchemy.exc import SQLAlchemyError

_TURN_ID = UUID("12345678-1234-5678-1234-567812345678")
_CREATED_AFTER = datetime(2026, 9, 25, tzinfo=UTC)
_SEARCH_BEFORE = _CREATED_AFTER + timedelta(minutes=5)


def _fetched_message(message_id: int, *turn_ids: UUID) -> discord.Message:
    """Build the SDK model from a Discord REST message payload."""
    channel = MagicMock()
    channel.id = 456
    channel.guild = None
    components = [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 2,
                    "label": "Cancel",
                    "custom_id": turn_card_custom_id(turn_id),
                }
                for turn_id in turn_ids
            ],
        }
    ]
    payload: dict[str, Any] = {
        "id": str(message_id),
        "type": 0,
        "content": "Working…",
        "channel_id": "456",
        "author": {"id": "123", "username": "Daimon", "discriminator": "0"},
        "components": components,
    }
    return discord.Message(state=MagicMock(), channel=channel, data=payload)  # type: ignore[arg-type]


class _HistoryThread:
    """Small history transport whose yielded groups represent SDK pages."""

    def __init__(self, pages: Sequence[Sequence[discord.Message] | discord.HTTPException]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def history(
        self,
        *,
        after: datetime,
        before: datetime,
        oldest_first: bool,
        limit: int,
    ) -> AsyncIterator[discord.Message]:
        self.calls.append(
            {"after": after, "before": before, "oldest_first": oldest_first, "limit": limit}
        )

        async def iterate() -> AsyncIterator[discord.Message]:
            for page in self.pages:
                if isinstance(page, discord.HTTPException):
                    raise page
                for message in page:
                    yield message

        return iterate()


def test_fetched_message_exposes_custom_id_through_action_row_button() -> None:
    message = _fetched_message(900, _TURN_ID)

    assert turn_card_ids_from_message(message) == frozenset({_TURN_ID}), (
        "discord.py should retain a posted button custom_id when rebuilding Message from REST data"
    )


@pytest.mark.asyncio
async def test_history_lookup_finds_turn_card_across_pages() -> None:
    thread = _HistoryThread(
        [
            [_fetched_message(899)],
            [_fetched_message(900, _TURN_ID)],
        ]
    )

    result = await find_turn_card_message(
        thread, turn_id=_TURN_ID, created_after=_CREATED_AFTER, before=_SEARCH_BEFORE
    )  # type: ignore[arg-type]

    assert result.state is TurnCardSearchState.FOUND, "one matching message should be found"
    assert result.message_ids == (900,), "the matching Discord message ID should be returned"
    assert thread.calls == [
        {
            "after": _CREATED_AFTER - timedelta(seconds=1),
            "before": _SEARCH_BEFORE,
            "oldest_first": True,
            "limit": 1000,
        }
    ], "the lookup should include the intent's millisecond bucket and bound the search window"


@pytest.mark.asyncio
async def test_history_lookup_reports_multiple_matching_messages() -> None:
    thread = _HistoryThread([[_fetched_message(901, _TURN_ID)], [_fetched_message(900, _TURN_ID)]])

    result = await find_turn_card_message(
        thread, turn_id=_TURN_ID, created_after=_CREATED_AFTER, before=_SEARCH_BEFORE
    )  # type: ignore[arg-type]

    assert result.state is TurnCardSearchState.MULTIPLE, (
        "two cards carrying one durable turn ID must be reported as ambiguous"
    )
    assert result.message_ids == (900, 901), "all matching message IDs should be retained"


@pytest.mark.asyncio
async def test_history_api_failure_after_match_is_indeterminate() -> None:
    response = MagicMock()
    response.status = 403
    forbidden = discord.Forbidden(response, "missing read-history permission")
    thread = _HistoryThread([[_fetched_message(900, _TURN_ID)], forbidden])

    result = await find_turn_card_message(
        thread, turn_id=_TURN_ID, created_after=_CREATED_AFTER, before=_SEARCH_BEFORE
    )  # type: ignore[arg-type]

    assert result.state is TurnCardSearchState.INDETERMINATE, (
        "an unread history page means duplicate count is unknown even after a match"
    )
    assert result.message_ids == (900,), "partial evidence should remain inspectable"


@pytest.mark.asyncio
async def test_exhaustive_history_without_match_is_only_reported_as_not_found() -> None:
    thread = _HistoryThread([[_fetched_message(899)], [_fetched_message(900)]])

    result = await find_turn_card_message(
        thread, turn_id=_TURN_ID, created_after=_CREATED_AFTER, before=_SEARCH_BEFORE
    )  # type: ignore[arg-type]

    assert result.state is TurnCardSearchState.NOT_FOUND, (
        "the lookup reports absence without deciding whether to post another card"
    )
    assert result.message_ids == (), "nonmatching messages should not be returned"


@pytest.mark.asyncio
async def test_history_lookup_reports_indeterminate_when_message_budget_is_exhausted() -> None:
    thread = _HistoryThread([[_fetched_message(899), _fetched_message(900)]])

    result = await find_turn_card_message(
        thread,
        turn_id=_TURN_ID,
        created_after=_CREATED_AFTER,
        before=_SEARCH_BEFORE,
        message_budget=2,
    )  # type: ignore[arg-type]

    assert result.state is TurnCardSearchState.INDETERMINATE, (
        "reaching the scan budget cannot prove that no later matching card exists"
    )


@pytest.mark.asyncio
async def test_initial_card_intent_is_committed_before_send_and_retired_after_cancel(
    db_session: Any, db_session_factory: Any
) -> None:
    tenant = await make_tenant(db_session, workspace_id="turn-card-discord")
    await db_session.commit()
    observed: dict[str, object] = {}
    edits: list[dict[str, Any]] = []
    cancel_event = asyncio.Event()

    async def send(**kwargs: Any) -> object:
        async with db_session_factory() as session:
            intents = await list_recoverable_turn_card_intents(session, platform="discord")
        assert len(intents) == 1, "intent must be committed and visible before Discord send"
        intent = intents[0]
        observed["intent_id"] = intent.id
        view = kwargs["view"]
        observed["custom_id"] = view.cancel_button.custom_id
        return SimpleNamespace(id=900)

    async def edit(_message: object, **_kwargs: Any) -> None:
        edits.append(_kwargs)
        return None

    def make_lifecycle(turn_id: UUID, on_first_post: Any) -> DiscordTurnLifecycle:
        return DiscordTurnLifecycle(
            send=send,
            edit=edit,
            agent_name="test-agent",
            model_id="claude-sonnet-4-6",
            cancel_view=CancelView(allowed_user_id=123, cancel=cancel_event, turn_id=turn_id),
            on_first_post=on_first_post,
        )

    intent, lifecycle = await post_initial_turn_card(
        db_session_factory,
        tenant_id=tenant.id,
        thread_id="456",
        make_lifecycle=make_lifecycle,
    )
    assert observed["intent_id"] == intent.id, "the durable row should back the posted button"
    assert observed["custom_id"] == turn_card_custom_id(intent.id), (
        "CancelView must carry the durable intent UUID"
    )
    async with db_session_factory() as session:
        rows = await list_recoverable_turn_card_intents(session, platform="discord")
    assert len(rows) == 1 and rows[0].message_id == "900", (
        "the Discord response ID should be committed before post_initial_turn_card returns"
    )

    await lifecycle.on_terminal_success(TurnState())
    assert any(edit.get("content") == "Turn cancelled." for edit in edits), (
        "empty terminal success should render the existing cancellation state"
    )
    assert await retire_terminal_turn_card(
        db_session_factory, intent_id=intent.id, expected_message_id="900"
    ), "a clean cancellation terminal should retire the matching card intent"
    async with db_session_factory() as session:
        rows = await list_recoverable_turn_card_intents(session, platform="discord")
    assert rows == [], "retired terminal intents should no longer be recoverable"


@pytest.mark.asyncio
async def test_ambiguous_initial_post_failure_keeps_prepared_intent(
    db_session: Any, db_session_factory: Any
) -> None:
    tenant = await make_tenant(db_session, workspace_id="turn-card-post-failure")
    await db_session.commit()
    cancel_event = asyncio.Event()

    async def send(**_kwargs: Any) -> object:
        async with db_session_factory() as session:
            intents = await list_recoverable_turn_card_intents(session, platform="discord")
        assert len(intents) == 1, "prepare must commit before the remote request starts"
        raise RuntimeError("transport ended before Discord response")

    async def edit(_message: object, **_kwargs: Any) -> None:
        return None

    def make_lifecycle(turn_id: UUID, on_first_post: Any) -> DiscordTurnLifecycle:
        return DiscordTurnLifecycle(
            send=send,
            edit=edit,
            agent_name="test-agent",
            model_id="claude-sonnet-4-6",
            cancel_view=CancelView(allowed_user_id=123, cancel=cancel_event, turn_id=turn_id),
            on_first_post=on_first_post,
        )

    with pytest.raises(RuntimeError, match="transport ended"):
        await post_initial_turn_card(
            db_session_factory,
            tenant_id=tenant.id,
            thread_id="789",
            make_lifecycle=make_lifecycle,
        )
    async with db_session_factory() as session:
        rows = await list_recoverable_turn_card_intents(session, platform="discord")
    assert len(rows) == 1 and rows[0].message_id is None, (
        "a transport exception is ambiguous; retain the prepared intent for later lookup"
    )


@pytest.mark.asyncio
async def test_message_id_persistence_failure_stops_before_helper_returns(
    db_session: Any, db_session_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await make_tenant(db_session, workspace_id="turn-card-record-failure")
    await db_session.commit()
    sends: list[int] = []

    async def send(**_kwargs: Any) -> object:
        sends.append(1)
        return SimpleNamespace(id=901)

    async def edit(_message: object, **_kwargs: Any) -> None:
        return None

    async def fail_record(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("message ID database write failed")

    monkeypatch.setattr(
        "daimon.adapters.discord.turn_card_recovery.record_turn_card_message", fail_record
    )

    def make_lifecycle(turn_id: UUID, on_first_post: Any) -> DiscordTurnLifecycle:
        return DiscordTurnLifecycle(
            send=send,
            edit=edit,
            agent_name="test-agent",
            model_id="claude-sonnet-4-6",
            cancel_view=CancelView(allowed_user_id=123, cancel=asyncio.Event(), turn_id=turn_id),
            on_first_post=on_first_post,
        )

    with pytest.raises(RuntimeError, match="message ID database write failed"):
        await post_initial_turn_card(
            db_session_factory,
            tenant_id=tenant.id,
            thread_id="901",
            make_lifecycle=make_lifecycle,
        )
    assert sends == [1], "Discord accepted the post before persistence failed"
    async with db_session_factory() as session:
        rows = await list_recoverable_turn_card_intents(session, platform="discord")
    assert len(rows) == 1 and rows[0].message_id is None, (
        "failed response-ID persistence must preserve the prepared intent for recovery"
    )


@pytest.mark.asyncio
async def test_terminal_intent_retirement_failure_is_best_effort(
    db_session: Any,
    db_session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, workspace_id="turn-card-retire-failure")
    await db_session.commit()

    async def send(**_kwargs: Any) -> object:
        return SimpleNamespace(id=903)

    async def edit(_message: object, **_kwargs: Any) -> None:
        return None

    def make_lifecycle(_turn_id: UUID, on_first_post: Any) -> DiscordTurnLifecycle:
        return DiscordTurnLifecycle(
            send=send,
            edit=edit,
            agent_name="test-agent",
            model_id="claude-sonnet-4-6",
            on_first_post=on_first_post,
        )

    intent, _lifecycle = await post_initial_turn_card(
        db_session_factory,
        tenant_id=tenant.id,
        thread_id="902",
        make_lifecycle=make_lifecycle,
    )

    async def fail_retirement(*_args: Any, **_kwargs: Any) -> bool:
        raise SQLAlchemyError("retirement database write failed")

    monkeypatch.setattr(
        "daimon.adapters.discord.turn_card_recovery.retire_turn_card_intent",
        fail_retirement,
    )
    retired = await retire_terminal_turn_card(
        db_session_factory, intent_id=intent.id, expected_message_id="903"
    )
    assert retired is False, "retirement failure must not replace an already terminal turn result"
