"""End-to-end Sentry scrubbing: real `init_sentry` → SDK pipeline → transport.

These drive the production configuration (before_send, event scrubber,
locals/breadcrumb settings) and assert on the final envelope, not on
`_scrub_event` in isolation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

import pytest
import sentry_sdk
from daimon.core.observability import capture_exception_with_scope, init_sentry
from sentry_sdk.transport import Transport

if TYPE_CHECKING:
    from sentry_sdk.envelope import Envelope


class CapturingTransport(Transport):
    """Keeps every envelope item's payload in memory instead of sending it."""

    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[object] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        for item in envelope.items:
            self.payloads.append(
                item.payload.json if item.payload.json is not None else item.payload.bytes
            )

    def rendered(self) -> str:
        return repr(self.payloads)


def init_capturing_sentry(
    monkeypatch: pytest.MonkeyPatch, *, integrations: list[object] | None = None
) -> CapturingTransport:
    """Run the production `init_sentry`, adding only an in-memory transport."""
    transport = CapturingTransport()
    real_init: Callable[..., object] = sentry_sdk.init

    def _init_with_transport(*args: object, **kwargs: object) -> object:
        return real_init(*args, transport=transport, **kwargs)

    monkeypatch.setattr(sentry_sdk, "init", _init_with_transport)
    init_sentry(
        dsn="https://public@o0.ingest.sentry.io/0",
        environment="test",
        process="mcp",
        release=None,
        traces_sample_rate=1.0,
        integrations=integrations or [],  # pyright: ignore[reportArgumentType]
    )
    return transport


@pytest.fixture
def capturing_sentry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CapturingTransport]:
    transport = init_capturing_sentry(monkeypatch)
    try:
        yield transport
    finally:
        sentry_sdk.flush()
        sentry_sdk.init()  # reset to a disabled client


def _canary() -> str:
    return "canary-" + uuid.uuid4().hex


@pytest.mark.parametrize(
    "render",
    [
        lambda c: json.dumps({"api_key": c}),
        lambda c: json.dumps({"detail": {"access_token": c, "scope": "repo"}}),
        lambda c: f"HTTP 400 from provider: {json.dumps({'error': 'bad', 'refresh_token': c})}",
        lambda c: repr({"access_token": c}),
        lambda c: repr({"nested": [{"client_secret": c}]}),
        lambda c: f"rejected 'password': '{c}' for user",
        lambda c: f'oauth failed "code": "{c}"',
    ],
    ids=[
        "json",
        "nested-json",
        "json-in-text",
        "dict-repr",
        "nested-repr",
        "quoted-pair",
        "dq-pair",
    ],
)
def test_structured_secret_in_exception_text_never_reaches_the_transport(
    capturing_sentry: CapturingTransport, render: Callable[[str], str]
) -> None:
    canary = _canary()
    try:
        raise RuntimeError(render(canary))
    except RuntimeError as exc:
        capture_exception_with_scope(exc)
    sentry_sdk.flush()

    assert capturing_sentry.payloads, "the event was captured"
    assert canary not in capturing_sentry.rendered()
    assert "RuntimeError" in capturing_sentry.rendered()


def test_secret_in_a_log_message_never_reaches_the_transport(
    capturing_sentry: CapturingTransport,
) -> None:
    canary = _canary()
    sentry_sdk.capture_message(f"callback failed: {json.dumps({'token': canary})} code={canary}")
    sentry_sdk.flush()

    assert capturing_sentry.payloads
    assert canary not in capturing_sentry.rendered()


def test_transaction_events_are_scrubbed_too(capturing_sentry: CapturingTransport) -> None:
    canary = _canary()
    with sentry_sdk.start_transaction(name="oauth-callback", op="http.server") as txn:
        txn.set_data("url", f"https://h/oauth/callback?code={canary}&state={canary}")
        sentry_sdk.get_current_scope().set_context("request_info", {"token": canary})
    sentry_sdk.flush()

    assert capturing_sentry.payloads, "the transaction was captured"
    assert canary not in capturing_sentry.rendered()


