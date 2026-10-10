"""Execute a single scenario with teardown, accounting, and typed outcomes."""

from __future__ import annotations

import re
import signal
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from qa.live.config import Pricing
from qa.live.context import Context
from qa.live.cost import Ledger, estimate
from qa.live.evaluate import evaluate
from qa.live.report import Result
from qa.live.schema import MODEL, Assertion, CatalogScenario, ProposedScenario, Step
from qa.live.types import Backend, Check, Judge, Pending, Turn, WatchTimeout, utcnow

GLOBAL_PATTERNS = (
    r"\(empty response\)",
    r"Discord Error \(\d+\)|Unknown Message|Invalid input:|Unexpected error:|"
    r"API Error \(\d+\)|Store error|Spec validation failed",
    r"access_token=",
)


@contextmanager
def deferred_interrupts() -> Iterator[None]:
    """Do not interrupt between successful channel creation and ownership recording."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


class Executor:
    def __init__(
        self, backend: Backend, judge: Judge, ledger: Ledger, pricing: Pricing, env: str
    ) -> None:
        self.backend = backend
        self.judge = judge
        self.ledger = ledger
        self.pricing = pricing
        self.env = env
        self.channels: dict[str, str] = {}
        self.created: list[str] = []
        self.context: Context | None = None
        self.trigger_attempted = False

    def run(self, scenario: CatalogScenario) -> Result:
        run_id = f"{utcnow().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
        result = Result(run_id, scenario.id, self.env)
        if isinstance(scenario, ProposedScenario):
            result.checks.append(
                Check(
                    "catalog",
                    "PENDING",
                    "; ".join(scenario.unsupported),
                )
            )
            return result
        if scenario.set == "B" or scenario.surface != "discord":
            result.checks.append(Check("surface", "PENDING", "human/platform hook required"))
            return result
        if self.env == "prod" and (
            scenario.tier != "canary"
            or scenario.setup
            or scenario.teardown
            or any(
                s.do not in {"new_channel", "mention", "thread_reply", "wait", "wait_done"}
                for s in scenario.steps
            )
        ):
            raise ValueError("production is limited to the two-turn canary, without admin actions")
        estimated = estimate(scenario, self.pricing)
        self.ledger.reserve(run_id, estimated)
        channel = ""
        fixture_dir = tempfile.TemporaryDirectory(prefix="daimon-qa-fixtures-")
        self.context = Context(
            {**self.backend.context(), "nonce": uuid.uuid4().hex[:8]},
            result,
            Path(fixture_dir.name),
        )
        self.channels = {}
        self.created = []
        self.trigger_attempted = False
        try:
            self.backend.preflight(
                {s.role for s in [*scenario.setup, *scenario.steps, *scenario.teardown]}
            )
            with deferred_interrupts():
                channel = self.backend.create_channel(f"qa-{scenario.id.lower()}-{run_id[-8:]}")
                result.channel_id = channel
                self.created.append(channel)
                self.channels["default"] = channel
                self.context.values["channel_id"] = channel
            try:
                for step in [*scenario.setup, *scenario.steps]:
                    self.step(step, result, channel)
                for turn in result.turns:
                    if turn.ended_at is None:
                        self.backend.collect(turn, self.backend.fallback_watch_s)
            except WatchTimeout:
                # A silent/stuck bot is a failed observation, not missing coverage.
                # Preserve the messages and evaluate latency/card assertions below.
                result.checks.append(Check("watch", "FAIL", "watch ended without terminal proof"))
            assertions = list(scenario.assertions)
            for turn in result.turns:
                assertions.extend(
                    Assertion(kind="text_absent", turn=turn.number, pattern=pattern)
                    for pattern in GLOBAL_PATTERNS
                )
            for assertion in assertions:
                resolved = Assertion.model_validate(
                    self.context.substitute(
                        assertion.model_dump(mode="json", by_alias=True),
                    )
                )
                result.checks.append(evaluate(resolved, result.turns, self.backend, self.judge))
        except Pending as exc:
            result.checks.append(Check("execution", "PENDING", str(exc)))
        except (KeyboardInterrupt, SystemExit) as exc:
            result.checks.append(Check("execution", "PENDING", type(exc).__name__))
        except Exception as exc:
            # External tool/DB/API exceptions can contain credentials. Persist the type only.
            result.checks.append(
                Check("execution", "FAIL", f"execution raised {type(exc).__name__}")
            )
        finally:
            if channel:
                for step in scenario.teardown:
                    try:
                        self.step(step, result, channel)
                    except BaseException as exc:
                        result.checks.append(Check("teardown", "FAIL", type(exc).__name__))
                for created in reversed(self.created):
                    try:
                        with deferred_interrupts():
                            self.backend.delete_channel(created)
                    except BaseException as exc:
                        result.checks.append(
                            Check(
                                "cleanup",
                                "FAIL",
                                f"delete channel {created}: {type(exc).__name__}",
                            )
                        )
            for turn in result.turns:
                try:
                    turn.usage = self.backend.usage(turn)
                    if not turn.usage.models or any(model != MODEL for model in turn.usage.models):
                        result.checks.append(
                            Check(
                                "model",
                                "FAIL",
                                "Daimon model is missing or outside the approved Haiku pin",
                                turn.number,
                            )
                        )
                except Exception:
                    result.notes.append(f"turn {turn.number}: usage unavailable")
                    result.checks.append(
                        Check("model", "FAIL", "Daimon model evidence unavailable", turn.number)
                    )
            usages = [t.usage for t in result.turns] + self.judge.usage
            fixture_dir.cleanup()
            self.ledger.receipt(run_id, usages, estimated, spend_possible=self.trigger_attempted)
        result.finalize()
        return result

    def step(self, step: Step, result: Result, channel: str) -> None:
        if self.context:
            step = self.context.step(step)
        kind = step.do
        if kind == "new_channel":
            if step.ref:
                if step.ref in self.channels:
                    raise ValueError("channel reference must be unique")
                if len(self.channels) == 1 and "default" in self.channels:
                    created = channel
                else:
                    if self.env == "prod":
                        raise ValueError("production canary permits only one QA channel")
                    with deferred_interrupts():
                        created = self.backend.create_channel(f"qa-{result.run_id[-8:]}-{step.ref}")
                        self.created.append(created)
                self.channels[step.ref] = created
                if self.context:
                    self.context.values[f"channel:{step.ref}"] = created
            return
        if kind in {"mention", "thread_reply", "channel_message"}:
            destination = self.channels.get(step.channel or "default", channel)
            if step.channel and step.channel not in self.channels:
                raise Pending(f"unknown channel reference: {step.channel}")
            reply_id: str | None = None
            if step.reply_to:
                reference = re.fullmatch(r"turn([1-9][0-9]*)\.chunk([1-9][0-9]*)", step.reply_to)
                assert reference is not None
                prior = next((t for t in result.turns if t.number == int(reference[1])), None)
                if prior is None or len(prior.messages) < int(reference[2]):
                    raise Pending("reply reference has no observed message")
                message = prior.messages[int(reference[2]) - 1]
                reply_id = str(message["id"])
                destination = str(message.get("channel_id") or prior.thread_id or prior.channel_id)
            elif kind == "thread_reply":
                destination = next((t.thread_id for t in reversed(result.turns) if t.thread_id), "")
                if not destination:
                    raise Pending("thread_reply requires an observed thread")
            self.backend.verify_model(destination)
            started_at = utcnow()
            self.trigger_attempted = True
            message = self.backend.send(
                destination,
                step,
                mention=kind == "mention" or step.mention,
                reply_message_id=reply_id,
            )
            if kind in {"mention", "thread_reply", "channel_message"}:
                result.turns.append(
                    Turn(
                        len(result.turns) + 1,
                        message,
                        destination,
                        started_at,
                        thread_id=destination if kind == "thread_reply" else None,
                    )
                )
        elif kind == "burst":
            destination = next(
                (t.thread_id for t in reversed(result.turns) if t.thread_id), channel
            )
            for i, text in enumerate(step.texts):
                if i:
                    time.sleep(step.interval_s)
                self.backend.verify_model(destination)
                started_at = utcnow()
                self.trigger_attempted = True
                trigger = self.backend.send(
                    destination, Step(do="mention", text=text), mention=True
                )
                result.turns.append(
                    Turn(
                        len(result.turns) + 1,
                        trigger,
                        destination,
                        started_at,
                        thread_id=destination if destination != channel else None,
                    )
                )
        elif kind == "wait":
            time.sleep(step.s or 0)
        elif kind == "wait_done":
            pending = [t for t in result.turns if t.ended_at is None]
            if not pending:
                raise Pending("wait_done requires an unfinished turn")
            for turn in pending:
                self.backend.collect(turn, step.timeout_s)
                if any(v in {"over_cap", "provisioning"} for v in turn.verdicts):
                    raise Pending("load-shed or provisioning notice: retry after preflight")
                if any(v in {"error", "cancelled"} for v in turn.verdicts):
                    result.checks.append(Check("terminal", "FAIL", str(turn.verdicts), turn.number))
        elif kind == "react":
            last = next((t for t in reversed(result.turns) if t.messages), None)
            if last is None:
                raise Pending("react requires a last answer")
            message = last.messages[-1]
            self.backend.react(
                str(message.get("channel_id") or last.thread_id or channel),
                str(message["id"]),
                step.emoji or "",
            )
            # Refresh evidence after the feedback action, including resulting reactions.
            self.backend.collect(last, step.timeout_s)
        elif kind in {"admin", "restart_workers"}:
            self.backend.admin(step, channel)
        elif kind == "headless_interrupt":
            raise Pending("headless interrupt hook is not implemented")
        elif kind == "dm":
            raise Pending("Discord forbids bot-to-bot DMs; no writes outside owned QA channels")
        else:
            raise Pending(f"step hook unavailable: {kind}")
