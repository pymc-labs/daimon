"""Shared GitHub request cards disclose details only in a verified modal."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from daimon.adapters.mcp.tools.github_request_delivery import _slack_blocks
from daimon.adapters.slack.agent_setup import github_requests as module
from daimon.core.github_connect_cards import build_connect_card
from daimon.core.github_request_cards import RequestCard


class _Sessions:
    @asynccontextmanager
    async def __call__(self):  # type: ignore[no-untyped-def]
        yield object()

    @asynccontextmanager
    async def begin(self):  # type: ignore[no-untyped-def]
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
        repo_names=["private/connected", "private/unconnected"],
        required_ability="write",
    )
    client = SimpleNamespace(views_open=AsyncMock())
    ephemeral = AsyncMock()
    admin = AsyncMock(return_value=False)
    monkeypatch.setattr(module, "derive_tenant_uuid", lambda **_kw: tenant_id)
    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(module, "lookup_request", AsyncMock(return_value=request))
    monkeypatch.setattr(module, "find_platform_principal", AsyncMock(return_value=None))
    monkeypatch.setattr(
        module,
        "list_authorized_repos",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    repo_full_name="private/connected", status="active", installation_id=7
                ),
            ]
        ),
    )
    monkeypatch.setattr(
        module,
        "get_app_installation",
        AsyncMock(return_value=SimpleNamespace(repo_full_names=["private/connected"])),
    )
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
    assert "private/connected" in str(view["blocks"])
    assert "private/unconnected" not in str(view["blocks"])
    assert "1 other repo(s) not connected yet" in str(view["blocks"])
    assert view["private_metadata"]
    assert "private/connected" not in str(payload)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["cancel", "approve"])
async def test_ephemeral_requester_buttons_use_container_ts(
    monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    request_id, tenant_id, account_id = (uuid.uuid4() for _ in range(3))
    request = SimpleNamespace(
        id=request_id,
        tenant_id=tenant_id,
        parent_channel_id="C1",
        thread_id="123.0",
        requester_account_id=account_id,
        requester_platform_user_id="U1",
        status="open",
        agent_name="Helper",
    )
    delivery = SimpleNamespace(message_id="123.456", dismissed_at=None)
    client = SimpleNamespace(chat_postEphemeral=AsyncMock(return_value={"message_ts": "123.789"}))
    monkeypatch.setattr(module, "derive_tenant_uuid", lambda **_kw: tenant_id)
    monkeypatch.setattr(module, "resolve_web_client", AsyncMock(return_value=client))
    monkeypatch.setattr(module, "lookup_request", AsyncMock(return_value=request))
    monkeypatch.setattr(
        module,
        "find_platform_principal",
        AsyncMock(return_value=SimpleNamespace(account_id=account_id)),
    )
    monkeypatch.setattr(module, "get_delivery", AsyncMock(return_value=delivery))
    monkeypatch.setattr(module, "resolve_is_admin", AsyncMock(return_value=True))
    monkeypatch.setattr(module, "cancel_request", AsyncMock(return_value=True))
    monkeypatch.setattr(module, "approve_connected_request", AsyncMock(return_value=True))
    monkeypatch.setattr(module, "lock_delivery_slot", AsyncMock())
    record = AsyncMock(return_value=True)
    monkeypatch.setattr(module, "record_reposted_requester_card", record)
    runtime = SimpleNamespace(sessionmaker=_Sessions())
    payload = {
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "channel": {"id": "C1"},
        "container": {"message_ts": "123.456"},
        "actions": [{"action_id": "github_request__decision", "value": f"{request_id}:{decision}"}],
    }
    await module.handle_action(runtime, payload)  # type: ignore[arg-type]
    if decision == "cancel":
        module.cancel_request.assert_awaited_once()  # type: ignore[attr-defined]
    else:
        module.approve_connected_request.assert_awaited_once()  # type: ignore[attr-defined]
    # Re-posting the requester card replaces the bound timestamp.
    await module.update_requester_card(
        runtime,
        client,
        tenant_id=tenant_id,
        request_id=request_id,
        text="Your GitHub link stopped working. Link again?",
        can_cancel=True,
        link_url="https://example.test/link",
    )  # type: ignore[arg-type]
    assert record.await_args.kwargs["message_id"] == "123.789"
    blocks = client.chat_postEphemeral.await_args.kwargs["blocks"]
    assert "Get a new link" in str(blocks)
    assert "Cancel request" in str(blocks)


def test_slack_review_modal_escapes_only_mrkdwn_control_characters() -> None:
    view = module._review_modal(  # type: ignore[attr-defined]
        uuid.uuid4(),
        channel_id="C1",
        message_id="1",
        agent_name='A" & <B>',
        repo_names=["owner/repo"],
        other_repo_count=1,
        ability="Read only",
    )
    text = view["blocks"][0]["text"]["text"]
    assert 'A" &amp; &lt;B&gt;' in text


@pytest.mark.asyncio
async def test_slack_review_modal_connect_button_has_link_emoji() -> None:
    client = SimpleNamespace(views_update=AsyncMock())
    await module._replace_modal(  # type: ignore[attr-defined]
        client,
        {"view": {"id": "V1", "private_metadata": "{}"}},
        "Ready.",
        url="https://example.test/link",
        card=build_connect_card(
            agent_name="ResearchBot",
            identity_enabled=False,
            avatar_url=None,
            public_base_url="https://mcp.test",
        ),
    )
    view = client.views_update.await_args.kwargs["view"]
    assert next(block for block in view["blocks"] if block["type"] == "actions")["elements"][0][
        "text"
    ] == {
        "type": "plain_text",
        "text": "🔗 Connect GitHub",
        "emoji": True,
    }


@pytest.mark.asyncio
async def test_ephemeral_try_again_is_a_link_button_without_message_payload() -> None:
    request_id = uuid.uuid4()
    blocks = _slack_blocks(
        RequestCard("Your GitHub link expired.", "Try again"),
        request_id=request_id,
        link_url="https://example.test/link",
    )
    assert "Try again" in str(blocks)
    assert "https://example.test/link" in str(blocks)
    payload = {
        "container": {"message_ts": "123.456"},
        "actions": [{"action_id": "github_request__link"}],
    }
    # Slack opens the URL itself; this callback has no state mutation.
    await module.handle_action(SimpleNamespace(), payload)  # type: ignore[arg-type]
