"""Mutation retry rules and the Anthropic driver's failure classification.

Classification table (evidence in `classify_mutation_failure`'s docstring):

| Failure                                   | delivery | retryable |
| ----------------------------------------- | -------- | --------- |
| 429 with Retry-After (seconds/ms/date)    | rejected | yes       |
| 429 without Retry-After (tier spend cap)  | rejected | no        |
| 429 `enforced_spend_limit_reached`        | rejected | no        |
| any status with `x-should-retry: false`   | as below | no        |
| 400 401 402 403 404 409 413 422           | rejected | no        |
| 408, 5xx, 529, other statuses             | unknown  | no        |
| connect error/timeout, pool timeout       | rejected | yes       |
| read/write/protocol errors, anything else | unknown  | no        |
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import anthropic
import httpx
import pytest
from mux.drivers.anthropic import classify_mutation_failure
from mux.errors import ProviderError
from mux.state.mutation_retry import (
    UNCLASSIFIED,
    MutationFailure,
    MutationRetryPolicy,
    parse_retry_after,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
REQUEST = httpx.Request("POST", "https://anthropic.invalid/v1/sessions/s/events")


def status_error(
    status: int, headers: dict[str, str] | None = None, *, body: object = None
) -> anthropic.APIStatusError:
    response = httpx.Response(status, headers=headers, request=REQUEST)
    error_type = {429: anthropic.RateLimitError, 400: anthropic.BadRequestError}.get(
        status, anthropic.APIStatusError
    )
    return error_type("refused", response=response, body=body)


def connection_error(
    cause: httpx.RequestError, *, failed: httpx.Request | None = REQUEST
) -> anthropic.APIConnectionError:
    """The SDK's wrapping of an httpx failure; `failed` is the request httpx was sending."""
    if failed is not None:
        cause.request = failed
    try:
        try:
            raise cause
        except Exception as error:
            raise anthropic.APIConnectionError(request=REQUEST) from error
    except anthropic.APIConnectionError as wrapped:
        return wrapped


def normalized(error: anthropic.AnthropicError) -> ProviderError:
    """The driver's boundary keeps the SDK error as the cause."""
    try:
        raise ProviderError("upstream", retryable=False) from error
    except ProviderError as wrapped:
        return wrapped


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "3"}, 3.0),
        ({"Retry-After": "1.5"}, 1.5),
        ({"retry-after-ms": "250", "retry-after": "9"}, 0.25),
        ({"retry-after": "Fri, 23 Oct 2026 12:00:30 GMT"}, 30.0),
        ({"retry-after": "Fri, 23 Oct 2026 11:59:00 GMT"}, 0.0),
        ({"retry-after": "-4"}, 0.0),
        ({"retry-after": "soon"}, None),
        ({"retry-after": "nan"}, None),
        ({}, None),
    ],
)
def test_retry_after_reads_seconds_milliseconds_and_http_dates(
    headers: dict[str, str], expected: float | None
) -> None:
    assert parse_retry_after(headers, now=NOW) == expected


RATE_LIMITED = MutationFailure("rejected", retryable=True, reason="http_429")


def test_backoff_grows_exponentially_with_bounded_jitter() -> None:
    policy = MutationRetryPolicy(base_delay_s=0.5, max_delay_s=8.0)
    assert [policy.backoff_s(n, random=lambda: 0.0) for n in range(6)] == [
        0.5,
        1.0,
        2.0,
        4.0,
        8.0,
        8.0,
    ]
    assert policy.backoff_s(0, random=lambda: 1.0) == 0.375


RETRY_AFTER_7 = MutationFailure("rejected", True, "http_429", retry_after_s=7.0)


@pytest.mark.parametrize(
    ("failure", "attempts", "elapsed", "since", "remaining", "wait", "stop"),
    [
        (RATE_LIMITED, 1, 0.0, 0.0, None, 0.5, None),
        (RATE_LIMITED, 3, 0.0, 0.0, None, 2.0, None),
        # Never earlier than Retry-After, even when the backoff is shorter.
        (RETRY_AFTER_7, 1, 0.0, 0.0, None, 7.0, None),
        # A later invocation waits only for what is left of it...
        (RETRY_AFTER_7, 1, 0.0, 3.0, None, 4.0, None),
        (RETRY_AFTER_7, 1, 0.0, 10.0, None, 0.0, None),
        # ...unless that does not fit its own deadline.
        (RETRY_AFTER_7, 1, 0.0, 3.0, 2.0, 0.0, "budget_exhausted"),
        (RATE_LIMITED, 5, 0.0, 0.0, None, 0.0, "attempts_exhausted"),
        (
            MutationFailure("rejected", True, "x", retry_after_s=61.0),
            1,
            0.0,
            0.0,
            None,
            0.0,
            "retry_after_too_long",
        ),
        (
            MutationFailure("rejected", True, "x", retry_after_s=5.0),
            1,
            0.0,
            0.0,
            4.0,
            0.0,
            "budget_exhausted",
        ),
        (RATE_LIMITED, 1, 89.9, 0.0, None, 0.0, "budget_exhausted"),
        # A later invocation after the elapsed budget is spent.
        (RATE_LIMITED, 1, 1.0, 95.0, None, 0.0, "budget_exhausted"),
        (RATE_LIMITED, 1, 0.0, 0.0, 0.0, 0.0, "budget_exhausted"),
        (MutationFailure("rejected", False, "http_401"), 1, 0.0, 0.0, None, 0.0, "not_retryable"),
        (UNCLASSIFIED, 1, 0.0, 0.0, None, 0.0, "not_retryable"),
    ],
)
def test_policy_decides_from_the_recorded_times(
    failure: MutationFailure,
    attempts: int,
    elapsed: float,
    since: float,
    remaining: float | None,
    wait: float,
    stop: str | None,
) -> None:
    """`elapsed`: attempt 0 to the refusal; `since`: the refusal to now."""
    refused_at = NOW + timedelta(seconds=elapsed)
    decision = MutationRetryPolicy().decide(
        failure,
        attempts_made=attempts,
        first_began=NOW,
        refused_at=refused_at,
        now=refused_at + timedelta(seconds=since),
        remaining_s=remaining,
        random=lambda: 0.0,
    )
    assert (decision.wait_s, decision.stop) == (wait, stop)


