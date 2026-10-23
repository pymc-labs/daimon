"""Actual platform queue/DB controls; synthetic root callbacks do not certify C01."""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import textwrap
import uuid
from collections.abc import Callable, Mapping
from typing import Any, cast

import daimon.adapters.slack.app as app_module
import pytest
from aioresponses import aioresponses
from daimon.adapters.slack.app import SlackApp
from daimon.testing.adapters.slack.conformance import (
    AdmissionBarrier,
    MentionTurn,
    MultiHumanEnvelope,
    ThreadInputs,
    multi_human_evidence,
)
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .harness import make_orchestrate_app

pytestmark = pytest.mark.asyncio


@pytest.fixture
def pooled_factory(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    # Concurrent orchestration tails need separate pooled connections.
    del db_clean
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


async def _assert_real_platform_fixture(
    pooled_factory: async_sessionmaker[AsyncSession],
) -> None:
    (app, anthropic) = make_orchestrate_app(pooled_factory)
    tenant_id = uuid.uuid4()
    client = AsyncWebClient(token="xoxb-test")
    calls: list[MentionTurn] = []

    async def run(turn: MentionTurn, barrier: AdmissionBarrier | None) -> None:
        calls.append(turn)
        if barrier is not None:
            barrier.started.set()
            await barrier.release.wait()

    def event(ts: str, root: str, user: str, text: str) -> dict[str, Any]:
        return {
            "ts": ts,
            "event_ts": "9" + ts,
            "thread_ts": root,
            "channel": "C_TEST",
            "user": user,
            "text": text,
            "files": [
                {
                    "id": "F_" + ts,
                    "name": ts + ".bin",
                    "mimetype": "application/octet-stream",
                    "size": 256,
                    "url_private": "https://offline.invalid/f",
                    "url_private_download": "https://offline.invalid/f",
                }
            ],
        }

    inputs = (
        ThreadInputs(
            event("1.001", "1.001", "U_A", "initial-a"),
            (
                event("1.002", "1.001", "U_B", "b-first"),
                event("1.003", "1.001", "U_A", "a-followup"),
                event("1.004", "1.001", "U_B", "b-second"),
            ),
        ),
        ThreadInputs(
            event("2.001", "2.001", "U_B", "initial-b"),
            (event("2.002", "2.001", "U_A", "other-thread"),),
        ),
    )
    try:
        with aioresponses() as transport:
            transport.post(  # pyright: ignore[reportUnknownMemberType]
                re.compile("https://slack\\.com/api/reactions\\.(add|remove).*"),
                payload={"ok": True},
                repeat=True,
            )
            evidence = await multi_human_evidence(
                MultiHumanEnvelope(app, tenant_id, "C_TEST", "T_TEST", client, inputs, run)
            )
            requests = [call for group in transport.requests.values() for call in group]
            reaction_requests = [
                (method, url.path, url.query["channel"], url.query["name"], url.query["timestamp"])
                for (method, url), calls_for_url in transport.requests.items()
                for _ in calls_for_url
            ]
        assert evidence["threads"] == (
            {
                "thread_id": "1.001",
                "received_message_ids": ("1.001", "1.002", "1.003", "1.004"),
                "queued_message_ids": ("1.002", "1.003", "1.004"),
                "claimed_while_queued": True,
                "first_callback_finished_while_queued": False,
            },
            {
                "thread_id": "2.001",
                "received_message_ids": ("2.001", "2.002"),
                "queued_message_ids": ("2.002",),
                "claimed_while_queued": True,
                "first_callback_finished_while_queued": False,
            },
        )
        assert [
            (c.event["user"], c.content_override, [f["id"] for f in c.files or ()]) for c in calls
        ] == [
            ("U_A", None, ["F_1.001"]),
            ("U_B", "b-first\n\nb-second", ["F_1.002", "F_1.004"]),
            ("U_A", "a-followup", ["F_1.003"]),
            ("U_B", None, ["F_2.001"]),
            ("U_A", "other-thread", ["F_2.002"]),
        ]
        assert all(
            c.tenant_id == tenant_id and c.channel == "C_TEST" and (c.team_id == "T_TEST")
            for c in calls
        )
        assert evidence["peak_callbacks_by_thread"] == {"1.001": 1, "2.001": 1}
        assert evidence["queue_empty_after"] is evidence["processing_empty_after"] is True
        assert not app.turn_queue.in_flight() and (not app.turn_queue.depth())
        assert len(requests) == 10
        expected = [
            ("POST", "/api/reactions.add", "C_TEST", "eyes", "91.001"),
            ("POST", "/api/reactions.add", "C_TEST", "eyes", "92.001"),
        ]
        for timestamp in ("91.002", "91.003", "91.004", "92.002"):
            for method in ("add", "remove"):
                expected.append(
                    (
                        "POST",
                        "/api/reactions." + method,
                        "C_TEST",
                        "hourglass_flowing_sand",
                        timestamp,
                    )
                )
        assert sorted(reaction_requests) == sorted(expected)
        assert all(
            batch["callback_returned"] is True
            for batch in cast(tuple[Mapping[str, object], ...], evidence["batches"])
        )

        assert "_run_thread_turn" not in app.__dict__
    finally:
        await anthropic.close()


@pytest.mark.parametrize("failure", ["exception", "timeout", "cancel", "early_return"])
async def test_exit_restores_handler_releases_ticket_and_preserves_failure(
    failure: str, pooled_factory: async_sessionmaker[AsyncSession]
) -> None:
    entered = asyncio.Event()
    (app, anthropic) = make_orchestrate_app(pooled_factory)

    def event(ts: str, root: str, human: str) -> dict[str, Any]:
        return {
            "channel": "C_TEST",
            "ts": ts,
            "event_ts": ts,
            "thread_ts": root,
            "user": human,
            "text": ts,
        }

    inputs = (
        ThreadInputs(event("1.01", "1.01", "U_A"), (event("1.02", "1.01", "U_B"),)),
        ThreadInputs(event("2.01", "2.01", "U_B"), (event("2.02", "2.01", "U_A"),)),
    )
    attr = "_run_thread_turn"
    original = getattr(app, attr)

    async def run(_turn: MentionTurn, barrier: AdmissionBarrier | None) -> None:
        assert barrier is not None
        entered.set()
        if failure == "exception":
            raise LookupError("controlled root callback failure")
        if failure == "early_return":
            barrier.started.set()
            return
        await asyncio.Event().wait()

    try:
        with aioresponses() as transport:
            transport.post(  # pyright: ignore[reportUnknownMemberType]
                re.compile("https://slack\\.com/api/reactions\\.(add|remove).*"),
                payload={"ok": True},
                repeat=True,
            )
            probe = multi_human_evidence(
                MultiHumanEnvelope(
                    app,
                    uuid.uuid4(),
                    "C_TEST",
                    "T_TEST",
                    AsyncWebClient(token="xoxb-test"),
                    inputs,
                    run,
                    timeout_s=1.0,
                )
            )
            task = asyncio.create_task(probe)
            await asyncio.wait_for(entered.wait(), timeout=2.0)
            if failure == "cancel":
                task.cancel()
            expected = {
                "exception": LookupError,
                "timeout": TimeoutError,
                "cancel": asyncio.CancelledError,
                "early_return": RuntimeError,
            }[failure]
            with pytest.raises(expected):
                await task
        assert getattr(app, attr) == original and attr not in app.__dict__
        assert not app._processing  # pyright: ignore[reportPrivateUsage]
        assert not app._pending  # pyright: ignore[reportPrivateUsage]
        assert not app.turn_queue.in_flight() and (not app.turn_queue.depth())
    finally:
        await anthropic.close()


async def test_slack_real_orchestration_db_sdk_reactions_and_per_author_file_batches(
    pooled_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _assert_real_platform_fixture(pooled_factory)


@pytest.mark.parametrize("mutation", ["busy_thread_admission", "cross_author_batching"])
async def test_production_mutation_rejects_the_same_independent_platform_fixture(
    pooled_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    if mutation == "busy_thread_admission":
        tree = ast.parse(textwrap.dedent(inspect.getsource(SlackApp._orchestrate)))  # pyright: ignore[reportPrivateUsage]
        matching = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "thread_key in self._processing"
        ]
        assert len(matching) == 1, "production admission branch must exist"
        matching[0].test = ast.Constant(value=False)
        ast.fix_missing_locations(tree)
        namespace: dict[str, Any] = vars(app_module).copy()
        exec(compile(tree, "<mutated-production-orchestrate>", "exec"), namespace)
        monkeypatch.setattr(SlackApp, "_orchestrate", namespace["_orchestrate"])
    else:

        def flatten(
            pending: list[dict[str, Any]],
            _author: Callable[[dict[str, Any]], str | None],
            *,
            separate: Callable[[dict[str, Any]], bool] = lambda _: False,
        ) -> list[list[dict[str, Any]]]:
            del separate  # This mutation deliberately ignores all grouping boundaries.
            return [pending] if pending else []

        monkeypatch.setattr(app_module, "group_by_author", flatten)
    with pytest.raises(AssertionError):
        await _assert_real_platform_fixture(pooled_factory)
