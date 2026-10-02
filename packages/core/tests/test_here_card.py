"""The fixed /here card has no model-written or credential-value fields."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_details import AgentDetails, KeyEntry
from daimon.core.here_card import assemble_here_card
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, TenantConfigRow

TENANT = uuid.uuid4()
SETTER = uuid.uuid4()
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _details(name: str, keys: tuple[KeyEntry, ...] = ()) -> AgentDetails:
    return AgentDetails(
        ma_agent_id="agent-id",
        name=name,
        model_id="test-model",
        model_display_name="Test model",
        daimon_managed=False,
        created_by_is_workspace=False,
        created_at=NOW,
        answers_here=name == "helper",
        keys=keys,
        applies_note="test",
    )


@pytest.mark.parametrize(
    ("tier", "expected_setter"),
    [("thread", None), ("channel", SETTER), ("tenant", SETTER), ("deployment", None)],
)
def test_winning_tier_and_attribution(tier: str, expected_setter: uuid.UUID | None) -> None:
    card = assemble_here_card(
        channel_id="one",
        agent_name="helper",
        tier=tier,
        channel=ChannelConfigRow(
            tenant_id=TENANT,
            channel_id="one",
            agent_name="helper",
            agent_name_set_by_account_id=SETTER,
            agent_name_set_at=NOW,
        ),
        tenant=TenantConfigRow(
            tenant_id=TENANT,
            agent_name="helper",
            agent_name_set_by_account_id=SETTER,
            agent_name_set_at=NOW,
        ),
        configuration_target_name="target" if tier == "thread" else None,
        policy=TenantAccessPolicy(),
        details=None,
    )
    assert card.tier == tier
    assert card.set_by_account_id == expected_setter
    assert ("Configuration target: target." in card.text) == (tier == "thread")


def test_isolation_and_invisible_credentials_are_omitted() -> None:
    details = _details("hidden", (KeyEntry(name="SECRET_NAME", updated_at=NOW),))
    card = assemble_here_card(
        channel_id="private",
        agent_name="helper",
        tier="channel",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(
            sealed_channel_ids=("private",),
            isolated_channel_ids=("private",),
            agent_channel_pins={"helper": ("private", "outside")},
        ),
        details=details,
        visible_channel_ids={"private"},
    )
    assert card.sealed and card.isolated
    assert card.pin_channels == ("private",)
    assert "SECRET_NAME" not in card.text
    assert card.credentials == ()


def test_key_names_only_and_session_status_unknown() -> None:
    details = _details("helper", (KeyEntry(name="API_KEY", updated_at=NOW),))
    card = assemble_here_card(
        channel_id="one",
        agent_name="helper",
        tier="channel",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(),
        details=details,
    )
    assert card.credentials[0].name == "API_KEY"
    assert card.credentials[0].usable_in_session is None
    assert "session unknown" in card.text


def test_thread_setter_and_visible_workspace_defaults() -> None:
    details = _details("helper")
    card = assemble_here_card(
        channel_id="one",
        agent_name="helper",
        tier="thread",
        channel=None,
        tenant=TenantConfigRow(tenant_id=TENANT, agent_name="helper"),
        configuration_target_name="configured-agent",
        thread_set_by_account_id=SETTER,
        thread_set_at=NOW,
        policy=TenantAccessPolicy(),
        details=details,
        channels=(ChannelConfigRow(tenant_id=TENANT, channel_id="override", agent_name="other"),),
        deployment_default=DeploymentDefault(agent_name="other"),
        visible_channel_ids={"one", "two", "override"},
    )
    assert card.set_by_account_id == SETTER
    assert card.default_channels == ("two",)
    assert card.configuration_target_name == "configured-agent"
