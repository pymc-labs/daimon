"""Explicit host Flash policy: no client creation or implicit selection.

The one retry is a create Google rate-limited, within the turn's deadline.
"""

import asyncio
import logging
import random
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from collections.abc import Set as AbstractSet
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from pydantic import JsonValue

from mux.contracts.ids import ModelRef
from mux.contracts.resources import Agent
from mux.drivers.gemini.mcp_auth import model_request
from mux.drivers.gemini.transport import (
    CreateNeverSent,
    Object,
    TokenCountingTransport,
    Transport,
    object_value,
    string,
)
from mux.errors import ProviderError
from mux.profiles.gemini import ARGON_MODEL, WORKER_MODEL

log = logging.getLogger(__name__)

FLASH_PRIMARY = "gemini-3.8-flash"
FLASH_MODELS = (FLASH_PRIMARY, "gemini-flash-latest", "gemini-3.5-flash-lite")
# Every model an attempt may name. Argon is only ever its own single attempt.
ATTEMPT_MODELS = (*FLASH_MODELS, ARGON_MODEL)


@dataclass(frozen=True)
class FlashAttempt:
    model: ModelRef
    attempt: int
    outcome: Literal["accepted", "refused", "unknown"]
    http_status: int | None = None
    interaction_id: str | None = None
    # False only when the create provably never reached Google (its connection
    # failed): nothing was sent, so its hold is freed at once.
    sent: bool = True


class FlashAccounting(Protocol):
    """Caller persists a hold before each dispatch, and preserves its outcome.

    A failed accounting operation stops the chain. Actuals arrive via scoped
    Usage. Free calls are reported `refused`: an HTTP 4xx that rejected the
    request itself, a 503 that refused the work (both with their status, no
    interaction returned), or a create that never reached Google (`sent=False`).
    Anything Google may have accepted stays `unknown`.
    An owner may also offer `release_unbilled(interaction_id=...)`, called when
    an interaction is read back cancelled, unbilled and with no step but its input.
    """

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None: ...
    async def after_attempt(self, *, receipt: FlashAttempt) -> None: ...


_DEFINITE = ("invalid_request", "auth", "permission", "not_found")


def definite_rejection(error: ProviderError) -> int | None:
    """The 4xx status of a create Google rejected outright, else None.

    Needs the provider's own HTTP status: a local refusal (no status), e.g. a
    response check after a 200, may follow an accepted request. Timeouts, 5xx and
    network failures may have been accepted too. A 429, with or without its
    QuotaFailure, is Google refusing to create anything.
    """
    code = error.native_error_code or ""
    if code == "http_429":
        return 429  # rate-limited: refused before any interaction existed
    if error.category not in _DEFINITE or not code.startswith("http_4"):
        return None
    status = code.removeprefix("http_")
    return int(status) if status.isdigit() else None


def only_input_steps(steps: object) -> bool:
    """No step but the caller's own input: Gemini produced nothing for it."""
    if steps is None:
        return True
    if not isinstance(steps, list):
        return False
    return all(
        isinstance(step, dict) and cast(dict[str, object], step).get("type") == "user_input"
        for step in cast(list[object], steps)
    )


# The turn's (or worker's) deadline, set by the host around each turn: a
# rate-limited create waits only while an answer can still come before it.
CREATE_DEADLINE: ContextVar[datetime | None] = ContextVar("gemini_create_deadline", default=None)
# Kept back from the deadline for the turn itself to run once created.
CREATE_DEADLINE_MARGIN_S = 20.0
CREATE_RETRIES = 4
_BACKOFF_BASE_S = 2.0
_sleep = asyncio.sleep  # the one wait, replaceable in tests without touching asyncio


def _rate_limited(error: ProviderError) -> bool:
    """A 429 Google would serve later: never a quota of 0 for the model."""
    return (
        error.category == "rate_limited"
        and error.native_error_code == "http_429"
        and error.native_code != "quota_zero"
    )


def _room_s() -> float | None:
    """Seconds a retry may still wait, or None with no deadline set (no retry)."""
    deadline = CREATE_DEADLINE.get()
    if deadline is None:
        return None
    return (deadline - datetime.now(UTC)).total_seconds() - CREATE_DEADLINE_MARGIN_S


def _wait_s(error: ProviderError, retries: int, room: float | None) -> float | None:
    """How long to wait before the next try, or None when no retry fits."""
    if retries >= CREATE_RETRIES or room is None or room <= 0:
        return None
    if error.retry_after_s is not None:
        # Google's own delay is the earliest it will serve: never shortened.
        return error.retry_after_s if error.retry_after_s <= room else None
    return min(_BACKOFF_BASE_S * 2**retries * random.uniform(0.8, 1.2), room)


