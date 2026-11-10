"""Pure parts of press-applies confirmation: digest, custom ids and cards."""

import ast
import uuid
from pathlib import Path

import daimon.core.host_calls as host_calls
from daimon.core.host_calls import (
    canonical_call_digest,
    card_custom_id,
    parse_custom_id,
    wake_key,
)

_AGENT = uuid.uuid4()


def _digest(**changes: object) -> str:
    base: dict[str, object] = {
        "route": "daimon",
        "server": "https://mcp.example",
        "tool": "send_message",
        "args": {"channel": "c1", "text": "hi"},
        "executing_agent_id": _AGENT,
    }
    return canonical_call_digest(**{**base, **changes})  # pyright: ignore[reportArgumentType]


def test_digest_binds_route_server_tool_args_and_agent() -> None:
    """Changing any approved fact changes the digest, so a swapped call is refused."""
    base = _digest()
    for change in (
        {"route": "other"},
        {"server": "https://other.example"},
        {"tool": "delete_message"},
        {"args": {"channel": "c2", "text": "hi"}},
        {"executing_agent_id": uuid.uuid4()},
    ):
        assert _digest(**change) != base, f"{change} must change the digest"


def test_digest_ignores_key_order_and_the_turn_origin() -> None:
    """The origin id is rewritten at execution, so it is not part of what was approved."""
    assert _digest(args={"text": "hi", "channel": "c1"}) == _digest(), "key order is canonical"
    assert _digest(args={"channel": "c1", "text": "hi", "origin_context_id": "x"}) == _digest(), (
        "origin_context_id is undigested"
    )


def test_custom_id_round_trips_and_rejects_others() -> None:
    """Only the card's own button ids parse, so other controls never reach the press."""
    card = uuid.uuid4()
    assert parse_custom_id(card_custom_id(card, "approve")) == (card, "approve"), "round trip"
    assert parse_custom_id(f"swc:{card}:confirm") is None, "a switch card id is not ours"
    assert len(card_custom_id(card, "deny")) <= 100, "Discord caps custom ids at 100"


def test_wake_key_is_one_per_record() -> None:
    """A double press can't queue two wakes for one record."""
    card = uuid.uuid4()
    assert wake_key(card) == wake_key(card), "stable per record"
    assert wake_key(card) != wake_key(uuid.uuid4()), "distinct across records"


def test_host_calls_never_waits_on_listen_notify_or_sleep() -> None:
    """Delivery is wake-only: no polling sleeps and no LISTEN/NOTIFY in the module."""
    tree = ast.parse(Path(host_calls.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "sleep", f"line {node.lineno}: no sleep in host_calls"
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            upper = node.value.upper()
            assert "LISTEN " not in upper and "NOTIFY " not in upper, (
                f"line {node.lineno}: no LISTEN/NOTIFY in host_calls"
            )
