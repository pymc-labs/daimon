"""Tests for routines_panel/submit.py.

Covers:
- evaluate_routines_create_submission: valid → proceed/clear + extra; missing
  field → response_action errors keyed to the right block.
- run_routines_create_submission (real Postgres + transport-level fakes):
  - dev_allow_all admin: a valid submission creates a routine row whose
    created_by_user_id is the submitting Slack user id.
  - bad cron: no row created + :x: ephemeral posted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yarl
from daimon.adapters.slack.routines_panel.submit import (
    RoutinesCreateDecision,
    evaluate_routines_create_submission,
    run_routines_create_submission,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core._models import Tenant
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.routines import list_routines_for_tenant
from daimon.testing.ma import build_fake_anthropic

_TEAM_ID = "T_RC"
_USER_ID = "U_RC_CREATOR"
_CHANNEL_ID = "C_RC"
_AGENT_NAME = "daimon"


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _values(
    *,
    agent: str = _AGENT_NAME,
    cron: str = "0 18 * * *",
    timezone_: str = "UTC",
    message: str = "daily standup",
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["routines_create__agent"] = {
        "routines_create__agent": {
            "type": "static_select",
            "selected_option": {"value": agent} if agent else None,
        }
    }
    out["routines_create__cron"] = {
        "routines_create__cron": {"type": "plain_text_input", "value": cron}
    }
    out["routines_create__timezone"] = {
        "routines_create__timezone": {"type": "plain_text_input", "value": timezone_}
    }
    out["routines_create__message"] = {
        "routines_create__message": {"type": "plain_text_input", "value": message}
    }
    return out


def _payload(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "user": {"id": _USER_ID},
        "view": {
            "callback_id": "routines__create",
            "state": {"values": values},
        },
    }


# ---------------------------------------------------------------------------
# Pure evaluator tests
# ---------------------------------------------------------------------------


def test_evaluate_routines_create_when_valid_returns_clear_and_proceed() -> None:
    decision = evaluate_routines_create_submission(_payload(_values()))

    assert isinstance(decision, RoutinesCreateDecision), "should return RoutinesCreateDecision"
    assert decision.proceed is True, "valid submission should proceed"
    assert decision.response_payload.get("response_action") == "clear", (
        "valid submission should ack with response_action=clear"
    )
    assert decision.extra.get("agent_name") == _AGENT_NAME, "agent_name carried to extra"
    assert decision.extra.get("cron_expr") == "0 18 * * *", "cron carried to extra"
    assert decision.extra.get("timezone") == "UTC", "timezone carried to extra"
    assert decision.extra.get("trigger_message") == "daily standup", "message carried to extra"


def test_evaluate_routines_create_when_agent_missing_returns_errors_keyed_agent() -> None:
    decision = evaluate_routines_create_submission(_payload(_values(agent="")))

    assert decision.proceed is False, "missing agent should not proceed"
    errors: dict[str, str] = decision.response_payload.get("errors", {})
    assert "routines_create__agent" in errors, "error keyed to the agent block"


def test_evaluate_routines_create_when_cron_missing_returns_errors_keyed_cron() -> None:
    decision = evaluate_routines_create_submission(_payload(_values(cron="")))

    assert decision.proceed is False, "missing cron should not proceed"
    errors = decision.response_payload.get("errors", {})
    assert "routines_create__cron" in errors, "error keyed to the cron block"


def test_evaluate_routines_create_when_message_missing_returns_errors_keyed_message() -> None:
    decision = evaluate_routines_create_submission(_payload(_values(message="")))

    assert decision.proceed is False, "missing message should not proceed"
    errors = decision.response_payload.get("errors", {})
    assert "routines_create__message" in errors, "error keyed to the message block"


# ---------------------------------------------------------------------------
# run_routines_create_submission — DB tests (real Postgres + fake MA)
# ---------------------------------------------------------------------------


def _agent_handler(tenant_id_str: str, *, agent_name: str = _AGENT_NAME) -> Any:
    """httpx.MockTransport handler serving one daimon-tagged agent on /v1/agents."""
    ma_agent_id = f"agent_{'a' * 24}"
    now = datetime.now(UTC).isoformat()
    agent_data: dict[str, object] = {
        "id": ma_agent_id,
        "type": "agent",
        "name": agent_name,
        "version": 1,
        "model": {"id": "claude-sonnet-4-6", "speed": "standard"},
        "system": None,
        "metadata": {
            MA_METADATA_KEY_TENANT: tenant_id_str,
            MA_METADATA_KEY_NAME: agent_name,
        },
        "mcp_servers": [],
        "tools": [],
        "skills": [],
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "description": None,
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/v1/agents":
            return httpx.Response(200, json={"data": [agent_data], "has_more": False})
        return httpx.Response(404, json={"error": f"unhandled {method} {path}"})

    return _handler


def _build_runtime(handler: Any, db_session_factory: Any) -> SlackRuntime:
    settings: MagicMock = MagicMock()
    settings.slack.dev_allow_all_admin = True
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(handler),
        sessionmaker=db_session_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )


@pytest.mark.asyncio
async def test_run_routines_create_when_dev_allow_all_creates_row_with_creator_user_id(
    fake_slack_web_client: Any,
    db_session_factory: Any,
) -> None:
    """A valid submission (dev_allow_all admin) creates a routine row whose
    created_by_user_id is the submitting Slack user id — the whole point of the
    Slack-native create surface."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    async with db_session_factory() as session:
        session.add(Tenant(id=tenant_id, platform="slack", external_id=_TEAM_ID))
        await session.commit()

    runtime = _build_runtime(_agent_handler(str(tenant_id)), db_session_factory)

    await run_routines_create_submission(
        runtime,
        fake_slack_web_client.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        extra={
            "agent_name": _AGENT_NAME,
            "cron_expr": "0 18 * * *",
            "timezone": "UTC",
            "trigger_message": "daily standup",
        },
    )

    async with db_session_factory() as session:
        rows = await list_routines_for_tenant(session, tenant_id=tenant_id)

    assert len(rows) == 1, "exactly one routine should be created"
    row = rows[0]
    assert row.created_by_user_id == _USER_ID, (
        "created_by_user_id must be the submitting Slack user id (not None)"
    )
    assert row.agent_name == _AGENT_NAME, "agent_name should be persisted"
    assert row.next_fire_at is not None, "next_fire_at should be computed from cron"

    ephemeral_key = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
    assert ephemeral_key in fake_slack_web_client.mock.requests, (
        "success should post a confirmation ephemeral"
    )
    texts = [c.kwargs["json"]["text"] for c in fake_slack_web_client.mock.requests[ephemeral_key]]
    assert any(":white_check_mark:" in t for t in texts), (
        "success confirmation must include :white_check_mark:"
    )