class FlashTransport:
    def __init__(
        self,
        source: Transport,
        accounting: FlashAccounting,
        *,
        native_agent: Agent | None = None,
        bounded_worker: bool = False,
        primary: str = FLASH_PRIMARY,
    ) -> None:
        if primary not in (FLASH_PRIMARY, ARGON_MODEL):
            raise ValueError("an explicit Flash or Argon primary is required")
        self._primary = primary
        self._bounded_worker = bounded_worker
        self._native_agent = native_agent
        self.source, self.accounting = source, accounting
        self._models: dict[str, ModelRef] = {}

    def model_for_interaction(self, interaction: str) -> ModelRef | None:
        return self._models.get(interaction)

    async def count_input_tokens(self, request: Mapping[str, JsonValue]) -> int:
        if not isinstance(self.source, TokenCountingTransport):
            raise ProviderError(
                "invalid_request", retryable=False, native_code="unknown_input_tokens"
            )
        return await self.source.count_input_tokens(request)

    async def create(self, request: Mapping[str, JsonValue]) -> Object:
        request = deepcopy(dict(request))
        capped = model_request(dict(request))
        original = {} if capped else object_value(request["agent_config"])
        required = (
            self._native_agent.spec.model.id if self._native_agent is not None else self._primary
        )
        if (
            required not in (FLASH_PRIMARY, WORKER_MODEL, ARGON_MODEL)
            or (request.get("model") if capped else original.get("model")) != required
        ):
            raise ProviderError(
                "invalid_request", retryable=False, native_code="flash_primary_required"
            )
        models = (FLASH_PRIMARY,) if self._bounded_worker else FLASH_MODELS
        if required == ARGON_MODEL:
            # A 503 on Argon is the turn's answer, never a silent switch to Flash.
            models = (ARGON_MODEL,)
        if self._bounded_worker and (not capped or required not in (FLASH_PRIMARY, ARGON_MODEL)):
            raise ProviderError(
                "invalid_request", retryable=False, native_code="worker_model_required"
            )
        if self._native_agent is not None:
            if capped:
                raise ProviderError(
                    "invalid_request", retryable=False, native_code="named_agent_model_required"
                )
            if request.get(
                "agent"
            ) != self._native_agent.ref.id or self._native_agent.spec.model != ModelRef(
                provider="gemini", id=required
            ):
                raise ProviderError(
                    "invalid_request", retryable=False, native_code="named_agent_model_required"
                )
            # A retained native definition cannot be replaced by the inline fallback chain.
            models = (self._native_agent.spec.model.id,)
        for index, name in enumerate(models):
            model = ModelRef(provider="gemini", id=name)
            await self.accounting.before_attempt(model=model, attempt=index + 1)
            bounded = dict(request)
            if capped:
                bounded["model"] = name
            else:
                bounded["agent_config"] = {**original, "model": name}
            try:
                return await self._dispatch(bounded, model, index + 1)
            except ProviderError as error:
                # Only a 503 moves down the chain; a rejected request is the answer.
                if error.category != "overloaded" or error.native_code != "503":
                    raise
                if index + 1 == len(models):
                    raise
        raise ProviderError("upstream", retryable=False, native_code="flash_chain_exhausted")

    async def _dispatch(
        self, request: Mapping[str, JsonValue], model: ModelRef, attempt: int
    ) -> Object:
        """One create against its reserved hold, always followed by exactly one receipt.

        However the create ends, the receipt is published: a timeout, a cancel or a
        malformed reply after the send stays `unknown` (Google may have accepted it),
        so the hold is never left without an outcome and the next attempt in this
        context is never refused for a missing receipt.
        """
        receipt = FlashAttempt(model=model, attempt=attempt, outcome="unknown")
        try:
            reply = await self._create_within_rate_limits(request, model)
            # No fallback on malformed/lost acceptance or persistence failure.
            interaction = string(reply["id"])
            string(reply["status"])
            self._models[interaction] = model
            receipt = FlashAttempt(
                model=model, attempt=attempt, outcome="accepted", interaction_id=interaction
            )
            return reply
        except ProviderError as error:
            refused = error.category == "overloaded" and error.native_code == "503"
            rejected = definite_rejection(error)
            unsent = error.category == "transient_network" and error.native_code == "connect_failed"
            receipt = FlashAttempt(
                model=model,
                attempt=attempt,
                outcome="refused" if refused or rejected is not None or unsent else "unknown",
                http_status=503 if refused else rejected,
                sent=not unsent,
            )
            raise
        except CreateNeverSent:
            # Stopped while waiting out a rate limit: only 429s, nothing created.
            receipt = FlashAttempt(
                model=model, attempt=attempt, outcome="refused", http_status=429, sent=False
            )
            raise
        finally:
            # Awaited in this task, never shielded: the owner's per-attempt marker is
            # context-local, and a shielded task would clear only its own copy. A
            # create cancelled once still completes this await.
            unwinding = sys.exc_info()[1]
            try:
                await self.accounting.after_attempt(receipt=receipt)
            except Exception:
                # A failed receipt never replaces a cancel or timeout in flight (the
                # hold stays for the TTL); beside a provider error, or on success,
                # it still stops the chain.
                if unwinding is None or isinstance(unwinding, ProviderError):
                    raise
                log.warning(
                    "gemini_attempt_receipt_failed attempt=%s outcome=%s",
                    attempt,
                    receipt.outcome,
                )

    async def _create_within_rate_limits(
        self, request: Mapping[str, JsonValue], model: ModelRef
    ) -> Object:
        """One attempt's create, retried while Google only rate-limits it.

        A 429 created nothing, so the same attempt (and its one hold) retries:
        after Google's own retryDelay when it fits before the turn's deadline
        (`CREATE_DEADLINE`), else after an exponential backoff with jitter capped
        by the time left. A quota of 0, a delay past the deadline, or spent
        retries ends it with the cause and the delay to report. A cancel while
        waiting is `CreateNeverSent`.
        """
        retries = 0
        while True:
            try:
                return await self.source.create(request)
            except ProviderError as error:
                if not _rate_limited(error):
                    raise
                wait = _wait_s(error, retries, _room_s())
                if wait is None:
                    log.warning(
                        "gemini create rate limited: model=%s retries=%d retry_after_s=%s",
                        model.id,
                        retries,
                        error.retry_after_s,
                    )
                    raise ProviderError(
                        "rate_limited",
                        retryable=False,
                        native_code=(
                            "free_quota_exhausted"
                            if error.native_code == "free_quota_exhausted"
                            else "quota_exhausted"
                        ),
                        native_error_code=error.native_error_code,
                        native_param=error.native_param or model.id,
                        retry_after_s=error.retry_after_s,
                    ) from error
                log.info(
                    "gemini create rate limited, retrying: model=%s retry=%d wait_s=%.1f",
                    model.id,
                    retries + 1,
                    wait,
                )
                try:
                    await _sleep(wait)
                except asyncio.CancelledError:
                    raise CreateNeverSent() from None
                retries += 1

    def mount_echo_digests(self, interaction_id: str) -> frozenset[str]:
        digests = getattr(self.source, "mount_echo_digests", None)
        return frozenset() if digests is None else digests(interaction_id)

    def bind_mount_echo(self, interaction_id: str, digests: AbstractSet[str]) -> None:
        bind = getattr(self.source, "bind_mount_echo", None)
        if bind is not None:
            bind(interaction_id, digests)

    async def get(self, interaction_id: str) -> Object:
        reply = await self.source.get(interaction_id)
        await self._release_unbilled(interaction_id, reply)
        return reply

    async def cancel(self, interaction_id: str) -> Object:
        reply = await self.source.cancel(interaction_id)
        await self._release_unbilled(interaction_id, reply)
        return reply

    async def _release_unbilled(self, interaction_id: str, reply: Mapping[str, object]) -> None:
        """A cancelled interaction that never ran frees its hold at once.

        Never ran: cancelled, no usage, and no step but the caller's own input
        (Gemini's stamps cannot show it: `updated` stays equal to `created` even
        while a run works). Settlement otherwise waits for a usage reading that
        never comes. Anything that may have run keeps its hold for settlement or
        the TTL. Keyed by the
        interaction alone, so a cancel seen by another transport (a deadline
        cleanup, a later poll) releases it too. A failed release is logged and
        left to the hold's TTL, never raised into the turn.
        """
        release = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.accounting, "release_unbilled", None),
        )
        if (
            release is None
            or reply.get("status") != "cancelled"
            or reply.get("usage") is not None
            or not only_input_steps(reply.get("steps"))
        ):
            return
        try:
            await release(interaction_id=interaction_id)
        except Exception:
            log.warning("gemini_hold_release_failed interaction_id=%s", interaction_id)

    async def download_snapshot(self, environment_id: str) -> bytes:
        return await self.source.download_snapshot(environment_id)

    async def open_stream(
        self, interaction_id: str, *, after: str | None = None
    ) -> AsyncIterator[Object]:
        return await self.source.open_stream(interaction_id, after=after)
