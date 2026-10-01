"""Executable guard that the two Discord posted-card renderers agree.

`daimon.adapters.mcp.tools.discord._posted_card.build_card_view` posts the
card; `daimon.adapters.discord.posted_controls.view.build_card_view` edits it
in place seconds later, from a different process. The two are duplicated on
purpose — the MCP adapter and the Discord adapter cannot import each other
(import-linter's independence contract) — but duplication without a guard
drifts silently: one extra separator, a different accent, a button built in
the other order, and the card visibly changes shape the instant a user
submits the form, with nothing else in the suite catching it. This module
imports both renderers directly (legitimate in a test, which is outside the
packages the contracts govern) and asserts byte-identical serialized
components for the same `PostedCard`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from daimon.adapters.discord.posted_controls.view import (
    build_card_view as discord_build_card_view,
)
from daimon.adapters.mcp.tools.discord._posted_card import (  # pyright: ignore[reportPrivateUsage]
    build_card_view as mcp_build_card_view,
)
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.credential_requests import build_skill_repo_target
from daimon.core.posted_controls import CardKind, CardState, PostedCard, build_posted_card

_EXPIRES_AT = datetime(2026, 4, 1, 12, 30, tzinfo=UTC)

#: Per-kind arguments `build_posted_card` demands: the mcp kind needs its
#: server url, the two repo kinds a branch and the repo the target packs.
_KIND_ARGS: dict[CardKind, dict[str, Any]] = {
    "env": {"target": "TOGGL_TOKEN"},
    "env_file": {"target": ".env"},
    "mcp": {"target": "linear", "mcp_server_url": "https://mcp.linear.app/sse"},
    "mcp_oauth": {"target": "notion", "mcp_server_url": "https://mcp.notion.com/mcp"},
    "repo": {
        "target": "https://github.com/acme/pipeline",
        "repo": "acme/pipeline",
        "branch": "main",
    },
    "skill_repo": {
        "target": build_skill_repo_target("https://github.com/acme/skills", "release", "skills"),
        "repo": "acme/skills",
        "branch": "release",
        "skill_repo_isolated": True,
    },
}

#: Per-state arguments, so every state in the machine is rendered by both
#: sides — the outcome and refusal states carry payloads the others reject.
_STATE_ARGS: dict[CardState, dict[str, Any]] = {
    "requested": {},
    "received": {},
    "applied": {
        "outcome": ConfigurationChange(
            target_name="research-bot", kind="key", detail="TOGGL_TOKEN", availability="saved"
        )
    },
    "partial": {
        "outcome": ConfigurationChange(
            target_name="research-bot",
            kind="key",
            detail="TOGGL_TOKEN",
            availability="preparation_failed",
        )
    },
    "refused": {"refusal": "replacement_admin_required"},
    "expired": {},
    "superseded": {},
}


def _card(kind: CardKind, state: CardState) -> PostedCard:
    return build_posted_card(
        kind=kind,
        state=state,
        agent_name="research-bot",
        responder_name="Daimon",
        requester_platform_user_id="100000000000000001",
        expires_at=_EXPIRES_AT,
        token="tok-abcdefghijklmnopqrst",
        **_KIND_ARGS[kind],
        **_STATE_ARGS[state],
    )


@pytest.mark.parametrize("state", list(_STATE_ARGS), ids=str)
@pytest.mark.parametrize("kind", list(_KIND_ARGS), ids=str)
def test_both_renderers_serialize_a_card_identically(kind: CardKind, state: CardState) -> None:
    card = _card(kind, state)

    assert (
        mcp_build_card_view(card).to_components() == discord_build_card_view(card).to_components()
    ), (
        f"the posting and editing renderers disagree on a {kind!r} card in state {state!r}; "
        "a user would see the card change shape the moment the form comes back"
    )
