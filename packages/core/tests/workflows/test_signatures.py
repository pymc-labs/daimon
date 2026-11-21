"""Webhook verification presets against their providers' published vectors."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.workflows.signatures import hint_of, new_secret, verify

# Standard Webhooks' reference vector.
SW_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SW_BODY = b'{"test": 2432232314}'
SW_AT = datetime.fromtimestamp(1614265330, UTC)
SW_HEADERS = {
    "webhook-id": "msg_p5jXN8AQM9LWM0D4loKWxJek",
    "webhook-timestamp": "1614265330",
    "webhook-signature": "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=",
}
# Slack's documented signing example.
SLACK_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
SLACK_BODY = (
    b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id="
    b"G8PSS9T3V&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook"
    b"-collect&text=&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F3977"
    b"00885554%2F96rGlfmibIGlgcZRskXaIFfN&trigger_id=398738663015.47445629121.803a0bc887a14d"
    b"10d2c447fce8b6703c"
)
SLACK_AT = datetime.fromtimestamp(1531420618, UTC)
SLACK_HEADERS = {
    "x-slack-request-timestamp": "1531420618",
    "x-slack-signature": "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503",
}
# GitHub's documented example.
GH_HEADERS = {
    "x-hub-signature-256": "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
}


def test_standard_webhooks_reference_vector_verifies_and_tampering_fails() -> None:
    assert verify("standard-webhooks", [SW_SECRET], SW_HEADERS, SW_BODY, now=SW_AT), "vector"
    assert not verify("standard-webhooks", [SW_SECRET], SW_HEADERS, SW_BODY + b" ", now=SW_AT), (
        "a changed body fails"
    )
    late = SW_AT + timedelta(minutes=6)
    assert not verify("standard-webhooks", [SW_SECRET], SW_HEADERS, SW_BODY, now=late), (
        "a timestamp over five minutes off fails"
    )


def test_a_rotated_secret_still_verifies_during_its_overlap() -> None:
    assert verify("standard-webhooks", [new_secret(), SW_SECRET], SW_HEADERS, SW_BODY, now=SW_AT), (
        "the previous secret is tried after the newest"
    )
    assert not verify("standard-webhooks", [new_secret()], SW_HEADERS, SW_BODY, now=SW_AT), (
        "another secret fails"
    )


def test_github_documented_vector_verifies() -> None:
    secret = "It's a Secret to Everybody"
    assert verify("github", [secret], GH_HEADERS, b"Hello, World!", now=SW_AT), "vector"
    assert not verify("github", [secret], GH_HEADERS, b"Hello, World?", now=SW_AT), "tampered"


def test_slack_documented_vector_verifies_within_five_minutes() -> None:
    assert verify("slack", [SLACK_SECRET], SLACK_HEADERS, SLACK_BODY, now=SLACK_AT), "vector"
    late = SLACK_AT + timedelta(minutes=6)
    assert not verify("slack", [SLACK_SECRET], SLACK_HEADERS, SLACK_BODY, now=late), "stale"


def test_stripe_signature_verifies_any_v1_and_refuses_stale() -> None:
    secret, body, stamp = "whsec_stripe_test", b'{"id":"evt_1"}', "1700000000"
    good = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    header = {"stripe-signature": f"t={stamp},v1={'0' * 64},v1={good},v0=ignored"}
    at = datetime.fromtimestamp(int(stamp), UTC)
    assert verify("stripe", [secret], header, body, now=at), "one matching v1 is enough"
    assert not verify("stripe", [secret], header, body, now=at + timedelta(minutes=6)), "stale"


@pytest.mark.parametrize(
    ("header", "ok"),
    [("Bearer tok_1", True), ("bearer tok_1", True), ("Bearer tok_2", False), ("tok_1", False)],
)
def test_bearer_compares_the_token(header: str, ok: bool) -> None:
    assert verify("bearer", ["tok_1"], {"authorization": header}, b"", now=SW_AT) is ok, header


def test_a_scheme_without_secrets_refuses_and_no_scheme_passes() -> None:
    assert not verify("github", [], GH_HEADERS, b"Hello, World!", now=SW_AT), "no secret"
    assert verify(None, [], {}, b"", now=SW_AT), "an unverified trigger needs an admin's grant"


def test_new_secrets_follow_the_standard_format_and_hint_shows_four_characters() -> None:
    secret = new_secret()
    assert secret.startswith("whsec_") and len(secret) == 6 + 44, secret
    assert hint_of(secret) == f"whsec_...{secret[-4:]}", "the hint shows the last four"
