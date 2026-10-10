"""Real adapter delivery through real discord.py serialization, with no network."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from typing import Any, cast

import discord
import pytest
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.core.turn import run_turn
from daimon.core.turn.posture import BillingExempt
from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState
from daimon.core.turn.termination import TerminationReason
from daimon.testing.discord_surface import DiscordSurfaceCapture, DiscordSurfaceHarness
from daimon.testing.ma import build_no_retry_anthropic
from daimon.testing.turn_router import build_turn_router


class Clock:
    now = 10.0

    def __call__(self) -> float:
        return self.now


def harness(**kwargs: Any) -> DiscordSurfaceHarness:
    return DiscordSurfaceHarness(
        evidence_id="qa:turn:1", session_id="sess:1", root_turn_id="root:1", **kwargs
    )


def state(text: str) -> TurnState:
    return TurnState(content=[TextBlock(kind="text", text=text)])


async def finish(lc: DiscordTurnLifecycle, text: str) -> None:
    await lc.post_initial()
    await lc.on_terminal_success(state(text))


def content(capture: DiscordSurfaceCapture) -> str:
    return "\n".join(message.content for message in capture.messages if message.content)


async def test_final_content_comes_from_real_delivery_not_callback_return() -> None:
    h = harness()

    async def invoke(lc: DiscordTurnLifecycle) -> str:
        await finish(lc, "actual posted answer")
        return "headless-only SECRET"

    capture = await h.run(invoke)
    assert isinstance(h.lifecycle.message_ref, discord.Message)
    assert isinstance(h.thread, discord.Thread)
    assert capture.text_capture_complete
    assert content(capture) == "actual posted answer"
    assert "SECRET" not in capture.model_dump_json()
    assert [event.operation for event in capture.events] == ["send", "edit", "edit"]
    assert len(capture.messages) == 1
    assert "Working on it" in " ".join(capture.events[0].message.embed_texts)
    assert "Working on it" not in " ".join(capture.messages[0].embed_texts)
    assert capture.messages[0].message_id == capture.events[0].message.message_id
    assert DiscordSurfaceCapture.model_validate_json(capture.model_dump_json()) == capture


async def test_draft_is_escaped_and_clipped_by_adapter_then_retires() -> None:
    clock = Clock()
    h = harness(clock=clock)
    draft = "*markdown* " + "x" * 6000 + "HIDDEN_DRAFT_TAIL"

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        clock.now += 11.0
        await lc.on_sse_event(
            cast(Any, SimpleNamespace(type="agent.message", content=[SimpleNamespace(text=draft)]))
        )
        await lc.on_render(state(draft))
        await lc.on_terminal_success(state("final"))

    capture = await h.run(invoke)
    preview = capture.events[1].message.embed_texts
    assert any(r"\*markdown\*" in item for item in preview)
    assert "HIDDEN_DRAFT_TAIL" not in " ".join(preview)
    assert draft not in " ".join(preview)
    assert content(capture) == "final"
    assert all("markdown" not in item for item in capture.messages[0].embed_texts)
    assert capture.events[0].message.payload_json != capture.events[1].message.payload_json


async def test_long_code_fence_is_split_repaired_and_summary_on_last_message() -> None:
    h = harness()
    text = "```python\n" + "print('hello')\n" * 400 + "```"
    capture = await h.run(lambda lc: finish(lc, text))
    assert len(capture.messages) > 2
    assert [message.order for message in capture.messages] == list(range(len(capture.messages)))
    assert all(len(message.content) <= 2000 for message in capture.messages)
    assert all(message.content.count("```") == 2 for message in capture.messages)
    assert all(not message.embed_texts for message in capture.messages[:-1])
    assert capture.messages[-1].embed_texts
    assert "print('hello')" in content(capture)


async def test_notify_posts_answer_below_card_and_deletes_card() -> None:
    h = harness(notify_on_completion=True)
    capture = await h.run(lambda lc: finish(lc, "answer"))
    assert content(capture) == "<@103>\nanswer"
    assert len(capture.messages) == 1
    assert capture.events[-1].operation == "delete"
    assert capture.messages[0].message_id != capture.events[0].message.message_id
    request = json.loads(capture.events[-2].request_json)
    assert request["allowed_mentions"]["users"] == [103]


async def test_failed_card_deletion_keeps_actual_visible_card() -> None:
    h = harness(notify_on_completion=True)
    h.gateway.fail_next("delete", discord.ClientException("offline delete refused"))
    capture = await h.run(lambda lc: finish(lc, "answer"))
    assert len(capture.messages) == 2
    assert not any(event.operation == "delete" for event in capture.events)
    assert capture.messages[0].embed_texts
    assert capture.messages[1].content == "<@103>\nanswer"


async def test_sealed_answer_is_permanent_once_with_short_narration_only_in_draft() -> None:
    h = harness()
    sealed = "durable answer " * 50
    tool = ToolUseBlock(kind="tool_use", id="t", type="agent.tool_use", name="bash", input={})
    turn = TurnState(
        content=[
            TextBlock(kind="text", text=sealed),
            tool,
            TextBlock(kind="text", text="final recap"),
        ]
    )

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        await lc.on_render(turn)
        await lc.on_render(turn)
        await lc.on_terminal_success(turn)

    capture = await h.run(invoke)
    assert [message.content for message in capture.messages] == ["final recap", sealed]
    assert (
        sum(
            event.operation == "send" and event.message.content == sealed
            for event in capture.events
        )
        == 1
    )


async def test_table_pixels_are_attachment_not_invented_visible_text() -> None:
    h = harness(render_tables=True)
    table = "Before\n\n| Metric | Value |\n| --- | --- |\n| TABLE_ONLY_SECRET | 42 |\n\nAfter"
    capture = await h.run(lambda lc: finish(lc, table))
    assert "TABLE_ONLY_SECRET" not in content(capture)
    assert "Before" in content(capture) and "After" in content(capture)
    assert capture.messages[0].attachment_names == ("table-1.png",)
    assert capture.post_capture_complete and not capture.text_capture_complete
    upload = json.loads(capture.events[-1].request_json)["attachments"][0]
    assert upload["id"] == 0 and "url" not in upload and "size" not in upload
    message = await h.thread.fetch_message(int(capture.messages[0].message_id))
    assert (await message.attachments[0].read()).startswith(b"\x89PNG")


@pytest.mark.parametrize("termination", [None, TerminationReason.INTERRUPTED])
async def test_empty_and_interrupted_turn_capture_adapter_stop_notice(
    termination: TerminationReason | None,
) -> None:
    h = harness()

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        await lc.on_terminal_success(TurnState(termination=termination))

    capture = await h.run(invoke)
    assert content(capture) == "Stopped.\nSend a message to start again."
    assert capture.messages[0].embed_texts == ()


async def test_unprompted_empty_turn_retracts_card_and_has_complete_empty_text() -> None:
    h = harness(unprompted=True)

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        assert h.snapshot().messages == ()
        await lc.on_render(state("draft"))
        await lc.on_terminal_success(TurnState())

    capture = await h.run(invoke)
    assert capture.text_capture_complete
    assert capture.messages == ()
    assert [event.operation for event in capture.events] == ["send", "delete"]
    assert json.loads(capture.events[0].request_json)["flags"] & 4096


async def test_no_terminal_delivery_does_not_complete_text() -> None:
    h = harness()
    capture = await h.run(lambda lc: lc.post_initial())
    assert capture.events and not capture.text_capture_complete
    with pytest.raises(ValueError, match="single-use"):
        await h.run(lambda lc: finish(lc, "second root"))


@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
async def test_failure_or_cancellation_after_delivery_keeps_incomplete_snapshot(
    error: BaseException,
) -> None:
    h = harness()

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await finish(lc, "actually posted")
        raise error

    with pytest.raises(type(error)):
        await h.run(invoke)
    capture = h.snapshot()
    assert content(capture) == "actually posted"
    assert not capture.text_capture_complete


async def test_delivery_failure_preserves_prior_card_and_no_complete_capture() -> None:
    h = harness()

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        h.gateway.fail_next("edit", RuntimeError("offline transport failure"))
        await lc.on_terminal_success(state("undelivered answer"))

    with pytest.raises(RuntimeError, match="transport failure"):
        await h.run(invoke)
    capture = h.snapshot()
    assert not capture.text_capture_complete
    assert "undelivered" not in content(capture)
    assert len(capture.events) == 1


async def test_gateway_refuses_unknown_network_operation_and_wrong_channel() -> None:
    h = harness()
    with pytest.raises(AttributeError):
        await h.client.fetch_user(1)
    with pytest.raises(ValueError, match="another channel"):
        await h.gateway.get_message(999, 1)
    assert not h.snapshot().text_capture_complete


async def test_real_host_driver_pumps_scripted_provider_into_real_discord_posts() -> None:
    h = harness()
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
    async with build_no_retry_anthropic(router) as provider:

        async def invoke(lc: DiscordTurnLifecycle) -> TurnState:
            await lc.post_initial()
            return await run_turn(
                anthropic=provider,
                session_id="sess:1",
                user_message="qa input",
                lifecycle=lc,
                cancel=asyncio.Event(),
                path="legacy",
                billing=BillingExempt(reason="cli-operator-run"),
            )

        capture = await h.run(invoke)
    assert capture.text_capture_complete
    assert content(capture) == "provider answer"
    assert hits == ["sess:1"] and len(bodies) == 1
    assert capture.session_id == "sess:1" and capture.root_turn_id == "root:1"
    assert isinstance(h.lifecycle.message_ref, discord.Message)


async def test_terminal_failure_captures_actual_error_card_without_headless_text() -> None:
    h = harness()

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await lc.post_initial()
        await lc.on_terminal_failure(TurnState(), RuntimeError("private provider error"))

    capture = await h.run(invoke)
    assert capture.text_capture_complete
    assert content(capture) == ""
    assert "Something went wrong." in capture.messages[0].embed_texts
    assert json.loads(capture.messages[0].payload_json)["embeds"][0]["color"] != 0


async def test_controls_are_real_serialized_buttons_retired_on_terminal() -> None:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Cancel", custom_id="qa-cancel"))
    h = harness(cancel_view=view)
    capture = await h.run(lambda lc: finish(lc, "answer"))
    assert capture.events[0].message.component_labels == ("Cancel",)
    assert capture.messages[0].component_labels == ()
    view.stop()


async def test_two_turns_do_not_reuse_message_ids() -> None:
    first = await harness().run(lambda lc: finish(lc, "first"))
    second = await DiscordSurfaceHarness(
        evidence_id="qa:turn:2", session_id="sess:1", root_turn_id="root:2"
    ).run(lambda lc: finish(lc, "second"))
    assert first.messages[0].message_id != second.messages[0].message_id
    assert not set(e.evidence_id for e in first.events).intersection(
        e.evidence_id for e in second.events
    )


async def test_completed_capture_freezes_at_invocation_close() -> None:
    h = harness()
    capture = await h.run(lambda lc: finish(lc, "original"))
    await h.transport.send("outside owned turn")
    assert h.snapshot() == capture
    assert "outside owned turn" not in capture.model_dump_json()


async def test_two_terminals_cannot_claim_complete_capture() -> None:
    h = harness()

    async def invoke(lc: DiscordTurnLifecycle) -> None:
        await finish(lc, "first")
        await lc.on_terminal_success(state("second"))

    capture = await h.run(invoke)
    assert not capture.post_capture_complete and not capture.text_capture_complete


async def test_same_name_uploads_keep_distinct_bytes_within_one_send_and_later_edits() -> None:
    h = harness()
    message = await h.transport.send(
        files=[
            discord.File(io.BytesIO(b"first"), filename="same.txt"),
            discord.File(io.BytesIO(b"second"), filename="same.txt"),
        ]
    )
    first, second = message.attachments
    assert first.url != second.url and first.id != second.id
    assert await first.read() == b"first" and await second.read() == b"second"
    edited = await h.transport.edit(
        message,
        attachments=[first, second, discord.File(io.BytesIO(b"third"), filename="same.txt")],
    )
    assert edited is not None
    third = edited.attachments[-1]
    edited_again = await h.transport.edit(
        edited,
        attachments=[*edited.attachments, discord.File(io.BytesIO(b"fourth"), filename="same.txt")],
    )
    assert edited_again is not None
    assert len({a.url for a in edited_again.attachments}) == 4
    assert [await a.read() for a in edited_again.attachments] == [
        b"first",
        b"second",
        b"third",
        b"fourth",
    ]
    assert await first.read() == b"first" and await third.read() == b"third"
