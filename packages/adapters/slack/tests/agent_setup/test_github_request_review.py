"""Shared GitHub request cards disclose details only in a verified modal."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.slack.agent_setup import github_requests as module


class _Sessions:
    @asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield object()


@pytest.mark.asyncio
async def test_review_requires_card_ts_and_live_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    request_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    request = SimpleNamespace(
        id=request_id,
        tenant_id=tenant_id,
        parent_channel_id="C1",
        admin_card_message_id="123.456",
        status="open",
        agent_name="Helper",
        repo_names=["private/repo"],
        required_ability="write",
    )
    client = SimpleNamespace(views_open=AsyncMock())
    ephemeral = AsyncMock()
    admin = AsyncMock(return_value=False)
    monkeypatch.setattr(module, "derive_tenant_uuid", lambda **_kw: tenant_id)
    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(module, "lookup_request", AsyncMock(return_value=request))
    monkeypatch.setattr(module, "find_platform_principal", AsyncMock(return_value=None))
    monkeypatch.setattr(module, "resolve_is_admin", admin)
    monkeypatch.setattr(module, "post_ephemeral", ephemeral)
    payload = {
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "message": {"ts": "wrong"},
        "trigger_id": "trigger",
        "actions": [{"action_id": "github_request__review", "value": str(request_id)}],
    }
    runtime = SimpleNamespace(sessionmaker=_Sessions())
    await module.handle_action(runtime, payload)  # type: ignore[arg-type]
    admin.assert_not_awaited()
    client.views_open.assert_not_awaited()
    payload["message"] = {"ts": "123.456"}
    await module.handle_action(runtime, payload)  # type: ignore[arg-type]
    client.views_open.assert_not_awaited()
    assert "Only a workspace admin" in ephemeral.await_args.kwargs["text"]
    admin.return_value = True
    await module.handle_action(runtime, payload)  # type: ignore[arg-type]
    client.views_open.assert_awaited_once()
    view = client.views_open.await_args.kwargs["view"]
    assert "private/repo" in str(view["blocks"])
    assert view["private_metadata"]
    assert "private/repo" not in str(payload)
    client.views_update = AsyncMock()
    modal_payload = {
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "private_metadata": json.dumps(
                {
                    "request_id": str(request_id),
                    "channel_id": "C1",
                    "message_id": "wrong",
                }
            ),
        },
        "actions": [{"action_id": "github_request__decision", "value": f"{request_id}:approve"}],
    }
    await module.handle_action(runtime, modal_payload)  # type: ignore[arg-type]
    client.views_update.assert_awaited_once()
    assert "unavailable" in str(client.views_update.await_args.kwargs["view"])