@pytest.mark.asyncio
async def test_run_routines_create_when_bad_cron_creates_no_row_and_posts_error(
    fake_slack_web_client: Any,
    db_session_factory: Any,
) -> None:
    """A bad cron expression must create no routine row and post an :x: ephemeral."""
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    async with db_session_factory() as session:
        session.add(Tenant(id=tenant_id, platform="slack", external_id=_TEAM_ID))
        await session.commit()

    runtime = _build_runtime(_agent_handler(str(tenant_id)), db_session_factory)

    await run_routines_create_submission(
        runtime,
        fake_slack_web_client.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        extra={
            "agent_name": _AGENT_NAME,
            "cron_expr": "not a cron",
            "timezone": "UTC",
            "trigger_message": "daily standup",
        },
    )

    async with db_session_factory() as session:
        rows = await list_routines_for_tenant(session, tenant_id=tenant_id)

    assert len(rows) == 0, "bad cron must create no routine row"

    ephemeral_key = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
    assert ephemeral_key in fake_slack_web_client.mock.requests, "bad cron should post an ephemeral"
    texts = [c.kwargs["json"]["text"] for c in fake_slack_web_client.mock.requests[ephemeral_key]]
    assert any(":x:" in t for t in texts), "bad cron ephemeral must include :x:"
