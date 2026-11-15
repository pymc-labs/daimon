"""The install's own id: stamped once, shared by every process, replaced on request."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from daimon.core.stores import deployment_identity
from daimon.core.turn import openai_environment


@pytest.fixture
def off_gcp(monkeypatch: pytest.MonkeyPatch) -> httpx.MockTransport:
    monkeypatch.setattr(openai_environment, "_deployment", None)
    monkeypatch.setattr(openai_environment, "_missed_until", None)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no metadata server", request=request)

    return httpx.MockTransport(unreachable)


async def test_the_id_is_stamped_once_and_read_back(db_session_factory) -> None:
    async with db_session_factory.begin() as session:
        first = await deployment_identity.get_or_stamp_deployment_id(session)
    async with db_session_factory.begin() as session:
        second = await deployment_identity.get_or_stamp_deployment_id(session)
    assert first == second, "a second process must read the stamped id, not stamp its own"


async def test_reset_replaces_the_id(db_session_factory) -> None:
    async with db_session_factory.begin() as session:
        first = await deployment_identity.get_or_stamp_deployment_id(session)
    async with db_session_factory.begin() as session:
        fresh = await deployment_identity.reset_deployment_id(session)
    async with db_session_factory.begin() as session:
        read = await deployment_identity.get_or_stamp_deployment_id(session)
    assert fresh != first and read == fresh, "reset must change the one stored id"


async def test_off_gcp_two_processes_name_the_deployment_alike(db_session_factory, off_gcp) -> None:
    kwargs = {"transport": off_gcp, "sessionmaker": db_session_factory, "project": "proj"}
    first = await openai_environment.deployment_id(**kwargs)
    openai_environment._missed_until = datetime.now(UTC) - timedelta(seconds=1)
    second = await openai_environment.deployment_id(**kwargs)
    assert first is not None and first == second, "one install, one id"
    assert first.endswith("-proj"), "the OpenAI project is part of the name"
    assert await openai_environment.deployment_id(transport=off_gcp) is None, "no database: unknown"


async def test_on_gcp_the_metadata_id_wins_over_the_stored_one(
    db_session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openai_environment, "_deployment", None)
    monkeypatch.setattr(openai_environment, "_missed_until", None)
    answered = httpx.MockTransport(lambda request: httpx.Response(200, text="example-prod"))
    found = await openai_environment.deployment_id(
        transport=answered, sessionmaker=db_session_factory, project="proj"
    )
    assert found == "example-prod", "GCP keeps its project id"
