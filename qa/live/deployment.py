"""Deployment evidence and one bounded retry, without product-failure alerts."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from qa.live.errors import redact
from qa.live.report import Result
from qa.live.schema import CatalogScenario
from qa.live.types import Backend, Check, Pending, objects, utcnow

if TYPE_CHECKING:
    from qa.live.runner import Executor


def finish_observation(result: Result, backend: Backend) -> None:
    observation = result.deployment
    if observation is None:
        return
    observation.ended_at = utcnow()
    if not observation.start_image and not result.turns:
        return
    for turn in result.turns:
        for message in turn.messages:
            if any(
                embed.get("title") == "Daimon restarted before this request finished."
                for embed in objects(message.get("embeds"))
            ):
                observation.events.append(
                    {
                        "event": "restart_card",
                        "turn": turn.number,
                        "message_id": message.get("id"),
                        "thread_id": turn.thread_id,
                        "timestamp": message.get("edited_timestamp") or message.get("timestamp"),
                        "embeds": message.get("embeds"),
                        "affects_turn": True,
                    }
                )
    end_probe_pending = False
    try:
        observation.end_image = backend.deployment_image()
    except Pending:
        # A verified start followed by mixed/missing workers is an active rollout.
        # The bounded settle gate determines whether a fresh attempt is possible.
        end_probe_pending = bool(observation.start_image)
        observation.error = "deployment image unavailable: Pending"
        result.notes.append(observation.error)
    except Exception as exc:
        observation.error = "deployment image unavailable: " + type(exc).__name__
        result.notes.append(redact(observation.error))
    try:
        observation.events.extend(
            backend.deployment_events(observation.started_at, observation.ended_at, result.turns)
        )
    except Exception as exc:
        observation.error = "deployment observation unavailable: " + type(exc).__name__
        result.notes.append(redact(observation.error))
    observation.interrupted = bool(
        end_probe_pending
        or (
            observation.start_image
            and observation.end_image
            and observation.start_image != observation.end_image
        )
        or any(event.get("affects_turn") is True for event in observation.events)
    )
    if observation.interrupted:
        result.checks.append(Check("deployment", "PENDING", "deploy-interrupted"))
    elif observation.error:
        result.checks.append(Check("deployment", "PENDING", observation.error))


def wait_for_stable_deployment(
    backend: Backend, *, timeout_s: float = 300, stable_s: float = 30
) -> None:
    deadline = time.monotonic() + timeout_s
    image: str | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        try:
            current = backend.deployment_image()
        except Pending:
            image = None
            stable_since = None
        else:
            now = time.monotonic()
            if current != image:
                image, stable_since = current, now
            elif stable_since is not None and now - stable_since >= stable_s:
                return
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    raise Pending("deployment did not settle within the retry window")


def run_with_deploy_retry(
    scenario: CatalogScenario,
    factory: Callable[[], Executor],
    on_result: Callable[[Result], None],
    *,
    retry_allowed: Callable[[], bool],
) -> list[Result]:
    """Persist both attempts; use a fresh backend/judge and never retry twice."""
    attempts: list[Result] = []
    for attempt in range(2):
        executor = factory()
        result = executor.run(scenario)
        if attempts:
            result.retry_of = attempts[0].run_id
        attempts.append(result)
        on_result(result)
        if not result.deployment or not result.deployment.interrupted or attempt:
            break
        if not retry_allowed():
            result.notes.append("deploy-interrupted retry refused by remaining pass budget")
            on_result(result)
            break
        try:
            wait_for_stable_deployment(executor.backend)
        except Pending as exc:
            result.notes.append(str(exc))
            on_result(result)
            break
    return attempts
