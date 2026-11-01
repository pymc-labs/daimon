"""Chat accounts: the vouched claim's form, and which workspaces an account reaches."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core.chat_accounts import ChatAccount, parse_chat_accounts, reached_workspaces
from daimon.core.stores.domain import TenantRow

_TID = "6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f"
_OID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _tenant(platform: str, external_id: str, *, status: str = "ready") -> TenantRow:
    return TenantRow(
        id=uuid.uuid4(),
        platform=platform,
        external_id=external_id,
        provision_status=status,
        registered_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_parse_keeps_well_formed_accounts_once_and_drops_the_rest() -> None:
    parsed = parse_chat_accounts(
        [
            "discord:123456789012345678",
            "discord:123456789012345678",
            "slack:T01ABC:U02DEF",
            f"teams:{_TID.upper()}:{_OID}",
            "discord:not-a-snowflake",
            "slack:T01ABC",
            "teams:x:y",
            "github:octocat",
            "",
        ]
    )

    assert parsed == (
        ChatAccount("discord", "123456789012345678", None),
        ChatAccount("slack", "U02DEF", "T01ABC"),
        ChatAccount("teams", _OID, _TID),
    ), f"got {parsed!r}"


def test_loaded_list_decides_and_unloaded_falls_back_to_the_proof() -> None:
    synced_in = _tenant("discord", "1")
    synced_out = _tenant("discord", "2")
    unsynced_proven = _tenant("discord", "3")
    unsynced_unproven = _tenant("discord", "4")
    pending = _tenant("discord", "5", status="pending")

    reached = reached_workspaces(
        [synced_in, synced_out, unsynced_proven, unsynced_unproven, pending],
        platform="discord",
        proven={"2": "Left", "3": "Proven", "5": "Pending"},
        member_of={synced_in.id},
        synced={synced_in.id: "Listed", synced_out.id: "Left"},
    )

    assert [(w.tenant.id, w.name) for w in reached] == [
        (synced_in.id, "Listed"),
        (unsynced_proven.id, "Proven"),
    ], "a list decides, even against the sign-in; no list falls back to the sign-in"


def test_workspace_scoped_ids_match_only_the_proven_workspace() -> None:
    proven = _tenant("slack", "T1")
    elsewhere = _tenant("slack", "T2")

    reached = reached_workspaces(
        [proven, elsewhere],
        platform="slack",
        proven={"T1": "One"},
        member_of={proven.id, elsewhere.id},
        synced={proven.id: "One", elsewhere.id: "Two"},
    )

    assert [w.tenant.id for w in reached] == [proven.id], (
        "a Slack id is matched only in the workspace the sign-in proves"
    )
