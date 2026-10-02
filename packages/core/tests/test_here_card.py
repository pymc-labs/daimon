"""The fixed /here card has no model-written or credential-value fields."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import daimon.core.here_card as here_card_module
import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_details import AgentDetails, GitHubDeploymentFacts, KeyEntry, McpServerEntry
from daimon.core.channel_isolation import IsolationViewer
from daimon.core.here_card import assemble_here_card, load_here_card
from daimon.core.roster import Roster, RosterAgent
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, ResolvedConfig, TenantConfigRow
from daimon.core.stores.domain import McpOAuthGrantRow

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
        set_by_label="Alex",
        policy=TenantAccessPolicy(),
        details=None,
    )
    assert card.tier == tier
    assert ("Set by: Alex" in card.text) == (expected_setter is not None)
    assert "set_by_account_id" not in card.model_dump()
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
    assert card.text.count("Credential session usability: unknown") == 1


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
        set_by_label="Alex",
        policy=TenantAccessPolicy(),
        details=details,
        channels=(ChannelConfigRow(tenant_id=TENANT, channel_id="override", agent_name="other"),),
        deployment_default=DeploymentDefault(agent_name="other"),
        visible_channel_ids={"one", "two", "override"},
    )
    assert "Set by: Alex" in card.text
    assert card.default_channels == ("two",)
    assert card.configuration_target_name == "configured-agent"


def test_default_places_respect_pins_and_isolation() -> None:
    card = assemble_here_card(
        channel_id="home",
        agent_name="helper",
        tier="deployment",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(
            sealed_channel_ids=("isolated",),
            isolated_channel_ids=("isolated",),
            agent_channel_pins={"helper": ("home", "allowed")},
        ),
        details=_details("helper"),
        deployment_default=DeploymentDefault(agent_name="helper"),
        visible_channel_ids={"home", "allowed", "outside", "isolated"},
    )
    assert card.default_channels == ("allowed",)


def test_large_server_card_stays_under_discord_limit() -> None:
    card = assemble_here_card(
        channel_id="home",
        agent_name="helper",
        tier="deployment",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(),
        details=_details("helper"),
        deployment_default=DeploymentDefault(agent_name="helper"),
        visible_channel_ids={"home", *(f"channel-{i}" for i in range(300))},
        category_channels_bot_can_view=tuple(f"#channel-{i}: yes" for i in range(300)),
    )
    assert len(card.text) < 4000
    assert "+292 more" in card.text


def test_only_callers_personal_grant_is_named() -> None:
    caller = uuid.uuid4()
    other = uuid.uuid4()
    details = _details("helper").model_copy(
        update={"mcp_servers": (McpServerEntry(name="notes", url="https://example.test/mcp"),)}
    )
    card = assemble_here_card(
        channel_id="home",
        agent_name="helper",
        tier="channel",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(),
        details=details,
        caller_account_id=caller,
        personal_grants=(
            McpOAuthGrantRow(
                agent_id=uuid.uuid4(), account_id=other, mcp_server_url="https://example.test/mcp"
            ),
        ),
    )
    assert not any(item.kind == "personal OAuth" for item in card.credentials)


async def test_loader_filters_setup_thread_for_non_admin_inside_isolated_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = TenantAccessPolicy(
        sealed_channel_ids=("private",),
        isolated_channel_ids=("private",),
        agent_channel_pins={"daimon": ("private",), "target": ("private",)},
    )
    viewer = IsolationViewer(policy, inside_channel_id="private")
    responder = RosterAgent(
        name="daimon", ma_agent_id="agent-id", model_id="test-model", is_built_in=True
    )
    details = _details("daimon").model_copy(
        update={"mcp_servers": (McpServerEntry(name="notes", url="https://example.test/mcp"),)}
    )
    other = uuid.uuid4()
    monkeypatch.setattr(here_card_module, "load_access_policy", AsyncMock(return_value=policy))
    isolation_load = AsyncMock(return_value=viewer)
    monkeypatch.setattr(here_card_module, "load_isolation_viewer", isolation_load)
    monkeypatch.setattr(
        here_card_module, "load_roster", AsyncMock(return_value=Roster(answering=responder))
    )
    monkeypatch.setattr(
        here_card_module.scoped_config_read,
        "resolve",
        AsyncMock(
            return_value=ResolvedConfig(
                agent_name="daimon",
                agent_name_tier="thread",
                thread_binding_kind="setup",
                configuration_target_name="target",
            )
        ),
    )
    monkeypatch.setattr(
        here_card_module.scoped_config_read,
        "get_scope",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        here_card_module.scoped_config_read,
        "list_propagations_for_tenant",
        AsyncMock(return_value=(None, [])),
    )
    monkeypatch.setattr(here_card_module, "load_agent_details", AsyncMock(return_value=details))
    monkeypatch.setattr(
        here_card_module.agent_mcp_credentials,
        "list_credentials",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        here_card_module.mcp_oauth_flows,
        "list_completed_grants",
        AsyncMock(
            return_value=[
                McpOAuthGrantRow(
                    agent_id=uuid.uuid4(),
                    account_id=other,
                    mcp_server_url="https://example.test/mcp",
                )
            ]
        ),
    )
    routines_load = AsyncMock(return_value=[])
    monkeypatch.setattr(here_card_module, "list_routines_for_tenant", routines_load)
    monkeypatch.setattr(
        here_card_module,
        "get_binding",
        AsyncMock(return_value=SimpleNamespace(creator_account_id=SETTER, created_at=NOW)),
    )
    monkeypatch.setattr(
        here_card_module, "get_discord_principal_for_account", AsyncMock(return_value="123")
    )
    resolve_display = AsyncMock(return_value="Alex @team <@123>")
    card = await load_here_card(
        MagicMock(),
        MagicMock(),
        tenant_id=TENANT,
        platform="discord",
        channel_id="private",
        thread_id="thread",
        default=DeploymentDefault(agent_name="other"),
        github=GitHubDeploymentFacts(has_fallback_pat=False, app_configured=False),
        public_mcp_url=None,
        is_admin=False,
        caller_account_id=uuid.uuid4(),
        resolve_setter_display=resolve_display,
        visible_channel_ids={"private", "outside"},
        category_channels_bot_can_view=(("private", "#private: yes"), ("outside", "#outside: yes")),
    )
    assert card.agent_name == "daimon"
    assert card.configuration_target_name == "target"
    assert "Set by: Alex ＠team ‹＠123›" in card.text
    assert "set_by_account_id" not in card.model_dump()
    resolve_display.assert_awaited_once_with("123")
    assert "outside" not in card.text
    assert not any(item.kind == "personal OAuth" for item in card.credentials)
    assert card.routines == ()
    routines_load.assert_not_awaited()
    assert isolation_load.await_args.kwargs["is_admin"] is False
    resolve_display.return_value = None
    unknown = await load_here_card(
        MagicMock(),
        MagicMock(),
        tenant_id=TENANT,
        platform="discord",
        channel_id="private",
        thread_id="thread",
        default=DeploymentDefault(agent_name="other"),
        github=GitHubDeploymentFacts(has_fallback_pat=False, app_configured=False),
        public_mcp_url=None,
        is_admin=False,
        caller_account_id=uuid.uuid4(),
        resolve_setter_display=resolve_display,
    )
    assert "Set by: unknown" in unknown.text
    assert "<@123>" not in unknown.text