@pytest.mark.parametrize(
    "value",
    [None, {}, {"delivery": "maybe", "retryable": True, "reason": "x"}, "rejected"],
)
def test_a_malformed_persisted_classification_reads_as_absent(value: object) -> None:
    assert MutationFailure.from_json(value) is None


def test_a_persisted_classification_round_trips() -> None:
    assert MutationFailure.from_json(RETRY_AFTER_7.as_json()) == RETRY_AFTER_7


def test_an_unknown_outcome_can_never_be_marked_retryable() -> None:
    with pytest.raises(ValueError, match="never retryable"):
        MutationFailure("unknown", retryable=True, reason="http_500")


@pytest.mark.parametrize(
    ("error", "delivery", "retryable", "reason"),
    [
        (status_error(429, {"retry-after": "2"}), "rejected", True, "http_429"),
        (status_error(429, {"retry-after-ms": "20"}), "rejected", True, "http_429"),
        (
            status_error(
                429,
                {
                    "date": "Fri, 23 Oct 2026 12:00:00 GMT",
                    "retry-after": "Fri, 23 Oct 2026 12:00:05 GMT",
                },
            ),
            "rejected",
            True,
            "http_429",
        ),
        (status_error(429), "rejected", False, "http_429"),
        (
            status_error(
                429,
                {"retry-after": "2"},
                body={
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "details": {"error_code": "enforced_spend_limit_reached"},
                    },
                },
            ),
            "rejected",
            False,
            "http_429",
        ),
        (
            status_error(429, {"retry-after": "2", "x-should-retry": "false"}),
            "rejected",
            False,
            "http_429",
        ),
        (status_error(400), "rejected", False, "http_400"),
        (status_error(401), "rejected", False, "http_401"),
        (status_error(402), "rejected", False, "http_402"),
        (status_error(403), "rejected", False, "http_403"),
        (status_error(404), "rejected", False, "http_404"),
        (status_error(409), "rejected", False, "http_409"),
        (status_error(413), "rejected", False, "http_413"),
        (status_error(422), "rejected", False, "http_422"),
        (status_error(408), "unknown", False, "http_408"),
        (status_error(410), "unknown", False, "http_410"),
        (status_error(500), "unknown", False, "http_500"),
        (status_error(503, {"x-should-retry": "true"}), "unknown", False, "http_503"),
        (status_error(504), "unknown", False, "http_504"),
        (status_error(529), "unknown", False, "http_529"),
        (connection_error(httpx.ConnectError("refused")), "rejected", True, "connect_error"),
        (connection_error(httpx.ConnectTimeout("slow")), "rejected", True, "connect_timeout"),
        (connection_error(httpx.PoolTimeout("busy")), "rejected", True, "pool_timeout"),
        (connection_error(httpx.ReadError("lost")), "unknown", False, "connection_lost"),
        (connection_error(httpx.WriteError("partial")), "unknown", False, "connection_lost"),
        (connection_error(httpx.ReadTimeout("slow")), "unknown", False, "connection_lost"),
        (connection_error(httpx.RemoteProtocolError("eof")), "unknown", False, "connection_lost"),
    ],
)
def test_anthropic_failures_classify_raw_and_normalized(
    error: anthropic.AnthropicError, delivery: str, retryable: bool, reason: str
) -> None:
    for observed in (error, normalized(error)):
        failure = classify_mutation_failure(observed)
        assert (failure.delivery, failure.retryable, failure.reason) == (
            delivery,
            retryable,
            reason,
        )


def test_a_redirected_or_foreign_request_failure_is_unknown() -> None:
    """One SDK attempt may follow a redirect: its failure says nothing about the POST."""
    redirect = httpx.Response(303, headers={"location": "/elsewhere"}, request=REQUEST)
    response = httpx.Response(
        429,
        headers={"retry-after": "1"},
        request=httpx.Request("GET", "https://anthropic.invalid/elsewhere"),
        history=[redirect],
    )
    after_redirect = anthropic.RateLimitError("slow down", response=response, body=None)
    elsewhere = httpx.Request("GET", "https://anthropic.invalid/elsewhere")
    for error in (
        after_redirect,
        connection_error(httpx.ConnectError("refused"), failed=elsewhere),
        connection_error(httpx.ConnectError("refused"), failed=None),
        status_error(303, {"location": "/elsewhere"}),
    ):
        failure = classify_mutation_failure(error)
        assert (failure.delivery, failure.retryable) == ("unknown", False)


def test_the_date_header_anchors_an_http_date_retry_after() -> None:
    failure = classify_mutation_failure(
        status_error(
            429,
            {
                "date": "Fri, 23 Oct 2026 12:00:00 GMT",
                "retry-after": "Fri, 23 Oct 2026 12:00:05 GMT",
            },
        )
    )
    assert failure.retry_after_s == 5.0


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ProviderError("rate_limited", retryable=True, native_code="429"),
        RuntimeError("not a provider failure"),
    ],
)
def test_anything_without_an_anthropic_cause_is_unclassified(error: BaseException) -> None:
    # Includes another driver's ProviderError: its cause is not Anthropic's.
    assert classify_mutation_failure(error) == UNCLASSIFIED
