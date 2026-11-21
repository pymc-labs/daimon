"""The scope matcher: an unattended write inside a step's `writes_to` goes out without a person."""

from __future__ import annotations

from typing import Any

import pytest
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.workflows.spec import WriteScope
from daimon.core.workflows.unattended import Call, decide_unattended_call, scope_allows
from pydantic import JsonValue

SEND = WriteScope.model_validate(
    {
        "tool": "gmail/send_message",
        "where": {"to": {"domains": ["example.com"]}, "meta.label": {"in": ["sent", 1]}},
        "free": ["subject", "body"],
    }
)
UPDATE = WriteScope.model_validate(
    {
        "tool": "notes/update_page",
        "where": {"page_id": {"created_by": {"tool": "notes/create_page", "id": "id"}}},
        "free": ["text"],
    }
)
CREATED = {"notes/create_page": {"p1"}}


def _send(**args: JsonValue) -> bool:
    return scope_allows([SEND], "gmail/send_message", {"meta": {"label": "sent"}, **args}, {})


@pytest.mark.parametrize(
    ("to", "allowed"),
    [
        ("ann@example.com", True),
        ("Ann@EXAMPLE.com", True),
        (["ann@example.com", "bob@example.com"], True),
        (["ann@example.com", "eve@evil.com"], False),
        ([], False),
        ("eve@evil.example.com", False),
        ("eve@example.com.evil.com", False),
        ("Ann <eve@evil.com> ann@example.com", False),
        ("eve@evil.com, ann@example.com", False),
        ('"ann@example.com"@evil.com', False),
        ("ann@example.com\nBcc: eve@evil.com", False),
        (1, False),
    ],
)
def test_an_address_passes_only_inside_the_listed_domains(to: JsonValue, allowed: bool) -> None:
    assert _send(to=to, subject="Hi") is allowed


def test_every_argument_is_free_or_passes_its_rule() -> None:
    assert _send(to="a@example.com", body="x"), "free arguments pass"
    assert not _send(to="a@example.com", bcc="eve@evil.com"), "an unnamed argument fails"
    assert not scope_allows([SEND], "gmail/send_message", {"to": "a@example.com"}, {}), (
        "a where argument must be there"
    )
    assert not _send(to="a@example.com", meta={"label": "sent", "extra": 1}), (
        "nested keys are covered one by one"
    )
    assert not scope_allows([SEND], "gmail/send_reply", {"to": "a@example.com"}, {}), (
        "the scope names one tool"
    )


@pytest.mark.parametrize(("label", "allowed"), [("sent", True), (1, True), (True, False)])
def test_listed_values_match_by_type(label: JsonValue, allowed: bool) -> None:
    args: dict[str, JsonValue] = {"to": "a@example.com", "meta": {"label": label}}
    assert scope_allows([SEND], "gmail/send_message", args, {}) is allowed


def test_created_by_allows_only_ids_this_run_made() -> None:
    assert scope_allows([UPDATE], "notes/update_page", {"page_id": "p1", "text": "x"}, CREATED)
    assert not scope_allows([UPDATE], "notes/update_page", {"page_id": "p2"}, CREATED)


def test_a_matched_scope_writes_even_when_untrusted_data_steers_the_step() -> None:
    def verdict(**kw: Any) -> tuple[str, str | None]:
        call = Call("gmail", "send_message", "allow", exposed=True, **kw)
        d = decide_unattended_call(ToolSafetyPolicy(), call)
        return d.verdict, d.reason

    inside = {"to": "a@example.com", "meta": {"label": "sent"}}
    assert verdict(args=inside, scopes=[SEND]) == ("allow", None)
    assert verdict(args={**inside, "to": "e@evil.com"}, scopes=[SEND]) == ("propose", "untrusted")
    assert verdict(args=inside) == ("propose", "untrusted"), "no scopes while the feature is off"
    assert verdict(args=inside, scopes=[SEND], test=True)[1] == "test_run"
