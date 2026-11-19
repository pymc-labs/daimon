"""Anthropic SDK client policy shared by the driver's ports.

Mutations get one native attempt and no redirects; failures are classified for
the host's durable retry policy.
"""

from __future__ import annotations

import email.utils
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import httpx
from anthropic import (
    AnthropicError,
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    AsyncCallNext,
    Middleware,
)

from mux.state.mutation_retry import UNCLASSIFIED, MutationFailure, parse_retry_after

if TYPE_CHECKING:
    from anthropic._request import APIRequest
    from anthropic._response import AsyncAPIResponse

_CLAIMED_MUTATION: ContextVar[bool] = ContextVar("anthropic_claimed_mutation", default=False)


@contextmanager
def single_attempt_mutation() -> Iterator[None]:
    """A durable claim permits one native attempt, independently of read retries."""
    token = _CLAIMED_MUTATION.set(True)
    try:
        yield
    finally:
        _CLAIMED_MUTATION.reset(token)


class _NoRedirects(Middleware):
    """Send a claimed mutation exactly as built; never follow a redirect.

    The SDK's httpx client follows redirects by default, so one SDK attempt
    could be an accepted POST answered by a 3xx plus a redirected GET whose
    429 or connection failure would read as "the POST was refused". With
    redirects off, a 3xx surfaces as a status error and classifies `unknown`.
    """

    async def handle_async(
        self, request: APIRequest, call_next: AsyncCallNext
    ) -> AsyncAPIResponse[Any]:
        unredirected = request.copy()
        unredirected.options.follow_redirects = False
        return await call_next(unredirected)


_NO_REDIRECTS = _NoRedirects()


def mutation_client(client: AsyncAnthropic, *, single_attempt: bool = False) -> AsyncAnthropic:
    """Do not mutate the shared client or weaken unrelated tasks/read requests.

    A turn mutation or durable claim gets `max_retries=0`: the SDK would
    otherwise repeat a POST blindly (this SDK sends no idempotency header), and the host's
    persistence layer owns every retry as a fresh durable attempt. It also
    never follows a redirect (`_NoRedirects`), so every failure the host
    classifies belongs to the mutation request itself.
    """
    if not single_attempt and not _CLAIMED_MUTATION.get():
        return client
    return client.with_options(max_retries=0).with_middleware(_NO_REDIRECTS)


# Statuses that prove an Anthropic mutation was refused before it executed.
# Source: https://platform.claude.com/docs/en/api/errors (fetched 2026-10-23).
# 400 invalid_request_error, 401 authentication_error, 402 billing_error,
# 403 permission_error, 404 not_found_error: the request was refused as sent.
# 409 conflict_error: "the request conflicts with the current state of a
# resource"; it was not applied, and repeating the same bytes cannot help.
# 413 request_too_large: "Cloudflare returns this error before the request
# reaches the API servers". 422 is the SDK's unprocessable-entity mapping.
# None of these is retryable: the same request fails the same way.
_REFUSED_STATUSES = frozenset({400, 401, 402, 403, 404, 409, 413, 422})

# Transport failures raised before any request byte is written: no TCP/TLS
# connection was established, or none was handed out by the pool.
_UNSENT_TRANSPORT_ERRORS: tuple[tuple[type[httpx.RequestError], str], ...] = (
    (httpx.ConnectError, "connect_error"),
    (httpx.ConnectTimeout, "connect_timeout"),
    (httpx.PoolTimeout, "pool_timeout"),
)


