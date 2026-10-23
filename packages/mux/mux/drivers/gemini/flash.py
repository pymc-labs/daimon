"""Explicit host Flash policy; no client creation, retry or implicit selection."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from collections.abc import Set as AbstractSet
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import JsonValue

from mux.contracts.ids import ModelRef
from mux.contracts.resources import Agent
from mux.drivers.gemini.mcp_auth import model_request
from mux.drivers.gemini.transport import (
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


class FlashAccounting(Protocol):
    """Caller persists a hold before each dispatch, and preserves its outcome.

    A failed accounting operation stops the chain. Actuals arrive via scoped
    Usage. The only free call this policy reports is a definite refusal: an
    HTTP 4xx that rejected the request itself (`refused` with a 4xx status), so
    no interaction exists. Anything Google may have accepted stays `unknown`.
    An owner may also offer `release_unbilled(interaction_id=...)`, called when
    an interaction is read back cancelled, unbilled and with no step but its input.
    """

    async def before_attempt(self, *, model: ModelRef, attempt: int) -> None: ...
    async def after_attempt(self, *, receipt: FlashAttempt) -> None: ...


_DEFINITE = ("invalid_request", "auth", "permission", "not_found")


def definite_rejection(error: ProviderError) -> int | None:
    """The 4xx status of a create Google rejected outright, else None.

    Needs the provider's own HTTP status: a local refusal (no status), e.g. a
    response check after a 200, may follow an accepted request. Timeouts, a bare
    429, 5xx and network failures may have been accepted too; a 429 naming its
    QuotaFailure was not.
    """
    code = error.native_error_code or ""
    if code == "http_429" and error.native_code in ("quota_zero", "quota_exhausted"):
        return 429  # Google's QuotaFailure: refused before any interaction existed
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
                reply = await self.source.create(bounded)
            except ProviderError as error:
                refused = error.category == "overloaded" and error.native_code == "503"
                rejected = definite_rejection(error)
                await self.accounting.after_attempt(
                    receipt=FlashAttempt(
                        model=model,
                        attempt=index + 1,
                        outcome="refused" if refused or rejected is not None else "unknown",
                        http_status=503 if refused else rejected,
                    )
                )
                # Only a 503 moves down the chain; a rejected request is the answer.
                if not refused or index + 1 == len(models):
                    raise
                continue
            # No fallback on malformed/lost acceptance or persistence failure.
            interaction = string(reply["id"])
            string(reply["status"])
            self._models[interaction] = model
            await self.accounting.after_attempt(
                receipt=FlashAttempt(
                    model=model,
                    attempt=index + 1,
                    outcome="accepted",
                    interaction_id=interaction,
                )
            )
            return reply
        raise ProviderError("upstream", retryable=False, native_code="flash_chain_exhausted")

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
