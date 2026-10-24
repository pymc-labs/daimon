"""Observe real Slack/Teams lifecycles at their offline SDK delivery boundaries."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any, cast

import aiohttp
import httpx
import pytest
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.adapters.teams.lifecycle import TeamsTurnLifecycle
from daimon.core.config import RecruitmentSettings
from daimon.core.turn import run_turn
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.privacy import TurnPrivacy
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from daimon.testing.platform_surface import PlatformSurfaceCapture
from daimon.testing.slack_surface import SlackSurfaceHarness
from daimon.testing.teams_surface import TeamsSurfaceHarness
from daimon.testing.turn_router import build_turn_router
from microsoft_teams.api import MessageActivityInput
from mux.testing.isolated_sdk import (
    SDK_ENV_VARS,
    AmbientSDKConfigurationError,
    build_async_anthropic,
)
from pydantic import ValidationError
from slack_sdk.models.blocks import SectionBlock

type Lifecycle = SlackTurnLifecycle | TeamsTurnLifecycle
type Harness = SlackSurfaceHarness | TeamsSurfaceHarness


class Clock:
    now = 10.0

    def __call__(self) -> float:
        return self.now


def harness(platform: str, **kwargs: Any) -> Harness:
    cls = SlackSurfaceHarness if platform == "slack" else TeamsSurfaceHarness
    return cls(evidence_id="qa:1", session_id="sess:1", root_turn_id="root:1", **kwargs)


def state(text: str) -> TurnState:
    return TurnState(content=[TextBlock(kind="text", text=text)])


async def finish(lc: Lifecycle, text: str) -> None:
    await lc.post_initial()
    await lc.on_terminal_success(state(text))


def body(capture: PlatformSurfaceCapture) -> str:
    return "\n".join(text for message in capture.messages for text in message.body_texts)


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_real_final_posts_replace_drafts_and_ignore_headless_return(platform: str) -> None:
    h = harness(platform, ask_human=True)

    async def invoke(lc: Lifecycle) -> str:
        await finish(lc, "actual posted answer")
        return "HEADLESS_SECRET"

    capture = await h.run(invoke)
    assert capture.text_capture_complete and capture.post_capture_complete
    assert body(capture) == "actual posted answer"
    assert "HEADLESS_SECRET" not in capture.model_dump_json()
    assert capture.events[0].operation == "send" and capture.events[1].operation == "edit"
    assert "Working on it" in " ".join(capture.events[0].message.card_texts)
    assert not any("Working on it" in text for m in capture.messages for text in m.card_texts)
    assert capture.messages[0].message_id == capture.events[0].message.message_id
    assert capture.events[0].message.control_labels == ("Stop",)
    assert capture.messages[-1].control_labels == ("👍", "👎", "🙋 Ask a person")
    if platform == "teams":
        assert len(capture.messages) == 2
        assert not capture.messages[0].card_texts and not capture.messages[0].control_labels
        assert "attachments" not in json.loads(capture.messages[0].payload_json)
    else:
        assert len(capture.messages) == 1
        assert isinstance(h, SlackSurfaceHarness) and not h.registered
    assert PlatformSurfaceCapture.model_validate_json(capture.model_dump_json()) == capture
    with pytest.raises(ValidationError, match="frozen"):
        capture.messages[0].body_texts = ("tampered",)


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_real_draft_is_clipped_flattened_and_not_final_answer(platform: str) -> None:
    clock = Clock()
    h = harness(platform, clock=clock)
    draft = "*markdown*\n" + "x" * 6000 + "HIDDEN_DRAFT_TAIL"

    async def invoke(lc: Lifecycle) -> None:
        await lc.post_initial()
        await lc.on_sse_event(
            cast(Any, SimpleNamespace(type="agent.message", content=[SimpleNamespace(text=draft)]))
        )
        assert len(h.recorder.events) == 1, "SSE reduction itself never posts"
        clock.now += 6.0
        await lc.on_render(state(draft))
        await lc.on_terminal_success(state("final"))

    capture = await h.run(invoke)
    preview = " ".join(capture.events[1].message.card_texts)
    assert "markdown" in preview and "HIDDEN_DRAFT_TAIL" not in preview
    assert draft not in preview and "\n" not in preview
    assert "*markdown* " in preview, "retain the exact posted draft markup"
    assert body(capture) == "final" and capture.text_capture_complete
    assert all("markdown" not in text for m in capture.messages for text in m.card_texts)


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_real_long_fences_preserve_order_and_put_controls_last(platform: str) -> None:
    text = "```python\n" + "print('hello')\n" * 2000 + "```"
    capture = await harness(platform).run(lambda lc: finish(lc, text))
    answers = [message for message in capture.messages if message.body_texts]
    assert len(answers) > 2 and capture.text_capture_complete
    assert [m.order for m in capture.messages] == list(range(len(capture.messages)))
    assert all("\n".join(m.body_texts).count("```") == 2 for m in answers)
    assert sum("\n".join(m.body_texts).count("print('hello')") for m in answers) == 2000
    assert all(not m.control_labels for m in capture.messages[:-1])
    assert capture.messages[-1].control_labels == ("👍", "👎")
    if platform == "teams":
        assert all(len(m.payload_json.encode()) < 28 * 1024 for m in answers)


async def test_slack_full_markdown_block_is_distinct_from_clipped_notification_fallback() -> None:
    text = "x" * 5000 + "VISIBLE_TAIL"
    h = harness("slack")
    capture = await h.run(lambda lc: finish(lc, text))
    assert body(capture) == text
    assert len(capture.messages[0].fallback_text) == 3000
    assert "VISIBLE_TAIL" not in capture.messages[0].fallback_text
    assert "VISIBLE_TAIL" in json.loads(capture.events[-1].request_json)["blocks"][0]["text"]


async def test_slack_native_table_cells_are_observed_in_actual_delivery_order() -> None:
    text = "Before.\n\n| Name | Value |\n| --- | --- |\n| Alpha | 42 |\n\nAfter."
    capture = await harness("slack", render_tables=True).run(lambda lc: finish(lc, text))
    assert capture.text_capture_complete
    assert [m.body_texts for m in capture.messages] == [
        ("Before.\n\n",),
        ("Name", "Value", "Alpha", "42"),
        ("\nAfter.",),
    ]
    assert json.loads(capture.messages[1].payload_json)["blocks"][0]["type"] == "table"
    assert not capture.messages[1].control_labels and capture.messages[-1].control_labels


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_completion_notification_posts_fresh_answer_and_retires_working_card(
    platform: str,
) -> None:
    h = harness(
        platform,
        **({"notify_on_completion": True} if platform == "slack" else {"completion_ping": True}),
    )
    capture = await h.run(lambda lc: finish(lc, "answer"))
    answer = next(m for m in capture.messages if m.body_texts)
    assert answer.message_id != capture.events[0].message.message_id
    assert "answer" in body(capture) and capture.text_capture_complete
    assert not any("Working on it" in t for m in capture.messages for t in m.card_texts)
    assert "Stop" not in [label for m in capture.messages for label in m.control_labels]
    if platform == "slack":
        assert "<@U_QA>" in body(capture)
    else:
        assert capture.messages[0].card_texts == ("Done.",)


@pytest.mark.parametrize("platform", ["slack", "teams"])
@pytest.mark.parametrize("tool_only", [False, True])
async def test_empty_and_tool_only_turns_keep_real_notices_and_control_policy(
    platform: str, tool_only: bool
) -> None:
    h = harness(platform)
    terminal = TurnState(
        content=[
            ToolUseBlock(kind="tool_use", id="tool:1", type="agent.tool_use", name="bash", input={})
        ]
        if tool_only
        else []
    )

    async def invoke(lc: Lifecycle) -> None:
        await lc.post_initial()
        await lc.on_terminal_success(terminal)

    capture = await h.run(invoke)
    assert capture.text_capture_complete and not body(capture)
    assert capture.messages[0].card_texts
    labels = capture.messages[0].control_labels
    assert labels == (("👍", "👎") if tool_only else ())
    assert "Stop" not in labels


async def test_teams_unprompted_empty_turn_posts_nothing() -> None:
    h = TeamsSurfaceHarness(
        evidence_id="qa:1", session_id="sess:1", root_turn_id="root:1", unprompted=True
    )

    async def invoke(lc: TeamsTurnLifecycle) -> None:
        await lc.post_initial()
        await lc.on_terminal_success(TurnState())

    capture = await h.run(invoke)
    assert capture.text_capture_complete and capture.events == capture.messages == ()


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_failure_card_comes_from_adapter_and_hides_private_exception(platform: str) -> None:
    h = harness(platform)

    async def invoke(lc: Lifecycle) -> None:
        await lc.post_initial()
        await lc.on_terminal_failure(TurnState(), RuntimeError("PRIVATE_ERROR_SECRET"))

    capture = await h.run(invoke)
    assert capture.text_capture_complete and not body(capture)
    assert "Something went wrong" in " ".join(capture.messages[0].card_texts)
    assert "PRIVATE_ERROR_SECRET" not in capture.model_dump_json()


@pytest.mark.parametrize("platform", ["slack", "teams"])
@pytest.mark.parametrize("failed_part", ["answer", "overflow", "controls"])
async def test_swallowed_delivery_errors_cannot_claim_complete_capture(
    platform: str, failed_part: str
) -> None:
    h = harness(platform)

    async def invoke(lc: Lifecycle) -> None:
        await lc.post_initial()
        if isinstance(h, SlackSurfaceHarness):
            # Slack controls ride the last answer block; a failed overflow loses both.
            method = "chat.update" if failed_part == "answer" else "chat.postMessage"
            h.client.fail_next(method, aiohttp.ClientConnectionError("offline failure"))
        else:
            call = 1 if failed_part == "answer" else 2
            h.sender.fail_on(call, httpx.ConnectError("offline failure"))
        text = (
            "x" * 30000
            if failed_part == "overflow" or (platform == "slack" and failed_part == "controls")
            else "answer"
        )
        await lc.on_terminal_success(state(text))

    capture = await h.run(invoke)
    assert not capture.post_capture_complete and not capture.text_capture_complete
    assert capture.delivery_failures == 1
    if failed_part == "answer":
        assert "answer" not in body(capture)
    else:
        assert body(capture), "retain the answer portion that actually landed"


@pytest.mark.parametrize("platform", ["slack", "teams"])
@pytest.mark.parametrize("count", [0, 2])
async def test_missing_or_duplicate_terminal_fails_closed(platform: str, count: int) -> None:
    h = harness(platform)

    async def invoke(lc: Lifecycle) -> str:
        await lc.post_initial()
        for _ in range(count):
            await lc.on_terminal_success(state("answer"))
        return "HEADLESS_SECRET"

    capture = await h.run(invoke)
    assert not capture.post_capture_complete and not capture.text_capture_complete
    assert "HEADLESS_SECRET" not in capture.model_dump_json()
    with pytest.raises(ValueError, match="single-use"):
        await h.run(invoke)


@pytest.mark.parametrize("platform", ["slack", "teams"])
@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_exception_after_delivery_preserves_incomplete_frozen_receipt(
    platform: str, error: BaseException
) -> None:
    h = harness(platform)

    async def invoke(lc: Lifecycle) -> None:
        await finish(lc, "actually posted")
        raise error

    with pytest.raises(type(error)):
        await h.run(invoke)
    capture = h.snapshot()
    assert body(capture) == "actually posted" and not capture.text_capture_complete
    assert not capture.post_capture_complete


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_close_freezes_receipt_even_if_later_posts_arrive(platform: str) -> None:
    h = harness(platform)
    capture = await h.run(lambda lc: finish(lc, "original"))
    if isinstance(h, SlackSurfaceHarness):
        await h.client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel="C_QA", thread_ts="1699999999.000001", text="outside window"
        )
    else:
        await h.sender.send(
            "19:qa-thread",
            MessageActivityInput(text="outside window"),
            service_url="https://teams.invalid/",
        )
    assert h.snapshot() == capture and "outside window" not in capture.model_dump_json()


async def test_slack_sdk_objects_serialize_before_capture_and_payloads_cannot_mutate_history() -> (
    None
):
    h = SlackSurfaceHarness(evidence_id="qa:1", session_id="sess:1", root_turn_id="root:1")
    blocks = [SectionBlock(text="SDK card")]
    response = await h.client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
        channel="C_QA", thread_ts="1699999999.000001", text="fallback", blocks=blocks
    )
    event = h.snapshot().events[0]
    assert event.message.card_texts == ("SDK card",)
    assert json.loads(event.request_json)["blocks"][0]["text"]["text"] == "SDK card"
    response_data = cast(dict[str, Any], response.data)  # pyright: ignore[reportUnknownMemberType]
    response_data["message"]["text"] = "response mutation"
    assert "response mutation" not in h.snapshot().model_dump_json()
    await h.client.chat_delete(channel="C_QA", ts=response_data["ts"])  # pyright: ignore[reportUnknownMemberType]
    assert h.snapshot().messages == () and h.snapshot().events[-1].operation == "delete"
    assert h.snapshot().events[0] == event


@pytest.mark.parametrize(
    "blocks",
    [
        [
            {
                "type": "image",
                "image_url": "https://example.invalid/image",
                "alt_text": "unseen image",
            }
        ],
        [
            {
                "type": "context",
                "elements": [
                    {
                        "type": "image",
                        "image_url": "https://example.invalid/image",
                        "alt_text": "unseen image",
                    }
                ],
            }
        ],
        [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [{"type": "user", "user_id": "U_UNKNOWN"}],
                    }
                ],
            }
        ],
    ],
)
async def test_slack_unsupported_visual_text_is_an_explicit_gap(
    blocks: list[dict[str, Any]],
) -> None:
    h = SlackSurfaceHarness(evidence_id="qa:1", session_id="sess:1", root_turn_id="root:1")

    async def invoke(lc: SlackTurnLifecycle) -> None:
        await finish(lc, "answer")
        await h.client.chat_postMessage(  # pyright: ignore[reportUnknownMemberType]
            channel="C_QA", thread_ts="1699999999.000001", text="ACCESSIBILITY_ONLY", blocks=blocks
        )

    capture = await h.run(invoke)
    assert capture.post_capture_complete and not capture.text_capture_complete
    assert capture.messages[-1].opaque_text and not capture.messages[-1].body_texts
    assert "ACCESSIBILITY_ONLY" not in body(capture)


async def test_teams_unobserved_attachment_is_an_explicit_gap() -> None:
    h = TeamsSurfaceHarness(evidence_id="qa:1", session_id="sess:1", root_turn_id="root:1")

    async def invoke(lc: TeamsTurnLifecycle) -> None:
        await finish(lc, "answer")
        activity = MessageActivityInput.model_validate(
            {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "image/png",
                        "contentUrl": "https://example.invalid/image",
                        "name": "unseen",
                    }
                ],
            }
        )
        await h.sender.send("19:qa-thread", activity, service_url="https://teams.invalid/")

    capture = await h.run(invoke)
    assert capture.post_capture_complete and not capture.text_capture_complete
    assert capture.messages[-1].opaque_text


async def test_fake_clients_refuse_unsupported_operations_and_unowned_destinations() -> None:
    slack = SlackSurfaceHarness(evidence_id="qa:1", session_id="sess:1", root_turn_id="root:1")
    slack_calls: list[tuple[str, dict[str, Any]]] = [
        ("users.info", {}),
        ("chat.postMessage", {"channel": "OTHER", "thread_ts": "1699999999.000001"}),
        ("chat.postMessage", {"channel": "C_QA", "thread_ts": "OTHER"}),
        ("chat.update", {"channel": "C_QA", "ts": "unowned", "blocks": []}),
    ]
    for method, payload in slack_calls:
        with pytest.raises((ValueError, KeyError)):
            await slack.client.api_call(method, json=payload)
    assert slack.snapshot().delivery_failures == 4 and not slack.snapshot().events
    teams = TeamsSurfaceHarness(evidence_id="qa:1", session_id="sess:1", root_turn_id="root:1")
    for destination, activity, url in [
        ("OTHER", MessageActivityInput(text="wrong"), "https://teams.invalid/"),
        ("19:qa-thread", MessageActivityInput(text="wrong"), "https://OTHER.invalid/"),
        (
            "19:qa-thread",
            MessageActivityInput(text="wrong", id="unowned"),
            "https://teams.invalid/",
        ),
    ]:
        with pytest.raises(ValueError):
            await teams.sender.send(destination, activity, service_url=url)
    assert teams.snapshot().delivery_failures == 3 and not teams.snapshot().events


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_actual_host_driver_pumps_scripted_provider_into_real_platform_posts(
    platform: str,
) -> None:
    h = harness(platform)
    bodies: list[dict[str, Any]] = []
    hits: list[str] = []
    router = build_turn_router(
        "00000000-0000-0000-0000-000000000001",
        session_id="sess:1",
        agent_text="provider answer",
        usage_event_id=None,
        sent_event_bodies=bodies,
        stream_hits=hits,
    )
    async with build_async_anthropic(
        transport=httpx.MockTransport(router.dispatch),
        api_key="test",
        base_url="https://api.anthropic.com",
        headers={},
        max_retries=0,
    ) as provider:

        async def invoke(lc: Lifecycle) -> TurnState:
            await lc.post_initial()
            return await run_turn(
                privacy=TurnPrivacy(platform, RecruitmentSettings()),
                anthropic=provider,
                session_id="sess:1",
                user_message="qa input",
                lifecycle=lc,
                cancel=asyncio.Event(),
                path="legacy",
                billing=BillingExempt(reason="cli-operator-run"),
            )

        capture = await h.run(invoke)
    if not capture.text_capture_complete or body(capture) != "provider answer":
        raise AssertionError("host driver did not deliver its real platform answer")
    if hits != ["sess:1"] or len(bodies) != 1:
        raise AssertionError("host driver changed its scripted request count")
    if capture.session_id != "sess:1" or capture.root_turn_id != "root:1":
        raise AssertionError("platform capture changed its owned session/root")
    if not provider.is_closed():
        raise AssertionError("isolated provider HTTP client was not closed")


async def test_replaced_opaque_slack_draft_still_prevents_complete_text_coverage() -> None:
    h = SlackSurfaceHarness(evidence_id="qa:1", session_id="sess:1", root_turn_id="root:1")

    async def invoke(lc: SlackTurnLifecycle) -> None:
        await lc.post_initial()
        ts = h.recorder.events[0].message.message_id
        await h.client.api_call(
            "chat.update",
            json={
                "channel": "C_QA",
                "ts": ts,
                "blocks": [
                    {
                        "type": "image",
                        "image_url": "https://example.invalid/image",
                        "alt_text": "unobserved",
                    }
                ],
            },
        )
        await lc.on_terminal_success(state("answer"))

    capture = await h.run(invoke)
    assert capture.post_capture_complete and not capture.text_capture_complete
    assert body(capture) == "answer" and not any(m.opaque_text for m in capture.messages)
    assert capture.events[1].message.opaque_text


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_monotonic_window_and_required_identity_fail_before_dispatch(platform: str) -> None:
    cls = SlackSurfaceHarness if platform == "slack" else TeamsSurfaceHarness
    with pytest.raises(ValidationError):
        cls(evidence_id="", session_id="sess:1", root_turn_id="root:1")
    clock = Clock()
    h = harness(platform, clock=clock)

    async def invoke(lc: Lifecycle) -> None:
        await finish(lc, "answer")
        clock.now = 1.0

    with pytest.raises(ValidationError, match="window"):
        await h.run(invoke)


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_independent_captures_never_reuse_post_ids(platform: str) -> None:
    first = await harness(platform).run(lambda lc: finish(lc, "first"))
    cls = SlackSurfaceHarness if platform == "slack" else TeamsSurfaceHarness
    second = await cls(evidence_id="qa:2", session_id="sess:1", root_turn_id="root:2").run(
        lambda lc: finish(lc, "second")
    )
    assert not {m.message_id for m in first.messages}.intersection(
        m.message_id for m in second.messages
    )
    assert not {e.evidence_id for e in first.events}.intersection(
        e.evidence_id for e in second.events
    )


async def test_slack_table_rejection_records_actual_fallback_and_incomplete_delivery() -> None:
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_slack_response import AsyncSlackResponse

    h = SlackSurfaceHarness(
        evidence_id="qa:1", session_id="sess:1", root_turn_id="root:1", render_tables=True
    )
    text = "| Name | Value |\n| --- | --- |\n| Alpha | 42 |"

    async def invoke(lc: SlackTurnLifecycle) -> None:
        await lc.post_initial()
        response = AsyncSlackResponse(
            client=h.client,
            http_verb="POST",
            api_url="offline://slack/chat.update",
            req_args={},
            data={"ok": False, "error": "invalid_blocks"},
            headers={},
            status_code=200,
        )
        h.client.fail_next("chat.update", SlackApiError("table rejected", response))
        await lc.on_terminal_success(state(text))

    capture = await h.run(invoke)
    assert capture.delivery_failures == 1 and not capture.text_capture_complete
    assert "Alpha" in body(capture) and "42" in body(capture)
    assert json.loads(capture.messages[0].payload_json)["blocks"][0]["type"] == "markdown"


@pytest.mark.parametrize("call", [2, 3])
async def test_teams_lost_retirement_or_controls_leaves_incomplete_capture(call: int) -> None:
    h = TeamsSurfaceHarness(
        evidence_id="qa:1", session_id="sess:1", root_turn_id="root:1", completion_ping=True
    )

    async def invoke(lc: TeamsTurnLifecycle) -> None:
        await lc.post_initial()
        h.sender.fail_on(call, httpx.ConnectError("offline failure"))
        await lc.on_terminal_success(state("answer"))

    capture = await h.run(invoke)
    assert body(capture) == "answer" and capture.delivery_failures == 1
    assert not capture.post_capture_complete and not capture.text_capture_complete
    if call == 2:
        assert "Working on it" in " ".join(capture.messages[0].card_texts)
    else:
        assert not capture.messages[-1].control_labels


@pytest.mark.parametrize("platform", ["slack", "teams"])
@pytest.mark.parametrize("name", [n for n in SDK_ENV_VARS if n.startswith("ANTHROPIC_")])
@pytest.mark.parametrize("empty", [False, True])
async def test_actual_platform_host_refuses_hostile_environment_before_http(
    monkeypatch: pytest.MonkeyPatch, platform: str, name: str, empty: bool
) -> None:
    # Exercise the exact host caller above, not just the shared factory. Only
    # invented settings exist here; never fetch or scrub operator values.
    value = (
        ""
        if empty
        else (
            "https://probe.invalid"
            if name == "ANTHROPIC_BASE_URL"
            else "X-Probe-Evil: offline-hostile-setting"
            if name == "ANTHROPIC_CUSTOM_HEADERS"
            else "offline-hostile-setting"
        )
    )
    ambient: dict[str, str] = {}

    def forbidden_http(*args: object, **kwargs: object) -> None:
        raise AssertionError("hostile platform provider reached HTTP allocation")

    with monkeypatch.context() as scoped:
        scoped.setattr(os, "environ", ambient)
        scoped.setenv(name, value)
        scoped.setattr(httpx, "AsyncClient", forbidden_http)
        scoped.setattr(httpx, "Client", forbidden_http)
        with pytest.raises(AmbientSDKConfigurationError) as error:
            await test_actual_host_driver_pumps_scripted_provider_into_real_platform_posts(platform)
        if name not in str(error.value) or (value and value in str(error.value)):
            raise AssertionError("platform refusal exposed a value or lost the setting name")
        if ambient != {name: value}:
            raise AssertionError("platform provider changed the environment")
