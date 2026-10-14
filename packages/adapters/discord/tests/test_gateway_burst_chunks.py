"""Frozen burst/dedupe/chunk steps through actual gateway and native SDK paths."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import discord
import pytest
import sentry_sdk
import yaml
from daimon.adapters.discord import bot as bot_module
from daimon.adapters.discord import lifecycle as lifecycle_module
from daimon.adapters.discord.split import split_for_discord_safe
from daimon.testing.discord_ingress import ConcurrentCheckpoint, DiscordConcurrentDriver
from daimon.testing.discord_surface import DiscordPostEvent
from mux.contracts.ids import Provider

from packages.adapters.discord.tests.test_gateway_ingress import (
    BusyWire,
    OwnedIngress,
    payload,
    start,
)
from packages.adapters.discord.tests.test_gateway_ingress import (
    owned as owned,
)

CATALOG = Path(__file__).resolve().parents[3] / "testing/fixtures/target53/catalog/scenarios"

if TYPE_CHECKING:
    from discord.types.gateway import MessageCreateEvent


def source(cell: str) -> tuple[dict[str, Any], str]:
    raw = (CATALOG / f"{cell}.yaml").read_bytes()
    return cast(dict[str, Any], yaml.safe_load(raw)), hashlib.sha256(raw).hexdigest()


def answer_posts(checkpoint: ConcurrentCheckpoint, thread_id: str) -> list[DiscordPostEvent]:
    """Latest accepted nonempty answer revision per actual surviving message ID."""
    latest: dict[str, DiscordPostEvent] = {}
    for post in checkpoint.ingress.posts:
        if post.message.channel_id != thread_id:
            continue
        if post.operation == "delete":
            latest.pop(post.message.message_id, None)
        else:
            latest[post.message.message_id] = post
    return [post for post in latest.values() if post.message.content]


def emit(cell: str, digest: str, backend: Provider, **facts: object) -> None:
    print(
        "C7_SLICE3_RECEIPT="
        + json.dumps({"cell": cell, "source_sha256": digest, "backend": backend, **facts})
    )


class SourceBurstClock:
    """Exact authored burst offsets; real native gates remain independent."""

    def __init__(self) -> None:
        self.initial_monotonic = 1000.0
        self.initial_utc = datetime.now(UTC)
        self.elapsed = 0.0

    def now(self) -> float:
        return self.initial_monotonic + self.elapsed

    def utc(self) -> datetime:
        return self.initial_utc + timedelta(seconds=self.elapsed)

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize(
    "cell", ["QA-NEW3-BURST-CHANNEL-MENTIONS", "QA-NEW4-DOUBLE-MENTION-DEDUPE"]
)
async def test_source_channel_burst_has_distinct_real_admissions(
    owned: OwnedIngress, backend: Provider, cell: str
) -> None:
    if backend != "anthropic":
        pytest.skip("PENDING: explicit owned alternate-provider native capture binding")
    scenario, digest = source(cell)
    burst = scenario["steps"][1]
    texts = cast(list[str], burst["texts"])
    assert burst["interval_s"] == 1
    answers = ["B1", "B2", "B3"] if "NEW3" in cell else ["DUP-OK", "DUP-OK"]
    clock = SourceBurstClock()
    harness = await start(owned, backend, utc_clock=clock.utc)
    harness.http.clock = clock.now
    for surface in harness.http.surfaces.values():
        surface.clock = clock.now
    harness.bot.turn_queue.clock = clock.now
    wire = BusyWire(owned, answers)
    driver = DiscordConcurrentDriver(harness)
    messages = [payload(1525400128000000000 + i, text) for i, text in enumerate(texts)]
    running: list[ConcurrentCheckpoint] = []
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            harness.deliver(messages[0])
            await asyncio.wait_for(harness.http.first_post.wait(), 30)
            harness.http.release_first_post.set()
            for i in range(len(messages)):
                if i:
                    clock.advance(burst["interval_s"])
                    await driver.deliver_while_busy(messages[i])
                await wire.running(i + 1)
                assert texts[i] in wire.roots[i].input_json
                checkpoint = await driver.checkpoint(f"source-burst-root-{i + 1}-running")
                assert checkpoint.ingress.queue_in_flight == i + 1
                assert len(checkpoint.live_bindings) == i + 1
                assert all(not root.release.is_set() for root in wire.roots)
                running.append(checkpoint)
            assert len({root.session_id for root in wire.roots}) == len(messages)
            assert len({root.root_id for root in wire.roots}) == len(messages)
            assert [m.message_id for m in running[-1].ingress.gateway_messages] == [
                m["id"] for m in messages
            ]
            assert [
                m.observed_s - running[-1].ingress.gateway_messages[0].observed_s
                for m in running[-1].ingress.gateway_messages
            ] == list(range(len(messages)))
            assert all(m["channel_id"] == "101" for m in messages)
            assert [t.message_id for t in running[-1].ingress.thread_creations] == [
                m["id"] for m in messages
            ]
            if "NEW4" in cell:
                assert messages[0]["content"] == messages[1]["content"]
                assert messages[0]["id"] != messages[1]["id"]
            for root in wire.roots:
                root.release.set()
            await asyncio.wait_for(harness.wait(), 30)
            final = await driver.checkpoint("source-burst-finished")
        assert not final.ingress.errors and not owned.native.violations
        assert final.ingress.callbacks_pending == final.ingress.queue_in_flight == 0
        posts = [answer_posts(final, m["id"]) for m in messages]
        assert all(len(p) == 1 for p in posts)
        assert [p[0].message.content for p in posts] == answers
        assert all(s.closed for s in wire.streams)
        expected_fail = "NEW4" in cell
        if expected_fail:
            assert len(final.ingress.thread_creations) == sum(map(len, posts)) == 2
            assert "Expected FAIL" in scenario["notes"]
        emit(
            cell,
            digest,
            backend,
            whole_cell="FAIL" if expected_fail else "PENDING",
            expected_failure=expected_fail,
            reason="ACTUAL_TWO_AUTHORED_MESSAGES_CREATE_TWO_THREADS_AND_ANSWERS"
            if expected_fail
            else "CONTROLLED_NATIVE_RECIPE_NOT_ORACLE_PINNED",
            adapter_facet="FAIL" if expected_fail else "PASS",
            roots=[
                {"session_id": r.session_id, "root_id": r.root_id, "input_json": r.input_json}
                for r in wire.roots
            ],
            checkpoints=[c.model_dump(mode="json") for c in (*running, final)],
        )
    finally:
        for root in wire.roots:
            root.release.set()
        await harness.close()


def inventory_answer() -> str:
    """Authored native-wire control, not provider-generated scenario proof."""
    lines = [
        "class Inventory:",
        '    """A toy inventory with explicit item records."""',
        "    def __init__(self):",
        "        self.items = {}",
    ]
    for i in range(116):
        lines.append(f"    # Inventory record {i:03d}: names and quantities are explicit.")
    return (
        "```python\n"
        + "\n".join(lines)
        + "\n```\n\n"
        + "\n".join(f"- Explanation point {i + 1}." for i in range(5))
    )


async def run_chunk_reply_probe(
    owned: OwnedIngress, checkpoints: list[ConcurrentCheckpoint] | None = None
) -> None:
    backend: Provider = "anthropic"
    cell = "QA-NEW10-SPLIT-ANSWER-AND-CHUNK-REPLY"
    scenario, digest = source(cell)
    harness = await start(owned, backend)
    wire = BusyWire(owned, [inventory_answer(), "CHUNK-OK"])
    driver = DiscordConcurrentDriver(harness)
    first = payload(1525400129000000000, scenario["steps"][1]["text"])
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            harness.deliver(first)
            await asyncio.wait_for(harness.http.first_post.wait(), 30)
            harness.http.release_first_post.set()
            root1 = await wire.running(1)
            root1.release.set()
            await asyncio.wait_for(harness.wait(), 30)
            split = await driver.checkpoint("first-answer-actual-chunks")
            if checkpoints is not None:
                checkpoints.append(split)
            chunks = answer_posts(split, first["id"])
            assert len(chunks) >= 2
            assert all(p.message.content.count("```") % 2 == 0 for p in chunks)
            assert (
                max(
                    b.message.observed_s - a.message.observed_s
                    for a, b in zip(chunks, chunks[1:], strict=False)
                )
                <= 5
            )
            assert all(not p.message.embed_texts for p in chunks[:-1])
            assert chunks[-1].message.embed_texts
            native_answer = next(r for r in root1.records if r["type"] == "agent.message")
            native_text = "".join(
                b["text"] for b in native_answer["content"] if b["type"] == "text"
            )
            assert native_text == inventory_answer()
            # Ignore only renderer-inserted fence wrappers and blank boundaries;
            # retain every original body line in order, including the overflow.
            actual_lines = [
                line
                for post in chunks
                for line in post.message.content.splitlines()
                if line.strip() and not line.startswith("```")
            ]
            native_lines = [
                line
                for line in native_text.splitlines()
                if line.strip() and not line.startswith("```")
            ]
            assert actual_lines == native_lines
            chunk2 = chunks[1].message
            thread = harness.bot.get_channel(int(first["id"]))
            assert isinstance(thread, discord.Thread)
            fetched = await thread.fetch_message(int(chunk2.message_id))
            assert fetched.id == int(chunk2.message_id) and fetched.content == chunk2.content
            assert fetched.author.id == int(harness.http.bot_user_id)
            actual_fetch = harness.http.message_fetches[-1]
            assert actual_fetch.message_id == chunk2.message_id
            second = payload(
                1525400129000000001,
                scenario["steps"][3]["text"],
                channel_id=thread.id,
            )
            assert scenario["steps"][3]["reply_to"] == "turn1.chunk2"
            second["message_reference"] = fetched.to_reference().to_dict()
            second["referenced_message"] = json.loads(actual_fetch.response_json)
            harness.deliver(second)
            root2 = await wire.running(2)
            assert root2.session_id == root1.session_id and root2.root_id != root1.root_id
            assert "CHUNK-OK" in root2.input_json
            root2.release.set()
            await asyncio.wait_for(harness.wait(), 30)
            final = await driver.checkpoint("chunk2-reply-actually-answered")
            if checkpoints is not None:
                checkpoints.append(final)
        assert len(final.ingress.parsed_replies) == 1
        reply = final.ingress.parsed_replies[-1]
        assert reply.message_id == second["id"]
        assert reply.reference_message_id == reply.resolved_message_id == chunk2.message_id
        assert reply.channel_id == reply.resolved_channel_id == first["id"]
        assert reply.resolved_author_id == "104"
        assert len(final.ingress.thread_creations) == 1
        assert any(p.message.content == "CHUNK-OK" for p in answer_posts(final, first["id"]))
        assert not final.ingress.errors and not owned.native.violations
        assert all(s.closed for s in wire.streams)
        emit(
            cell,
            digest,
            backend,
            whole_cell="PENDING",
            reason="CONTROLLED_NATIVE_RECIPE_NOT_ORACLE_PINNED",
            adapter_facet="PASS",
            chunk2_id=chunk2.message_id,
            native_input=root2.input_json,
            first_native_records=root1.records,
            checkpoints=[c.model_dump(mode="json") for c in (split, final)],
        )
    finally:
        for root in wire.roots:
            root.release.set()
        await harness.close()


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
async def test_source_reply_resolves_actual_second_answer_chunk(
    owned: OwnedIngress, backend: Provider
) -> None:
    if backend != "anthropic":
        pytest.skip("PENDING: explicit owned alternate-provider native capture binding")
    await run_chunk_reply_probe(owned)


@pytest.mark.parametrize(
    "mutation",
    ["overflow_dropped", "later_overflow_dropped", "parsed_reference_dropped", "avatar_missing"],
)
async def test_real_chunk_reply_probe_detects_edge_mutations(
    owned: OwnedIngress, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from daimon.testing.discord_surface import OfflineDiscordGateway
    from discord.state import ConnectionState

    if mutation == "overflow_dropped":

        def drop_overflow(text: str) -> list[str]:
            return [text[:1900]]

        monkeypatch.setattr(lifecycle_module, "split_for_discord_safe", drop_overflow)
    elif mutation == "later_overflow_dropped":
        original_split = split_for_discord_safe

        def drop_later_overflow(text: str) -> list[str]:
            return original_split(text)[:2]

        monkeypatch.setattr(lifecycle_module, "split_for_discord_safe", drop_later_overflow)
    elif mutation == "parsed_reference_dropped":
        original = ConnectionState.parse_message_create

        def drop_reference(state: ConnectionState, data: Any) -> None:
            changed = dict(data)
            changed.pop("message_reference", None)
            original(state, cast("MessageCreateEvent", changed))

        monkeypatch.setattr(ConnectionState, "parse_message_create", drop_reference)
    else:
        original_fetch = OfflineDiscordGateway.get_message

        async def omit_avatar(
            gateway: OfflineDiscordGateway, channel_id: int, message_id: int
        ) -> Any:
            result = await original_fetch(gateway, channel_id, message_id)
            result["author"].pop("avatar", None)
            return result

        monkeypatch.setattr(OfflineDiscordGateway, "get_message", omit_avatar)
    captured: list[ConcurrentCheckpoint] = []
    expected = AttributeError if mutation == "avatar_missing" else AssertionError
    with pytest.raises(expected):
        await run_chunk_reply_probe(owned, captured)
    assert captured
    if mutation == "overflow_dropped":
        assert len(answer_posts(captured[-1], "1525400129000000000")) == 1
    elif mutation == "later_overflow_dropped":
        assert len(answer_posts(captured[-1], "1525400129000000000")) == 2
    elif mutation == "parsed_reference_dropped":
        assert len(captured) == 2 and not captured[-1].ingress.parsed_replies
        assert any(p.message.content == "CHUNK-OK" for p in captured[-1].ingress.posts)
        raw = json.loads(captured[-1].ingress.gateway_messages[-1].payload_json)
        assert raw["message_reference"]["message_id"]
    print(
        "C7_SLICE3_MUTATION_RECEIPT="
        + json.dumps(
            {"mutation": mutation, "checkpoints": [c.model_dump(mode="json") for c in captured]}
        )
    )


async def test_serializing_production_channel_admission_loses_burst_witness(
    owned: OwnedIngress, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = await start(owned, "anthropic")
    wire = BusyWire(owned, ["B1", "B2", "B3"])
    driver = DiscordConcurrentDriver(harness)
    lock = asyncio.Lock()
    original = bot_module.DaimonBot.on_message

    async def serialized(
        bot: bot_module.DaimonBot,
        message: discord.Message,
        *,
        startup_replay: bool = False,
        resume_admitted: bool = False,
    ) -> None:
        async with lock:
            await original(
                bot, message, startup_replay=startup_replay, resume_admitted=resume_admitted
            )

    monkeypatch.setattr(bot_module.DaimonBot, "on_message", serialized)
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_client(sentry_sdk.Client(dsn="", default_integrations=False))
            harness.deliver(payload(1525400130000000000, "QA burst 1"))
            await asyncio.wait_for(harness.http.first_post.wait(), 30)
            harness.http.release_first_post.set()
            await wire.running(1)
            await driver.deliver_while_busy(payload(1525400130000000001, "QA burst 2"))
            await driver.deliver_while_busy(payload(1525400130000000002, "QA burst 3"))
            with pytest.raises(TimeoutError):
                await driver.wait_for(
                    "three-actual-concurrent-admissions",
                    lambda c: c.ingress.queue_in_flight == 3 and len(c.live_bindings) == 3,
                    timeout_s=0.25,
                )
            observed = await driver.checkpoint("production-serialization-mutation")
        assert len(observed.ingress.gateway_messages) == 3
        assert observed.ingress.callbacks_pending == 3
        assert (
            observed.ingress.queue_in_flight == len(observed.live_bindings) == len(wire.roots) == 1
        )
        print(
            "C7_SLICE3_MUTATION_RECEIPT="
            + json.dumps(
                {
                    "mutation": "production_channel_serialized",
                    "checkpoints": [observed.model_dump(mode="json")],
                }
            )
        )
    finally:
        await harness.close()
