"""Page-event keys are derived, never stored, and survive a crypto key rotation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from daimon.core.artifact_pages.keys import page_event_key
from daimon.core.continuity.page_events import sign_page_event
from daimon.core.stores import watches
from sqlalchemy.ext.asyncio import AsyncSession

from .artifact_support import page_watch, watch_owner


@pytest.mark.fresh_schema
async def test_a_derived_key_is_accepted_before_and_after_a_crypto_key_rotation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    binding = await watch_owner(db_session, monkeypatch)
    page_id = uuid.uuid4()
    watch, key_id = await page_watch(
        db_session, binding, page_id=page_id, crypto_keys=(old,), now=now
    )
    assert await watches.live_key_id(db_session, watch.id, now=now) == key_id
    for keys in ((old,), (new, old)):
        key = page_event_key(keys, watch.id, key_id)
        assert key is not None, keys
        event = sign_page_event(
            key=key, watch_id=watch.id, page_id=page_id, key_id=key_id, body=b"{}", now=now
        )
        assert await watches.accept_page_event(db_session, event, key=key) is not None, keys
    assert page_event_key((new,), watch.id, key_id) is None, "gone with its crypto key"
