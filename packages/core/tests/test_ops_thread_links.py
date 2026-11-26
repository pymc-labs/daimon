"""An issue event links to its conversation on Discord, Slack and Teams from stored ids alone."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.ops_overview import thread_url
from daimon.core.stores.domain import OpsEventRow, TenantRow

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
TEAMS_CHANNEL = "19:abc123@thread.tacv2"


def _tenant(platform: str, external_id: str) -> TenantRow:
    return TenantRow(
        id=uuid.uuid4(),
        platform=platform,
        external_id=external_id,
        provision_status="ready",
        registered_at=NOW,
        created_at=NOW,
    )


def _event(platform: str, channel_id: str | None, thread_id: str | None) -> OpsEventRow:
    return OpsEventRow(
        id=uuid.uuid4(),
        occurred_at=NOW,
        count=1,
        code="turn.failed",
        area="turn",
        severity="error",
        reason="timeout",
        fingerprint="f",
        agent_id=None,
        routine_id=None,
        watch_id=None,
        account_id=None,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        session_id=None,
        subject_key=None,
        release=None,
        note=None,
    )


@pytest.mark.parametrize(
    ("tenant", "event", "expected"),
    [
        (
            _tenant("discord", "111"),
            _event("discord", "222", "333"),
            "https://discord.com/channels/111/333",
        ),
        (
            _tenant("slack", "T0123ABC"),
            _event("slack", "C0123ABC", "1700000000.000100"),
            "https://app.slack.com/client/T0123ABC/C0123ABC/thread/C0123ABC-1700000000.000100",
        ),
        (
            _tenant("slack", "T0123ABC"),
            _event("slack", "C0123ABC", None),
            "https://app.slack.com/client/T0123ABC/C0123ABC",
        ),
        (
            _tenant("teams", "entra-1"),
            _event("teams", TEAMS_CHANNEL, f"{TEAMS_CHANNEL};messageid=1700000000000"),
            "https://teams.microsoft.com/l/message/19:abc123@thread.tacv2/1700000000000"
            "?tenantId=entra-1&parentMessageId=1700000000000",
        ),
    ],
    ids=["discord", "slack thread", "slack channel", "teams channel thread"],
)
def test_a_known_conversation_links_to_its_platform(
    tenant: TenantRow, event: OpsEventRow, expected: str
) -> None:
    assert thread_url(tenant, event) == expected


@pytest.mark.parametrize(
    ("tenant", "event"),
    [
        (_tenant("slack", "T0123ABC"), _event("slack", None, "1700000000.000100")),
        (_tenant("slack", "T0123ABC"), _event("slack", "not a channel", None)),
        (_tenant("teams", "entra-1"), _event("teams", "19:a_b@unq.gbl.spaces", None)),
        (_tenant("teams", "entra-1"), _event("teams", TEAMS_CHANNEL, None)),
        (_tenant("slack", "T0123ABC"), _event("discord", "222", "333")),
        (_tenant("web", "x"), _event("web", "1", "2")),
    ],
    ids=["slack no channel", "slack bad id", "teams chat", "teams no root", "mismatch", "web"],
)
def test_an_event_without_a_linkable_conversation_has_no_link(
    tenant: TenantRow, event: OpsEventRow
) -> None:
    assert thread_url(tenant, event) is None
    assert thread_url(None, event) is None
