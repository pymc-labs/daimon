"""A turn's origin id stays inside that turn: history the model reads is redacted."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from daimon.core.stores.domain import Role, TurnOriginRow
from daimon.core.turn_origin import (
    REDACTED_ORIGIN_ID,
    redact_turn_origin_ids,
    redact_turn_origin_text,
    render_turn_origin,
)
from pydantic import JsonValue

ORIGIN_ID = uuid.UUID("0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d")


def _origin(platform: str = "teams") -> TurnOriginRow:
    return TurnOriginRow.model_validate(
        {
            "id": ORIGIN_ID,
            "tenant_id": uuid.uuid4(),
            "account_id": uuid.uuid4(),
            "platform": platform,
            "parent_channel_id": "a:1personal-chat",
            "thread_id": "a:1personal-chat",
            "responder_ma_agent_id": "agt_daimon",
            "responder_name": "daimon",
            "configuration_target_ma_agent_id": None,
            "configuration_target_name": None,
            "role": Role.USER,
            "is_setup": False,
            "created_at": datetime(2026, 10, 20, tzinfo=UTC),
            "expires_at": datetime(2026, 10, 20, 1, tzinfo=UTC),
        }
    )


def test_the_turns_own_controls_carry_the_id_and_the_redaction_removes_only_it() -> None:
    rendered = render_turn_origin(_origin(), effective_is_admin=False)

    redacted = redact_turn_origin_text(rendered)

    assert str(ORIGIN_ID) in rendered, "the turn's own prompt must keep its id"
    assert str(ORIGIN_ID) not in redacted
    controls = json.loads(rendered.split("<turn_controls>\n", 1)[1].split("\n", 1)[0])
    kept = json.loads(redacted.split("<turn_controls>\n", 1)[1].split("\n", 1)[0])
    assert kept == {**controls, "origin_context_id": REDACTED_ORIGIN_ID}, (
        "every other control must be left as it was"
    )
    assert redacted.endswith("</turn_controls>"), "the block must stay well formed"


def test_every_platform_round_trips_through_the_renderer() -> None:
    for platform in ("teams", "discord", "slack"):
        rendered = render_turn_origin(_origin(platform), effective_is_admin=False)
        assert str(ORIGIN_ID) in rendered and str(ORIGIN_ID) not in redact_turn_origin_text(
            rendered
        ), platform


def test_ids_in_tool_inputs_nested_events_and_prose_are_redacted() -> None:
    event: dict[str, JsonValue] = {
        "type": "agent.mcp_tool_use",
        "input": {"path": "/me/messages", "origin_context_id": str(ORIGIN_ID)},
        "content": [
            {"type": "text", "text": f'{{"origin_context_id": "{ORIGIN_ID}", "x": 1}}'},
            {"type": "text", "text": f"Your origin_context_id is {ORIGIN_ID}."},
            {"type": "text", "text": f'escaped: {{\\"origin_context_id\\": \\"{ORIGIN_ID}\\"}}'},
        ],
        "count": 3,
        "flag": None,
    }

    redacted = redact_turn_origin_ids(event)

    assert str(ORIGIN_ID) not in json.dumps(redacted)
    assert isinstance(redacted, dict)
    assert redacted["input"] == {"path": "/me/messages", "origin_context_id": REDACTED_ORIGIN_ID}
    assert redacted["count"] == 3 and redacted["flag"] is None


def test_other_ids_and_words_are_left_alone() -> None:
    other = uuid.uuid4()
    text = f"session {other}, the origin_context_id field, origin_context_id to credential tools"

    assert redact_turn_origin_text(text) == text
    assert redact_turn_origin_ids({"origin_context_id": 5, "id": str(other)}) == {
        "origin_context_id": 5,
        "id": str(other),
    }