@pytest.mark.parametrize(
    "render",
    [
        lambda c: f"boot failed: SLACK_BOT_TOKEN={c}",
        lambda c: f"env GITHUB_TOKEN={c} rejected",
        lambda c: f"my_api_key={c}",
        lambda c: f"DAIMON_DB_PASSWORD={c};",
        lambda c: f"X_CLIENT_SECRET: {c}",
        lambda c: f"password = {c}",
        lambda c: f"header Authorization: Basic {c}",
        lambda c: f"Authorization: token {c}",
        lambda c: f"connect postgresql://daimon:{c}@db:5432/daimon failed",
        lambda c: f"1 validation error: input_value='{c}', input_type=str",
        lambda c: f"signing key={c}",
    ],
    ids=[
        "prefixed-token",
        "github-token",
        "lower-api-key",
        "prefixed-password",
        "colon-secret",
        "spaced-password",
        "basic-auth",
        "token-scheme",
        "url-userinfo",
        "pydantic-input",
        "bare-key",
    ],
)
def test_free_text_secret_shapes_never_reach_the_transport(
    capturing_sentry: CapturingTransport, render: Callable[[str], str]
) -> None:
    canary = _canary()
    try:
        raise RuntimeError(render(canary))
    except RuntimeError as exc:
        capture_exception_with_scope(exc)
    sentry_sdk.flush()

    assert capturing_sentry.payloads
    assert canary not in capturing_sentry.rendered()


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens=4096 input_tokens=12 output_tokens=7",
        "session_id=3f2a author=ada",
        "exit code=1 state=done",
    ],
    ids=["token-counts", "ids-and-author", "code-and-state-in-prose"],
)
def test_ordinary_diagnostics_are_not_redacted(
    capturing_sentry: CapturingTransport, text: str
) -> None:
    try:
        raise RuntimeError(text)
    except RuntimeError as exc:
        capture_exception_with_scope(exc)
    sentry_sdk.flush()

    assert text in capturing_sentry.rendered()


def test_secret_tags_and_user_are_scrubbed(capturing_sentry: CapturingTransport) -> None:
    canary = _canary()
    sentry_sdk.set_tag("slack_bot_token", canary)
    sentry_sdk.set_tag("note", f"token={canary}")
    sentry_sdk.set_user({"id": "u1", "email": f"{canary}@example.invalid"})
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        capture_exception_with_scope(exc)
    sentry_sdk.flush()

    assert capturing_sentry.payloads
    assert canary not in capturing_sentry.rendered()


_ADVERSARIAL = {
    "unclosed-list-items": "[1," * 21000 + "]",
    "openers": "{" * 64000,
    "brackets": "[" * 32000 + "]" * 32000,
    "quoted-key-no-close": '"token":\'' * 7000,
    "quotes": "'" * 64000,
    "pairs": "a=" * 32000,
    "mixed": "{'a': [" * 9000,
    "colon-pairs": "a: " * 21000,
    "urls": "http://x" * 8000,
    "urls-then-query": "http://x" * 8000 + "?a=1",
    "url-query": ("http://h/" + "p" * 60 + "?") * 900,
    "quoted-keys": "'k': " * 12000,
    "secret-pairs": "password=" * 7200,
    "secret-lists": "password=[" * 6500,
    "secret-dicts": "password={" * 6500,
    "secret-colons": "token=a:" * 8000,
    "secret-flags": "-token=x" * 8000,
    "jwt-dashes": "eyJ-" * 16384,
    "sk-dashes": "sk--" * 16384,
    "xox-dashes": "xoxb-" * 13000,
    "fernet-runs": "gAAAAA" * 10900,
}


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_redaction_is_linear_on_adversarial_text(name: str) -> None:
    """Scrubbing runs on the capturing thread (often the event loop): it must stay fast."""
    import time

    from daimon.core.observability import _redact_secret_text  # pyright: ignore[reportPrivateUsage]

    text = _ADVERSARIAL[name]
    started = time.perf_counter()
    _redact_secret_text(text)
    elapsed = time.perf_counter() - started

    # ~100 ms target (each case runs in <=50 ms locally); headroom for loaded CI
    # hosts. The previous scan took >60 s on the unclosed-list case.
    assert elapsed < 0.25, f"{name}: {elapsed:.3f}s"


def test_text_over_the_size_cap_is_truncated_and_still_redacted() -> None:
    from daimon.core.observability import _redact_secret_text  # pyright: ignore[reportPrivateUsage]

    canary = _canary()
    out = _redact_secret_text(f"token={canary} " + "x" * 200_000)

    assert canary not in out
    assert len(out) < 70_000


