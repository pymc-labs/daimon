"""Session compatibility preserves work before any credential synchronization."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from anthropic import InternalServerError
from daimon.core.stores.thread_sessions import get_thread_session_by_id
from daimon.core.turn.errors import SessionAgentMismatch
from daimon.core.turn.session_identity import check_session_agent
from daimon.testing.factories import make_thread_session
from daimon.testing.ma import MARouter, build_fake_anthropic
from daimon.testing.ma_models import ma_session, ma_session_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("destination", ["agent_original", "agent_new"])
async def test_session_identity_preserves_mapping_and_watermark(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    legacy: bool,
    destination: str,
) -> None:
    mapping = await make_thread_session(
        db_session,
        ma_session_id="sess_work",
        ma_agent_id=None if legacy else "agent_original",
        watermark_message_id="progress",
    )
    await db_session.commit()
    now = datetime.now(UTC)
    observed = ma_session(
        id="sess_work",
        agent=ma_session_agent(id="agent_original", name="original"),
        environment_id="env_science",
        created_at=now,
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions/sess_work",
        lambda request, match: httpx.Response(200, json=observed.model_dump(mode="json")),
    )
    anthropic = build_fake_anthropic(router.dispatch)
    if destination == "agent_new":
        with pytest.raises(SessionAgentMismatch) as error:
            await check_session_agent(
                anthropic, db_session_factory, mapping=mapping, responder_ma_agent_id=destination
            )
        assert error.value.mapping_id == mapping.id, "mismatch identifies retained mapping"
        assert error.value.source_agent_id == "agent_original", "source comes from mapping or MA"
        assert error.value.destination_agent_id == destination, (
            "destination is the resolved responder"
        )
    else:
        identity = await check_session_agent(
            anthropic, db_session_factory, mapping=mapping, responder_ma_agent_id=destination
        )
        assert identity.session_exists, "same agent can reuse its workspace"
        assert (identity.observed is not None) is legacy, (
            "the retrieved session is handed back exactly when a retrieve happened"
        )
    db_session.expire_all()
    retained = await get_thread_session_by_id(db_session, id=mapping.id)
    assert retained is not None, "mismatch must not delete a mapping"
    assert retained.ma_agent_id == "agent_original", (
        "legacy discovery records the observed identity"
    )
    assert retained.ma_session_id == "sess_work" and retained.status == "live", (
        "existing work stays live"
    )
    assert retained.watermark_message_id == "progress", "mismatch must not reset history progress"


@pytest.mark.parametrize("status", [404, 500])
async def test_missing_legacy_session_remains_separate_from_upstream_failure(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    status: int,
) -> None:
    mapping = await make_thread_session(db_session, ma_session_id="sess_missing")
    await db_session.commit()
    client = build_fake_anthropic(
        lambda request: httpx.Response(
            status, json={"type": "error", "error": {"type": "api_error", "message": "unavailable"}}
        )
    )
    if status == 404:
        identity = await check_session_agent(
            client, db_session_factory, mapping=mapping, responder_ma_agent_id="agent_new"
        )
        assert not identity.session_exists, "missing session goes to existing recovery path"
        assert identity.observed is None, "a missing session yields no observed session"
    else:
        with pytest.raises(InternalServerError):
            await check_session_agent(
                client, db_session_factory, mapping=mapping, responder_ma_agent_id="agent_new"
            )
    retained = await get_thread_session_by_id(db_session, id=mapping.id)
    assert retained is not None and retained.status == "live", (
        "lookup failure must not discard work"
    )
    assert retained.ma_agent_id is None, "failed lookup must not invent a source identity"
