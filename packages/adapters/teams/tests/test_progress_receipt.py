"""Initial receipts come from real Teams SDK activity serialization and ACKs."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from daimon.adapters.teams.lifecycle import TeamsTurnLifecycle
from daimon.adapters.teams.progress_receipt import InitialProgressReceipt
from daimon.core.turn.state import TextBlock, TurnState
from microsoft_teams.api import (
    Account,
    ApiClient,
    ConversationAccount,
    ConversationReference,
    MessageActivityInput,
    SentActivity,
)
from microsoft_teams.apps.activity_send import send_or_update_activity
from microsoft_teams.common import Client, ClientOptions
from pydantic import ValidationError

from .conftest import CONVERSATION_ID, SERVICE_URL, TeamsApiFake, assert_teams_accepts


class SDKSender:
    """The same SDK send helper App.send uses, with all HTTP intercepted."""

    def __init__(self) -> None:
        self.wire = TeamsApiFake()
        client = Client(ClientOptions(token="offline-token", headers={}))
        client.use(self.wire)
        self.api = ApiClient(SERVICE_URL, client)

    async def send(
        self,
        conversation_id: str,
        activity: MessageActivityInput,
        *,
        service_url: str | None,
    ) -> SentActivity:
        return await send_or_update_activity(
            self.api,
            activity,
            ConversationReference(
                channel_id="msteams",
                service_url=service_url or SERVICE_URL,
                bot=Account(id="offline-bot"),
                conversation=ConversationAccount(id=conversation_id),
            ),
        )


def lifecycle(
    sender: SDKSender,
    receipts: list[InitialProgressReceipt] | None,
    **kwargs: Any,
) -> TeamsTurnLifecycle:
    return TeamsTurnLifecycle(
        sender=sender,
        conversation_id=CONVERSATION_ID,
        service_url=SERVICE_URL,
        cancel_key="owned-cancel-key",
        clock=lambda: 100.0,
        on_initial_progress=receipts.append if receipts is not None else None,
        **kwargs,
    )


async def test_receipt_joins_actual_wire_and_public_card_reference() -> None:
    sender = SDKSender()
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(sender, receipts)
    await adapter.post_initial()
    assert len(receipts) == len(sender.wire.activity_requests) == 1
    receipt, post = receipts[0], sender.wire.activity_requests[0]
    assert_teams_accepts(post)
    assert receipt.message_id == adapter.message_id == "m-1"
    assert receipt.destination_id == CONVERSATION_ID and receipt.destination_id in post.url
    assert receipt.thread_id is None and receipt.operation == "send" and post.method == "POST"
    assert receipt.observed_s == 100.0 and receipt.phase == "initial"
    assert json.loads(receipt.rendered_json) == post.body
    assert "owned-cancel-key" in receipt.rendered_json
    assert InitialProgressReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    assert (
        not {"session_id", "root_turn_id", "accepted", "running"}
        & InitialProgressReceipt.model_fields.keys()
    )


async def test_render_terminal_do_not_reissue_initial_receipt() -> None:
    sender = SDKSender()
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(sender, receipts)
    await adapter.post_initial()
    frozen = receipts[0].model_dump_json()
    await adapter.on_render(TurnState())
    await adapter.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="answer")]))
    assert len(receipts) == 1 and receipts[0].model_dump_json() == frozen
    assert len(sender.wire.activity_requests) > 1


async def test_unprompted_initial_is_suppressed() -> None:
    sender = SDKSender()
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(sender, receipts, unprompted=True)
    await adapter.post_initial()
    assert not receipts and not sender.wire.requests and adapter.message_id is None


async def test_no_receipt_for_rejected_send(monkeypatch: pytest.MonkeyPatch) -> None:
    receipts: list[InitialProgressReceipt] = []

    async def reject(*args: object) -> httpx.Response:
        request = httpx.Request("POST", SERVICE_URL)
        response = httpx.Response(403, json={"error": "offline refusal"}, request=request)
        response.raise_for_status()
        return response

    monkeypatch.setattr(TeamsApiFake, "send", reject)
    sender = SDKSender()
    adapter = lifecycle(sender, receipts)
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.post_initial()
    assert not receipts and adapter.message_id is None


async def test_opt_in_does_not_change_serialized_request() -> None:
    sender = SDKSender()
    await lifecycle(sender, None).post_initial()
    first = sender.wire.activity_requests[0].body
    await lifecycle(sender, []).post_initial()
    assert sender.wire.activity_requests[1].body == first


async def test_production_posting_mutation_removes_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken_initial(self: TeamsTurnLifecycle) -> None:
        pass

    monkeypatch.setattr(TeamsTurnLifecycle, "post_initial", broken_initial)
    with pytest.raises(AssertionError):
        await test_receipt_joins_actual_wire_and_public_card_reference()


async def test_adopted_card_receipt_records_real_edit() -> None:
    sender = SDKSender()
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(sender, receipts, adopt_message_id="owned-card")
    await adapter.post_initial()
    receipt, post = receipts[0], sender.wire.activity_requests[0]
    assert receipt.message_id == adapter.message_id == "owned-card"
    assert receipt.operation == "edit" and post.method == "PUT"
    assert post.url.endswith("/activities/owned-card")
    assert_teams_accepts(post)
    assert json.loads(receipt.rendered_json) == post.body


@pytest.mark.parametrize(
    "field,value", [("message_id", ""), ("observed_s", float("nan")), ("root_turn_id", "made-up")]
)
async def test_receipt_is_closed_and_refuses_invalid_json(field: str, value: object) -> None:
    receipts: list[InitialProgressReceipt] = []
    await lifecycle(SDKSender(), receipts).post_initial()
    data = receipts[0].model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        InitialProgressReceipt.model_validate_json(json.dumps(data))


async def test_receipt_waits_for_actual_sdk_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    receipts: list[InitialProgressReceipt] = []
    original = TeamsApiFake.send

    async def hold(*args: Any, **kwargs: Any) -> httpx.Response:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(TeamsApiFake, "send", hold)
    sender = SDKSender()
    adapter = lifecycle(sender, receipts)
    task = asyncio.create_task(adapter.post_initial())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not receipts and adapter.message_id is None and not sender.wire.requests
        release.set()
        await asyncio.wait_for(task, 5)
        assert len(receipts) == len(sender.wire.activity_requests) == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
