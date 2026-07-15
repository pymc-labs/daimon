"""Tests for conversation_workspace_slug pure helper."""

from __future__ import annotations

import re

from daimon.core.notebooks.publish import (
    _AGENT_SLUG_PATTERN,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.notebooks.workspace import conversation_workspace_slug


def test_returns_32_hex_chars() -> None:
    out = conversation_workspace_slug(principal_key="acct-123", conversation_id="channel-456")
    assert re.fullmatch(r"[a-f0-9]{32}", out), (
        "slug must be exactly 32 lowercase hex chars from blake2b(digest_size=16).hexdigest()"
    )


def test_same_inputs_produce_same_slug() -> None:
    a = conversation_workspace_slug(principal_key="acct-123", conversation_id="channel-456")
    b = conversation_workspace_slug(principal_key="acct-123", conversation_id="channel-456")
    assert a == b, "function must be deterministic — same inputs yield same slug"


def test_different_principals_produce_different_slugs() -> None:
    a = conversation_workspace_slug(principal_key="acct-123", conversation_id="ch")
    b = conversation_workspace_slug(principal_key="acct-124", conversation_id="ch")
    assert a != b, "different principals must isolate to different workspaces"


def test_different_conversations_produce_different_slugs() -> None:
    a = conversation_workspace_slug(principal_key="acct-123", conversation_id="channel-456")
    b = conversation_workspace_slug(principal_key="acct-123", conversation_id="channel-457")
    assert a != b, "different conversations under same principal must yield different slugs"


def test_empty_strings_still_produce_valid_slug() -> None:
    out = conversation_workspace_slug(principal_key="", conversation_id="")
    assert re.fullmatch(r"[a-f0-9]{32}", out), (
        "empty inputs are tolerated — caller is responsible for non-empty in normal flows"
    )


def test_output_is_resolvable_as_agent_slug() -> None:
    out = conversation_workspace_slug(principal_key="acct", conversation_id="chan")
    assert _AGENT_SLUG_PATTERN.fullmatch(out), (
        "output must be feedable to _resolve_slug as agent_slug"
    )