def classify_mutation_failure(error: BaseException) -> MutationFailure:
    """What a failed claimed Anthropic mutation proves; conservative by default.

    Walks `__cause__` to the SDK exception, so both a raw SDK error and the
    driver's normalized `ProviderError` classify. No Anthropic endpoint is
    `repeatable`: the SDK sends no idempotency key, so a repeat after an
    ambiguous failure is never deduplicated.

    - 429 is `rejected`: the API rate-limits with a token bucket before work
      starts (docs: api/rate-limits; Managed Agents create endpoints have their
      own per-organization limit) and says "when the response has a
      `retry-after` header, wait that many seconds before retrying". It is
      retryable only with that header: the tier spend-cap 429 has none and
      "keeps failing until access resumes", and `enforced_spend_limit_reached`
      never retries. The docs never state outright that a 429'd Managed Agents
      POST had no effect; the evidence is the admission-control design.
    - `x-should-retry: false` from the server always forbids a retry.
    - 408, 5xx (500 api_error, 504 timeout_error "timed out while processing")
      and 529 overloaded_error are `unknown`: nothing says the request did not
      run, so they are never retried and are reconciled instead.
    - Connection failures are `rejected` and retryable only when httpx failed
      before writing the request (`_UNSENT_TRANSPORT_ERRORS`); a read, write or
      protocol failure may follow acceptance and is `unknown`.
    - A response or failure that belongs to any request other than the
      original one (a followed redirect) is `unknown`, whatever its status:
      the original request may have been accepted.
    - Anything else, including cancellation and host timeouts, is `unknown`.
    """
    cause = _sdk_cause(error)
    if isinstance(cause, APIStatusError):
        if cause.response.history:
            return MutationFailure("unknown", retryable=False, reason="redirected")
        return _classify_status(cause)
    if isinstance(cause, APIConnectionError):
        reason = never_written(cause)
        if reason is not None:
            return MutationFailure("rejected", retryable=True, reason=reason)
        return MutationFailure("unknown", retryable=False, reason="connection_lost")
    return UNCLASSIFIED


def never_written(error: APIConnectionError) -> str | None:
    """Why the SDK's own request provably never left this process, or None.

    True only for a connection, connect-timeout or pool failure raised for
    the very request the SDK built (not a redirect it followed).
    """
    transport = error.__cause__
    for kind, reason in _UNSENT_TRANSPORT_ERRORS:
        if isinstance(transport, kind):
            try:
                failed = transport.request
            except RuntimeError:
                return None
            original = error.request
            if failed is original or (failed.method, failed.url) == (
                original.method,
                original.url,
            ):
                return reason
            return None
    return None


def _sdk_cause(error: BaseException) -> AnthropicError | None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, AnthropicError):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


def _classify_status(error: APIStatusError) -> MutationFailure:
    status = error.status_code
    reason = f"http_{status}"
    headers = error.response.headers
    server_forbids_retry = headers.get("x-should-retry") == "false"
    if status == 429:
        retry_after = parse_retry_after(headers, now=_server_now(headers))
        retryable = (
            retry_after is not None
            and not server_forbids_retry
            and _error_code(error.body) != "enforced_spend_limit_reached"
        )
        return MutationFailure(
            "rejected", retryable=retryable, reason=reason, retry_after_s=retry_after
        )
    if status in _REFUSED_STATUSES:
        return MutationFailure("rejected", retryable=False, reason=reason)
    return MutationFailure("unknown", retryable=False, reason=reason)


def _server_now(headers: httpx.Headers) -> datetime:
    """An HTTP-date Retry-After is relative to the server's `date` header."""
    date = headers.get("date")
    if date is None:
        return datetime.now(UTC)
    try:
        parsed = cast(datetime, email.utils.parsedate_to_datetime(date))
    except (TypeError, ValueError, IndexError):
        return datetime.now(UTC)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _error_code(body: object) -> str | None:
    """`error.details.error_code`, which marks the spend-cap 429."""
    if not isinstance(body, Mapping):
        return None
    outer = cast(Mapping[str, object], body)
    nested = outer.get("error")
    detail = cast(Mapping[str, object], nested) if isinstance(nested, Mapping) else outer
    details = detail.get("details")
    if not isinstance(details, Mapping):
        return None
    code = cast(Mapping[str, object], details).get("error_code")
    return code if isinstance(code, str) else None
