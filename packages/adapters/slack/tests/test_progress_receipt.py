"""Actual Slack SDK posts supply initial card receipt references and payloads."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol, cast
from uuid import UUID

import pytest
import yarl
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.adapters.slack.progress_receipt import InitialProgressReceipt
from daimon.core.turn.state import TextBlock, TurnState
from pydantic import ValidationError
from slack_sdk.errors import SlackApiError

from .conftest import FakeSlackWebClient

POST = yarl.URL("https://slack.com/api/chat.postMessage")
UPDATE = yarl.URL("https://slack.com/api/chat.update")


class RequestCall(Protocol):
    kwargs: dict[str, Any]


def post_payload(fake: FakeSlackWebClient, index: int = 0) -> dict[str, Any]:
    calls = cast(list[RequestCall], fake.mock.requests[("POST", POST)])
    return cast(dict[str, Any], calls[index].kwargs["json"])


def register(ts: str, cancel: asyncio.Event, author_id: str) -> None:
    pass


def lifecycle(
    fake: FakeSlackWebClient,
    receipts: list[InitialProgressReceipt] | None,
    **kwargs: Any,
) -> SlackTurnLifecycle:
    return SlackTurnLifecycle(
        client=fake.client,
        channel="C_OWNED",
        thread_ts="111.000",
        cancel=asyncio.Event(),
        author_id="U_OWNER",
        agent_name="Daimon",
        model_id="offline",
        register=register,
        deregister=lambda ts: None,
        clock=lambda: 100.0,
        intent_id=UUID(int=1),
        on_initial_progress=receipts.append if receipts is not None else None,
        **kwargs,
    )


async def test_receipt_joins_actual_wire_and_public_card_reference(
    fake_slack_web_client: FakeSlackWebClient,
) -> None:
    fake = fake_slack_web_client
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(fake, receipts)
    await adapter.post_initial()
    assert len(receipts) == 1
    receipt = receipts[0]
    wire = post_payload(fake)
    assert receipt.message_id == adapter.status_ts
    assert receipt.destination_id == wire["channel"] == "C_OWNED"
    assert receipt.thread_id == wire["thread_ts"] == "111.000"
    assert receipt.operation == "send" and receipt.observed_s == 100.0
    assert json.loads(receipt.rendered_json) == {"blocks": wire["blocks"], "text": wire["text"]}
    assert UUID(int=1).hex in receipt.rendered_json
    assert receipt.phase == "initial"
    assert InitialProgressReceipt.model_validate_json(receipt.model_dump_json()) == receipt
    assert (
        not {"session_id", "root_turn_id", "accepted", "running"}
        & InitialProgressReceipt.model_fields.keys()
    )


async def test_render_terminal_and_debounce_do_not_reissue_initial_receipt(
    fake_slack_web_client: FakeSlackWebClient,
) -> None:
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(fake_slack_web_client, receipts)
    await adapter.post_initial()
    frozen = receipts[0].model_dump_json()
    await adapter.post_initial()
    await adapter.on_render(TurnState())
    await adapter.on_terminal_success(TurnState(content=[TextBlock(kind="text", text="answer")]))
    assert len(receipts) == 1 and receipts[0].model_dump_json() == frozen
    assert fake_slack_web_client.mock.requests[("POST", UPDATE)]


async def test_no_receipt_for_rejected_send(fake_slack_web_client: FakeSlackWebClient) -> None:
    fake = fake_slack_web_client
    fake.mock.clear()
    fake.mock.post(POST, payload={"ok": False, "error": "invalid_auth"})  # pyright: ignore[reportUnknownMemberType]  # SDK mock stub has Pattern[Unknown].
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(fake, receipts)
    with pytest.raises(SlackApiError):
        await adapter.post_initial()
    assert not receipts and adapter.status_ts is None


async def test_opt_in_does_not_change_serialized_request(
    fake_slack_web_client: FakeSlackWebClient,
) -> None:
    fake = fake_slack_web_client
    await lifecycle(fake, None).post_initial()
    first = post_payload(fake)
    await lifecycle(fake, []).post_initial()
    assert post_payload(fake, 1) == first


async def test_production_posting_mutation_removes_receipt(
    fake_slack_web_client: FakeSlackWebClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_flush(self: SlackTurnLifecycle, *, initial: bool = False) -> None:
        pass

    monkeypatch.setattr(SlackTurnLifecycle, "_maybe_flush", broken_flush)
    with pytest.raises(AssertionError):
        await test_receipt_joins_actual_wire_and_public_card_reference(fake_slack_web_client)


async def test_adopted_card_receipt_records_real_edit(
    fake_slack_web_client: FakeSlackWebClient,
) -> None:
    receipts: list[InitialProgressReceipt] = []
    adapter = lifecycle(fake_slack_web_client, receipts, adopt_status_ts="owned-card")
    await adapter.post_initial()
    assert len(receipts) == 1 and receipts[0].operation == "edit"
    assert receipts[0].message_id == adapter.status_ts == "owned-card"
    assert len(fake_slack_web_client.mock.requests[("POST", UPDATE)]) == 1


@pytest.mark.parametrize(
    "field,value", [("message_id", ""), ("observed_s", float("nan")), ("root_turn_id", "made-up")]
)
async def test_receipt_is_closed_and_refuses_invalid_json(
    fake_slack_web_client: FakeSlackWebClient,
    field: str,
    value: object,
) -> None:
    receipts: list[InitialProgressReceipt] = []
    await lifecycle(fake_slack_web_client, receipts).post_initial()
    data = receipts[0].model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        InitialProgressReceipt.model_validate_json(json.dumps(data))


async def test_receipt_waits_for_actual_sdk_ack(fake_slack_web_client: FakeSlackWebClient) -> None:
    from aioresponses import CallbackResult

    entered, release = asyncio.Event(), asyncio.Event()
    receipts: list[InitialProgressReceipt] = []

    async def hold(*args: Any, **kwargs: Any) -> CallbackResult:
        entered.set()
        await release.wait()
        return CallbackResult(payload={"ok": True, "ts": "owned-ack"})

    fake_slack_web_client.mock.clear()
    fake_slack_web_client.mock.post(POST, callback=hold)  # pyright: ignore[reportUnknownMemberType]  # SDK mock stub has Pattern[Unknown].
    adapter = lifecycle(fake_slack_web_client, receipts)
    task = asyncio.create_task(adapter.post_initial())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not receipts and adapter.status_ts is None
        release.set()
        await asyncio.wait_for(task, 5)
        assert len(receipts) == 1 and receipts[0].message_id == "owned-ack"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
