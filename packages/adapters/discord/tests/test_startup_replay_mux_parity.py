"""The completed-orphan reader recovers through the scoped port with read-only SDK requests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.discord import startup_replay
from daimon.core.stores.domain import ThreadSessionRow
from daimon.testing.turn_router import turn_events
from mux.contracts.ids import Scope
from mux.drivers.anthropic import AnthropicManagedAgents
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.drivers.anthropic.ma_transport import ScriptedReply, ScriptedTransport

NOW = datetime(2026, 10, 11, 8, tzinfo=UTC)
TENANT = UUID(int=7)
ACCOUNT = UUID(int=8)


def row(*, marker: datetime | None = NOW, account: UUID | None = ACCOUNT) -> ThreadSessionRow:
    return ThreadSessionRow(
        id=UUID(int=9),
        tenant_id=TENANT,
        account_id=account,
        platform="discord",
        thread_id="thread-N7",
        ma_session_id="session-N7",
        watermark_message_id=None,
        status="active",
        created_at=NOW,
        updated_at=NOW,
        active_turn_started_at=marker,
    )


def events(
    *, old: bool = False, user: bool = True, failed: bool = False
) -> list[dict[str, object]]:
    processed = NOW - timedelta(seconds=1) if old else NOW
    result: list[dict[str, object]] = (
        [
            {
                "id": "event-user",
                "type": "user.message",
                "processed_at": processed.isoformat(),
                "content": [{"type": "text", "text": "Question"}],
            }
        ]
        if user
        else []
    )
    result.extend(turn_events(agent_text="Recovered answer", usage_event_id=None, now=processed))
    if failed:
        result[-1]["stop_reason"] = {"type": "retries_exhausted"}
    return result


def replies(
    payload: dict[str, object],
    *,
    scenario: str,
    code: int = 200,
    retry: bool = False,
) -> list[ScriptedReply]:
    result = []
    if retry:
        result.append(
            ScriptedReply(
                "GET",
                "/v1/sessions/session-N7",
                httpx.Response(500, json={"error": {"type": "api_error", "message": "retry"}}),
            )
        )
    result.append(
        ScriptedReply("GET", "/v1/sessions/session-N7", httpx.Response(code, json=payload))
    )
    if code == 200 and payload.get("status") == "idle":
        result.append(
            ScriptedReply(
                "GET",
                "/v1/sessions/session-N7/events",
                httpx.Response(
                    200,
                    json={
                        "data": events(
                            old=scenario == "old",
                            user=scenario != "no-user",
                            failed=scenario == "failed",
                        ),
                        "has_more": False,
                        "next_page": None,
                    },
                ),
            )
        )
    return result


def test_the_read_deadline_is_unchanged() -> None:
    assert startup_replay.ORPHAN_RESULT_READ_TIMEOUT_S == 5.0


@pytest.mark.parametrize(
    ("payload", "scenario", "expected"),
    [
        ({"status": "running"}, "current", None),
        ({"status": "terminated"}, "current", None),
        ({"status": "future-provider-status"}, "current", None),
        ({"status": None}, "current", None),
        ({"status": "idle"}, "current", "Recovered answer"),
        (
            {"status": "idle", "id": None, "agent": None, "metadata": None},
            "current",
            "Recovered answer",
        ),
        (
            {"status": "idle", "agent": "unused-partial-field", "resources": [None]},
            "current",
            "Recovered answer",
        ),
        (
            ma_session(id="session-N7", status="idle").model_dump(mode="json"),
            "current",
            "Recovered answer",
        ),
        ({"status": "idle"}, "old", None),
        ({"status": "idle"}, "no-user", None),
        ({"status": "idle"}, "no-marker", None),
        ({"status": "idle"}, "failed", None),
    ],
)
async def test_status_only_read_and_recovery_match_main(
    payload: dict[str, object], scenario: str, expected: str | None
) -> None:
    after = ScriptedTransport()
    after.queue(*replies(payload, scenario=scenario))
    mapping = row(marker=None if scenario == "no-marker" else NOW)
    async with after.client() as new:
        assert await startup_replay.completed_orphan_text(new, mapping) == expected
    after.assert_consumed()
    assert all(request.method == "GET" for request in after.requests)


@pytest.mark.parametrize("code", [401, 404, 429, 500])
async def test_api_error_is_still_handled_without_an_event_read(code: int) -> None:
    payload = {"error": {"type": "api_error", "message": "provider rejected read"}}
    after = ScriptedTransport()
    after.queue(*replies(payload, scenario="current", code=code))
    async with after.client() as new:
        assert await startup_replay.completed_orphan_text(new, row()) is None
    after.assert_consumed()
    assert len(after.requests) == 1


@pytest.mark.parametrize("payload", [{}, {"id": None, "metadata": None}])
async def test_missing_consumed_status_keeps_the_sdk_none_default(
    payload: dict[str, object],
) -> None:
    after = ScriptedTransport()
    after.queue(*replies(payload, scenario="current"))
    async with after.client() as new:
        assert await startup_replay.completed_orphan_text(new, row()) is None
    after.assert_consumed()


async def test_sdk_read_retries_and_then_replays_without_mutating_client_policy() -> None:
    after = ScriptedTransport()
    after.queue(*replies({"status": "idle"}, scenario="current", retry=True))
    async with after.client(max_retries=1) as new:
        assert await startup_replay.completed_orphan_text(new, row()) == "Recovered answer"
        assert new.max_retries == 1
    after.assert_consumed()
    assert [request.path for request in after.requests] == [
        "/v1/sessions/session-N7",
        "/v1/sessions/session-N7",
        "/v1/sessions/session-N7/events",
    ]


@pytest.mark.parametrize("account", [ACCOUNT, None])
async def test_read_authorizes_only_the_mapping_session_and_uses_its_tenant(
    monkeypatch: pytest.MonkeyPatch, account: UUID | None
) -> None:
    original = startup_replay.managed_agents
    observed: list[tuple[Scope, frozenset[tuple[str, str]]]] = []

    def capture(
        client: AsyncAnthropic, *, scope: Scope, resources: frozenset[tuple[str, str]]
    ) -> AnthropicManagedAgents:
        observed.append((scope, resources))
        return original(client, scope=scope, resources=resources)

    monkeypatch.setattr(startup_replay, "managed_agents", capture)
    transport = ScriptedTransport()
    transport.queue(*replies({"status": "running"}, scenario="current"))
    async with transport.client() as client:
        assert await startup_replay.completed_orphan_text(client, row(account=account)) is None
    transport.assert_consumed()
    assert len(observed) == 1
    scope, resources = observed[0]
    assert scope.tenant_id == str(TENANT)
    assert scope.account_id == (str(account) if account else "service")
    assert scope.authorization_id == "discord:completed_orphan_text"
    assert not scope.is_platform and not scope.is_legacy_host_authorized
    assert resources == frozenset({("session", "session-N7")})
