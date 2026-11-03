"""Offline discord.py ViewStore retention checks for private route controls."""

import asyncio
from unittest.mock import MagicMock

from daimon.adapters.discord.commands.direct_messages import DirectMessageCog
from discord.ui.view import ViewStore


async def test_latest_switch_per_route_removes_superseded_views_from_discord_store():
    cog = DirectMessageCog(MagicMock())
    store = ViewStore(MagicMock())
    previous = None
    for index in range(100):
        # Opening notices, replies and server switches all use this control factory.
        view = cog._switch_control(user_id=999, route_id=555, scope_id=f"dm:{index}")
        store.add_view(view, message_id=1000 + index)
        assert view.timeout is not None and view.timeout > 0
        assert len(store._views) == 1 and len(store._synced_message_views) == 1
        assert len(cog._switch_views) == 1
        if previous is not None:
            assert previous.is_finished()
        previous = view
    await cog.cog_unload()
    assert not store._views and not store._synced_message_views
    assert not cog._switch_views
    assert previous.is_finished()


async def test_switch_timeout_clears_view_store_and_route_registry():
    cog = DirectMessageCog(MagicMock())
    store = ViewStore(MagicMock())
    view = cog._switch_control(user_id=999, route_id=555, scope_id="dm:1")
    store.add_view(view, message_id=1001)
    view._dispatch_timeout()
    await asyncio.sleep(0)
    assert not store._views and not store._synced_message_views
    assert not cog._switch_views
    await cog.cog_unload()


async def test_unload_stops_pending_selectors_and_drops_original_text(
    db_session_factory, monkeypatch
):
    from .test_cold_direct_messages import message, selector, setup

    cog, _, _, _ = await setup(db_session_factory, monkeypatch, count=2)
    msg = message()
    await cog.on_message(msg)
    view = selector(msg)
    assert cog._pending
    await cog.cog_unload()
    assert view.is_finished() and view._expiry.cancelled()
    assert not cog._pending


async def test_an_inflight_reply_cannot_register_a_new_switch_after_unload():
    cog = DirectMessageCog(MagicMock())
    await cog.cog_unload()
    view = cog._switch_control(user_id=999, route_id=555, scope_id="dm:1")
    assert view.is_finished() and view._expiry.cancelled()
    assert not cog._switch_views
