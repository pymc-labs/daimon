"""Execute a single scenario with teardown, accounting, and typed outcomes."""

from __future__ import annotations

import re
import signal
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from qa.live.config import Pricing
from qa.live.context import Context
from qa.live.cost import Ledger, estimate
from qa.live.deployment import finish_observation
from qa.live.errors import exception_evidence
from qa.live.evaluate import evaluate
from qa.live.models import BackendName, ModelPolicy
from qa.live.report import Result
from qa.live.schema import Assertion, CatalogScenario, ProposedScenario, Step
from qa.live.types import (
    Backend,
    Check,
    DeploymentEvidence,
    Judge,
    Pending,
    Turn,
    WatchTimeout,
    utcnow,
)

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
        self,
        backend: Backend,
        judge: Judge,
        ledger: Ledger,
        pricing: Pricing,
        env: str,
        *,
        models: ModelPolicy | None = None,
        model_backend: BackendName = "anthropic",
    ) -> None:
        self.models = models or ModelPolicy()
        self.model_backend: BackendName = model_backend
        self.backend = backend
        self.judge = judge
        self.ledger = ledger
        self.pricing = pricing
        self.env = env
        self.channels: dict[str, str] = {}
        self.created: list[str] = []
        self.context: Context | None = None
        self.trigger_attempted = False
        self.watchers: dict[int, Future[None]] = {}
        self.watch_pool: ThreadPoolExecutor | None = None
        self.burst_timeout = backend.fallback_watch_s
        self.burst_workers = 0

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
        if (
            self.env == "staging"
            and scenario.set == "A"
            and scenario.surface == "headless"
            and not scenario.setup
            and not scenario.steps
            and not scenario.teardown
            and scenario.assertions
            and all(a.kind == "http_check" for a in scenario.assertions)
        ):
            context = Context(
                {**self.backend.context(), "nonce": uuid.uuid4().hex[:8]}, result, Path(".")
            )
            try:
                for assertion in scenario.assertions:
                    resolved = Assertion.model_validate(
                        context.substitute(assertion.model_dump(mode="json", by_alias=True))
                    )
                    result.checks.append(evaluate(resolved, [], self.backend, self.judge))
            except Pending as exc:
                result.checks.append(Check("execution", "PENDING", str(exc)))
            except Exception as exc:
                result.errors.append(exception_evidence(exc, "http_check"))
                result.checks.append(
                    Check("execution", "PENDING", "harness error: " + type(exc).__name__)
                )
            result.finalize()
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
        harness_failed = False
        if self.env == "staging":
            result.deployment = DeploymentEvidence()
        try:
            for step in [*scenario.setup, *scenario.steps]:
                if step.guild is not None:
                    requested = self.context.resolve(step.guild)
                    if requested != self.context.values.get("guild_id"):
                        raise Pending("new_channel guild requires a separately approved QA target")
            if result.deployment:
                try:
                    result.deployment.start_image = self.backend.deployment_image()
                except Pending:
                    result.deployment.error = "deployment image unavailable before any trigger"
                    raise
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
                steps = [*scenario.setup, *scenario.steps]
                self.burst_workers = sum(len(s.texts) for s in steps if s.do == "burst")
                for index, step in enumerate(steps):
                    if step.do == "burst":
                        self.burst_timeout = next(
                            (s.timeout_s for s in steps[index + 1 :] if s.do == "wait_done"),
                            self.backend.fallback_watch_s,
                        )
                    self.step(step, result, channel)
                self.join_watchers(result)
                for turn in result.turns:
                    if turn.ended_at is None:
                        self.collect(turn, self.backend.fallback_watch_s)
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
                check = evaluate(resolved, result.turns, self.backend, self.judge)
                result.checks.append(check)
                if check.kind == "judge" and check.reason.startswith("judge execution unavailable"):
                    result.notes.append(f"turn {check.turn}: {check.reason}")
        except Pending as exc:
            result.checks.append(Check("execution", "PENDING", str(exc)))
        except (KeyboardInterrupt, SystemExit) as exc:
            harness_failed = True
            result.errors.append(exception_evidence(exc, "execution"))
            result.checks.append(
                Check("execution", "PENDING", f"harness error: {type(exc).__name__}")
            )
        except Exception as exc:
            harness_failed = True
            result.errors.append(exception_evidence(exc, "execution"))
            result.checks.append(
                Check("execution", "PENDING", f"harness error: {type(exc).__name__}")
            )
        finally:
            # Finish read-only watchers before deleting their owned channels.
            for future in self.watchers.values():
                try:
                    future.result()
                except WatchTimeout:
                    result.checks.append(
                        Check("watch", "FAIL", "watch ended without terminal proof")
                    )
                except BaseException as exc:
                    harness_failed = True
                    result.errors.append(exception_evidence(exc, "watcher"))
                    result.checks.append(
                        Check("execution", "PENDING", f"harness error: {type(exc).__name__}")
                    )
            self.watchers.clear()
            if self.watch_pool:
                self.watch_pool.shutdown(wait=True)
                self.watch_pool = None
            result.errors.extend(self.judge.errors)
            if channel:
                for step in scenario.teardown:
                    try:
                        self.step(step, result, channel)
                    except BaseException as exc:
                        result.errors.append(exception_evidence(exc, "teardown"))
                        result.checks.append(
                            Check("teardown", "PENDING", f"harness error: {type(exc).__name__}")
                        )
                for created in reversed(self.created):
                    try:
                        with deferred_interrupts():
                            self.backend.delete_channel(created)
                    except BaseException as exc:
                        result.errors.append(exception_evidence(exc, "cleanup"))
                        result.checks.append(
                            Check(
                                "cleanup",
                                "PENDING",
                                f"harness error: delete channel {created}: {type(exc).__name__}",
                            )
                        )
            for turn in result.turns:
                try:
                    turn.usage = self.backend.usage(turn)
                    if turn.usage.skipped_reason and not turn.usage.models and turn.usage.usd == 0:
                        result.checks.append(
                            Check(
                                "model",
                                "PENDING",
                                "n/a: Daimon skipped this turn: " + turn.usage.skipped_reason,
                                turn.number,
                            )
                        )
                    elif not turn.usage.models and harness_failed:
                        result.checks.append(
                            Check(
                                "model",
                                "PENDING",
                                "harness error: turn model could not be collected",
                                turn.number,
                            )
                        )
                    elif not turn.usage.models or any(
                        not self.models.accepts(self.model_backend, model, self.env)
                        for model in turn.usage.models
                    ):
                        result.checks.append(
                            Check(
                                "model",
                                "FAIL",
                                "Daimon model is missing or outside the approved cheap-model pin",
                                turn.number,
                            )
                        )
                except Exception as exc:
                    result.errors.append(exception_evidence(exc, "usage"))
                    result.notes.append(f"turn {turn.number}: usage unavailable")
                    result.checks.append(
                        Check(
                            "model",
                            "PENDING",
                            f"harness error: usage evidence unavailable: {type(exc).__name__}",
                            turn.number,
                        )
                    )
            usages = [t.usage for t in result.turns] + self.judge.usage
            fixture_dir.cleanup()
            self.ledger.receipt(run_id, usages, estimated, spend_possible=self.trigger_attempted)
            if result.deployment:
                finish_observation(result, self.backend)
        result.finalize()
        return result

    def watch(self, turn: Turn, started: threading.Event) -> None:
        started.set()
        self.collect(turn, self.burst_timeout)

    def collect(self, turn: Turn, timeout: float) -> None:
        turn.settled = False
        try:
            self.backend.collect(turn, timeout)
        except WatchTimeout:
            turn.settled = False
            turn.ended_at = turn.ended_at or utcnow()
            raise

    def join_watchers(self, result: Result) -> None:
        errors: list[tuple[int, BaseException]] = []
        for number, future in self.watchers.items():
            try:
                future.result()
            except WatchTimeout:
                result.checks.append(
                    Check("watch", "FAIL", "watch ended without terminal proof", number)
                )
            except BaseException as exc:
                errors.append((number, exc))
        self.watchers.clear()
        if errors:
            for number, exc in errors[1:]:
                if not isinstance(exc, Pending):
                    result.errors.append(exception_evidence(exc, "watcher"))
                    result.checks.append(
                        Check(
                            "execution", "PENDING", f"harness error: {type(exc).__name__}", number
                        )
                    )
            raise errors[0][1]

    def step(self, step: Step, result: Result, channel: str) -> None:
        if self.watchers and step.role != "user":
            raise Pending(
                "harness error: non-user steps are forbidden while burst watchers are active"
            )
        if self.context:
            step = self.context.step(step)
        kind = step.do
        if kind == "new_channel":
            if step.guild and step.guild != self.backend.context().get("guild_id"):
                raise Pending("new_channel guild requires a separately approved QA target")
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
            if kind == "channel_message" and not step.mention and not step.reply_to:
                message = self.backend.send(destination, step, mention=False)
                result.seed_messages.append({"message_id": message, "channel_id": destination})
                return
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
            # One pin preflight before the burst; remote probes must not
            # serialize its posts. Every completed turn still verifies its model.
            self.backend.verify_model(destination)
            if self.watch_pool is None:
                self.watch_pool = ThreadPoolExecutor(
                    max_workers=max(self.burst_workers, len(step.texts))
                )
            schedule = time.monotonic()
            for i, text in enumerate(step.texts):
                time.sleep(max(0, schedule + i * step.interval_s - time.monotonic()))
                started_at = utcnow()
                self.trigger_attempted = True
                trigger = self.backend.send(
                    destination, Step(do="mention", text=text), mention=True
                )
                turn = Turn(
                    len(result.turns) + 1,
                    trigger,
                    destination,
                    started_at,
                    thread_id=destination if destination != channel else None,
                )
                result.turns.append(turn)
                started = threading.Event()
                self.watchers[turn.number] = self.watch_pool.submit(self.watch, turn, started)
                started.wait()
        elif kind == "wait":
            time.sleep(step.s or 0)
        elif kind == "wait_done":
            pending = [t for t in result.turns if t.ended_at is None or t.number in self.watchers]
            if not pending:
                raise Pending("wait_done requires an unfinished turn")
            watched = set(self.watchers)
            self.join_watchers(result)
            for turn in pending:
                if turn.number not in watched:
                    self.collect(turn, step.timeout_s)
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
            self.collect(last, step.timeout_s)
        elif kind in {"admin", "restart_workers"}:
            self.backend.admin(step, channel)
            if self.context:
                self.context.values.update(self.backend.context())
        elif kind == "headless_interrupt":
            raise Pending("headless interrupt hook is not implemented")
        elif kind == "dm":
            raise Pending("Discord forbids bot-to-bot DMs; no writes outside owned QA channels")
        else:
            raise Pending(f"step hook unavailable: {kind}")