def test_a_scrubber_failure_sends_a_stripped_event_not_the_original(
    monkeypatch: pytest.MonkeyPatch, capturing_sentry: CapturingTransport
) -> None:
    import daimon.core.observability as observability

    def _explode(*args: object, **kwargs: object) -> object:
        raise RecursionError("too deep")

    monkeypatch.setattr(observability, "_drop_frame_vars", _explode)
    canary = _canary()
    try:
        raise RuntimeError(f"token={canary}")
    except RuntimeError as exc:
        capture_exception_with_scope(exc)
    sentry_sdk.flush()

    rendered = capturing_sentry.rendered()
    assert "redaction failed" in rendered
    assert canary not in rendered
    assert "RuntimeError" in rendered


def _plain(canary: str) -> str:
    """GitHub tokens are alphanumeric: drop the canary's hyphen for those shapes."""
    return canary.replace("-", "")


def _hard_canary() -> str:
    """A canary holding the characters that end a naive value match."""
    return "hard-" + uuid.uuid4().hex[:8]


@pytest.mark.parametrize(
    "render",
    [
        lambda c: f'API_TOKEN="hello {c}"',
        lambda c: f"API_TOKEN='hello {c}'",
        lambda c: f'API_TOKEN="ab\\"{c}"',
        lambda c: f"API_TOKEN=ab:{c}",
        lambda c: f"API_TOKEN=ab;{c}",
        lambda c: f"API_TOKEN=ab,{c}",
        lambda c: f"API_TOKEN=ab={c}",
        lambda c: f"API_TOKEN=ab&{c}",
        lambda c: f"API_TOKEN=ab){c}",
        lambda c: f"API_TOKEN=ab'{c}",
        lambda c: f"API_TOKEN=;{c}",
        lambda c: f"run failed: --password={c}",
        lambda c: f"--api-key={c} rejected",
        lambda c: f"-token={c}",
        lambda c: f"cli --password {c} exited 1",
        lambda c: (
            f"Command '['git', 'push', '--password', '{c}']' returned non-zero exit status 1."
        ),
        lambda c: f'body: {{\\"token\\": \\"{c}\\"}}',
        lambda c: f"slack said xoxb-1234567890-{c}",
        lambda c: f"github ghp_{_plain(c)}abcdefghijklmnop",
        lambda c: f"pat github_pat_11AAAA_{_plain(c)}xyzxyzxyzxyz",
        lambda c: f"key sk-ant-api03-{c}abcdefghijkl",
        lambda c: f"key sk-proj-{c}abcdefghijklmnop",
        lambda c: f"DAIMON_CRYPTO__KEYS={c}",
        lambda c: (
            "{'token': "
            + "1" * 3
            + f", 'oauth': {{'client_secret': {{'v': '{c}'}}}}, 'n': "
            + "[1," * 40
            + ""
        ),
    ],
    ids=[
        "dq-space",
        "sq-space",
        "dq-escaped",
        "colon",
        "semicolon",
        "comma",
        "equals",
        "ampersand",
        "paren",
        "apostrophe",
        "empty-then",
        "flag-eq",
        "flag-eq-dash",
        "single-dash",
        "flag-space",
        "called-process",
        "escaped-json",
        "slack-token",
        "github-token",
        "github-pat",
        "anthropic-key",
        "openai-key",
        "keys-suffix",
        "unparseable-nested",
    ],
)
def test_hard_value_shapes_never_reach_the_transport(
    capturing_sentry: CapturingTransport, render: Callable[[str], str]
) -> None:
    canary = _hard_canary()
    try:
        raise RuntimeError(render(canary))
    except RuntimeError as exc:
        capture_exception_with_scope(exc)
    sentry_sdk.flush()

    assert capturing_sentry.payloads
    assert canary not in capturing_sentry.rendered()
    assert _plain(canary) not in capturing_sentry.rendered()


def test_span_tags_and_plain_headers_are_scrubbed() -> None:
    from daimon.core.observability import _scrub_event  # pyright: ignore[reportPrivateUsage]

    canary = _canary()
    event: dict[str, object] = {
        "type": "transaction",
        "spans": [{"op": "http", "tags": {"api_token": canary, "note": f"token={canary}"}}],
        "request": {"headers": {"Referer-Note": f"see token={canary}"}},
        "tags": {"count": 3},
    }

    scrubbed = _scrub_event(event, {})  # pyright: ignore[reportArgumentType]

    assert canary not in repr(scrubbed)
    assert scrubbed is not None and "[redaction failed]" not in repr(scrubbed)


def _best_of_three(fn: Callable[[], object]) -> float:
    import time

    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - started)
    return best


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
def test_redaction_time_grows_linearly(name: str) -> None:
    """Doubling an adversarial input must not much more than double the time."""
    from daimon.core.observability import _redact_secret_text  # pyright: ignore[reportPrivateUsage]

    text = _ADVERSARIAL[name][: 16 * 1024]
    single = _best_of_three(lambda: _redact_secret_text(text))
    double = _best_of_three(lambda: _redact_secret_text(text + text))

    assert double <= 3 * single + 0.01, f"{name}: {single:.4f}s -> {double:.4f}s"


def test_whole_event_scrub_stays_within_the_event_text_budget() -> None:
    """Past ~256 KB of text per event the rest is replaced, not scanned."""
    from daimon.core.observability import _scrub_event  # pyright: ignore[reportPrivateUsage]

    canary = _canary()
    big = "x" * (60 * 1024)
    event: dict[str, object] = {
        "exception": {
            "values": [{"type": "RuntimeError", "value": big} for _ in range(6)]
            + [{"type": "RuntimeError", "value": f"token={canary}"}]
        },
    }

    scrubbed = _scrub_event(event, {})  # pyright: ignore[reportArgumentType]

    rendered = repr(scrubbed)
    assert canary not in rendered
    assert "[redacted: event budget]" in rendered


def test_adversarial_event_is_not_much_slower_than_a_harmless_one() -> None:
    from daimon.core.observability import _scrub_event  # pyright: ignore[reportPrivateUsage]

    def _event(values: list[str]) -> dict[str, object]:
        return {"exception": {"values": [{"type": "E", "value": v} for v in values]}}

    adversarial = [_ADVERSARIAL[name] for name in sorted(_ADVERSARIAL)][:4]
    harmless = ["ok=1 status: fine " * (len(v) // 18) for v in adversarial]

    base = _best_of_three(lambda: _scrub_event(_event(list(harmless)), {}))  # pyright: ignore[reportArgumentType]
    hostile = _best_of_three(lambda: _scrub_event(_event(list(adversarial)), {}))  # pyright: ignore[reportArgumentType]

    assert hostile <= 5 * base + 0.05, f"{base:.3f}s vs {hostile:.3f}s"


@pytest.mark.parametrize(
    "render",
    [
        lambda c: f'password=a:token="b {c}"',
        lambda c: f"Authorization: Token ab;{c}",
        lambda c: f"socket token xapp-1-{c}ABCDEFGH",
        lambda c: f"jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIx{_plain(c)}.c2lnbmF0dXJlLXZhbHVl",
        lambda c: f"body a=1%26client_secret%3D{c}%26b=2",
        lambda c: f"POST https://discord.com/api/webhooks/123456789/{c} returned 404",
    ],
    ids=["overlap", "auth-token-scheme", "xapp", "jwt", "form-encoded", "discord-webhook"],
)
def test_more_secret_shapes_never_reach_the_transport(
    capturing_sentry: CapturingTransport, render: Callable[[str], str]
) -> None:
    canary = _hard_canary()
    try:
        raise RuntimeError(render(canary))
    except RuntimeError as exc:
        capture_exception_with_scope(exc)
    sentry_sdk.flush()

    assert capturing_sentry.payloads
    assert canary not in capturing_sentry.rendered()
    assert _plain(canary) not in capturing_sentry.rendered()


@pytest.mark.parametrize(
    "render",
    [
        lambda c: f"GET https://h/cb?msg=a%20b&uid={c} failed x%3Dy",
        lambda c: f"GET https://h/oauth?error=bad%20scope&code={c}%26",
        lambda c: f"body password=ab%20{c}%26next=1",
        lambda c: f"body a=1%2526client_secret%253D{c}",
    ],
    ids=[
        "encoded-space-then-param",
        "oauth-encoded-space",
        "password-encoded-space",
        "double-encoded",
    ],
)
def test_percent_encoded_text_never_leaks_later_params(
    capturing_sentry: CapturingTransport, render: Callable[[str], str]
) -> None:
    canary = _hard_canary()
    try:
        raise RuntimeError(render(canary))
    except RuntimeError as exc:
        capture_exception_with_scope(exc)
    sentry_sdk.flush()

    assert capturing_sentry.payloads
    assert canary not in capturing_sentry.rendered()
