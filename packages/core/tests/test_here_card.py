"""The fixed /here card has no model-written or credential-value fields."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import daimon.core.here_card as here_card_module
import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.agent_details import AgentDetails, GitHubDeploymentFacts, KeyEntry, McpServerEntry
from daimon.core.here_card import (
    CredentialStatus,
    HereCard,
    assemble_here_card,
    load_here_card,
    render_here_card_text,
)
from daimon.core.roster import Roster, RosterAgent
from daimon.core.rule_views import RuleViewer
from daimon.core.scope import ChannelConfigRow, DeploymentDefault, ResolvedConfig, TenantConfigRow
from daimon.core.stores.domain import McpOAuthGrantRow
from daimon.core.stores.platform_names import KnownName

TENANT = uuid.uuid4()
SETTER = uuid.uuid4()
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("state", "expected_lines"),
    [
        (
            "no_view",
            ["No channel access", "Ask an admin to check Daimon's access."],
        ),
        (
            "no_replies",
            ["Replies disabled here"],
        ),
        (
            "no_agent",
            ["No agent selected", "Ask an admin: /agent-setup"],
        ),
        (
            "blocked",
            [
                "ResearchBot can't answer here",
                "Reading: Any conversation",
                "Publishing: No approval",
            ],
        ),
        (
            "channel",
            ["ResearchBot answers here", "Reading: Any conversation", "Publishing: No approval"],
        ),
        (
            "thread",
            [
                "ResearchBot answers in this thread",
                "Reading: Any conversation",
                "Publishing: No approval",
            ],
        ),
    ],
)
def test_mcp_plain_text_states(state: str, expected_lines: list[str]) -> None:
    card = HereCard(
        channel_rule=ChannelRule(),
        who_may_answer="",
        reads_kept_inside=False,
        agent_name=None if state == "no_agent" else "ResearchBot",
        tier="thread" if state == "thread" else "channel",
        bot_can_view=state != "no_view",
        effective_writers="none" if state == "no_replies" else "any",
        agent_can_answer_here=state != "blocked",
        effective_readers="any",
        publishing_needs_approval=False,
        bot_can_read_history=True,
        credentials=(
            CredentialStatus(name="SECRET_NAME", kind="MCP token", configured=True),
            CredentialStatus(name="ghp_fake_value", kind="GitHub", configured=True),
        ),
        text="",
    )
    text = render_here_card_text(card)
    assert text.splitlines() == expected_lines
    assert "SECRET_NAME" not in text
    assert "ghp_fake_value" not in text
    assert "Channel setting. Threads can differ." not in text
    assert " · " not in text


def test_unknown_reading_publishing_and_history_drop_their_lines() -> None:
    card = HereCard(
        channel_rule=ChannelRule(),
        who_may_answer="",
        reads_kept_inside=False,
        agent_name="ResearchBot",
        tier="channel",
        bot_can_view=True,
        effective_writers="any",
        agent_can_answer_here=True,
        effective_readers="unknown",
        publishing_needs_approval=None,
        bot_can_read_history=None,
        text="",
    )
    shown = here_card_module.render_here_card(card)
    assert (shown.reading, shown.publishing, shown.extras) == (None, None, ())
    assert render_here_card_text(card).splitlines() == ["ResearchBot answers here"]


def test_card_title_fits_discord_and_slack_limits() -> None:
    card = HereCard(
        channel_rule=ChannelRule(),
        who_may_answer="",
        reads_kept_inside=False,
        agent_name="X" * 1000,
        tier="thread",
        bot_can_view=True,
        effective_writers="any",
        agent_can_answer_here=True,
        effective_readers="any",
        publishing_needs_approval=None,
        bot_can_read_history=None,
        text="",
    )
    assert len(here_card_module.render_here_card(card).title) <= 150


def test_status_uses_first_applicable_blocker() -> None:
    card = HereCard(
        channel_rule=ChannelRule(),
        who_may_answer="",
        reads_kept_inside=False,
        agent_name=None,
        tier=None,
        bot_can_view=False,
        effective_writers="none",
        agent_can_answer_here=None,
        effective_readers="any",
        publishing_needs_approval=None,
        bot_can_read_history=False,
        text="",
    )
    assert here_card_module.render_here_card(card).title == "No channel access"
    assert (
        here_card_module.render_here_card(card.model_copy(update={"bot_can_view": True})).title
        == "Replies disabled here"
    )


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
    assert card.set_at == (NOW if expected_setter is not None else None)
    assert card.set_by_label == ("Alex" if expected_setter is not None else None)
    assert "set_by_account_id" not in card.model_dump()
    assert "set by" not in card.text
    assert "configuring" not in card.text
    assert card.text.startswith(
        "helper answers in this thread" if tier == "thread" else "helper answers here"
    )


def test_own_readers_and_invisible_credentials_are_omitted() -> None:
    details = _details("hidden", (KeyEntry(name="SECRET_NAME", updated_at=NOW),))
    card = assemble_here_card(
        channel_id="private",
        agent_name="helper",
        tier="channel",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(
            channel_rules={"private": ChannelRule(readers="own", writers="own")},
            agent_rules={"helper": AgentRule(runs_in=("private", "outside"))},
        ),
        details=details,
        visible_channel_ids={"private"},
    )
    assert card.channel_rule.readers == "own"
    assert card.agent_runs_in == ("private",)
    assert card.reads_kept_inside
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
    assert "API_KEY" not in card.text
    assert "Credential" not in card.text
    assert card.publishing_needs_approval is False
    assert card.memory_writable_here


def test_discord_card_reports_history_separately_from_view() -> None:
    card = assemble_here_card(
        channel_id="one",
        agent_name="helper",
        tier="channel",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(),
        details=None,
        bot_can_view=True,
        caller_can_view=True,
        bot_can_read_history=False,
        caller_can_read_history=True,
    )
    assert "No access to earlier messages" in card.text


def test_channel_and_agent_rules_show_derived_limits() -> None:
    card = assemble_here_card(
        channel_id="home",
        agent_name="helper",
        tier="channel",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(
            channel_rules={"home": ChannelRule(readers="own", writers="own")},
            agent_rules={"helper": AgentRule(runs_in=("home",))},
        ),
        details=_details("helper"),
        visible_channel_ids={"home"},
    )
    assert card.channel_rule == ChannelRule(readers="own", writers="own")
    assert card.effective_readers == "own"
    assert card.agent_runs_in == ("home",)
    assert card.agent_home == "home"
    assert card.who_may_answer == "this channel's own agents"
    assert card.agent_can_answer_here
    assert card.reads_kept_inside
    assert card.memory_writable_here
    assert card.publishing_needs_approval
    assert "Publishing: Approval required" in card.text
    assert "Only own agents answer here" in card.text
    assert "sealed" not in card.text


def test_stricter_thread_and_category_rules_are_shown() -> None:
    card = assemble_here_card(
        channel_id="123",
        thread_id="456",
        category_id="category",
        agent_name="helper",
        tier="thread",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(
            channel_rules={"456": ChannelRule(readers="inside")},
            category_rules={"category": ChannelRule(writers="none")},
        ),
        details=_details("helper"),
    )
    assert card.channel_rule == ChannelRule()
    assert card.thread_rule == ChannelRule(readers="inside")
    assert card.category_rule == ChannelRule(writers="none")
    assert (card.effective_readers, card.effective_writers) == ("inside", "none")
    assert not card.agent_can_answer_here
    assert card.who_may_answer == "nobody"
    assert not card.memory_writable_here
    assert card.text.startswith("Replies disabled here")
    assert "rule:" not in card.text


def test_agent_alias_rules_intersect() -> None:
    card = assemble_here_card(
        channel_id="home",
        agent_name="helper",
        tier="channel",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(
            agent_rules={
                "helper": AgentRule(runs_in=("home", "second")),
                "alias": AgentRule(runs_in=("second",)),
            }
        ),
        details=_details("helper"),
        agent_rule_names=("helper", "alias"),
    )
    assert card.agent_runs_in == ("second",)
    assert not card.agent_can_answer_here


def test_thread_setter_and_configuration_target_share_routing_line() -> None:
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
        visible_channel_ids={"one", "two", "override"},
    )
    assert card.text.startswith("helper answers in this thread")
    assert "configured-agent" not in card.text
    assert "Alex" not in card.text
    assert "Other defaults" not in card.text
    assert "Routines:" not in card.text
    assert card.configuration_target_name == "configured-agent"


def test_card_shows_only_visible_runs_in_places() -> None:
    card = assemble_here_card(
        channel_id="home",
        agent_name="helper",
        tier="deployment",
        channel=None,
        tenant=None,
        configuration_target_name=None,
        policy=TenantAccessPolicy(
            channel_rules={"isolated": ChannelRule(readers="own", writers="own")},
            agent_rules={"helper": AgentRule(runs_in=("home", "allowed"))},
        ),
        details=_details("helper"),
        visible_channel_ids={"home", "allowed", "outside", "isolated"},
    )
    assert card.agent_runs_in == ("allowed", "home")
    assert "Other defaults" not in card.text


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
        visible_channel_ids={"home", *(f"channel-{i}" for i in range(300))},
        category_channels_bot_can_view=tuple(f"#channel-{i}: yes" for i in range(300)),
    )
    assert len(card.text) < 4000
    assert "channel-" not in card.text


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


async def test_loader_filters_setup_thread_for_non_admin_inside_own_readers_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = TenantAccessPolicy(
        channel_rules={"private": ChannelRule(readers="own", writers="own")},
        agent_rules={
            "daimon": AgentRule(runs_in=("private",)),
            "target": AgentRule(runs_in=("private",)),
        },
    )
    viewer = RuleViewer(policy, inside_channel_id="private")
    responder = RosterAgent(
        name="daimon", ma_agent_id="agent-id", model_id="test-model", is_built_in=True
    )
    details = _details("daimon").model_copy(
        update={"mcp_servers": (McpServerEntry(name="notes", url="https://example.test/mcp"),)}
    )
    other = uuid.uuid4()
    monkeypatch.setattr(here_card_module, "load_access_policy", AsyncMock(return_value=policy))
    rule_load = AsyncMock(return_value=viewer)
    monkeypatch.setattr(here_card_module, "load_rule_viewer", rule_load)
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
    setup_agent = AsyncMock(return_value=SimpleNamespace(name="daimon", metadata={}))
    monkeypatch.setattr(here_card_module, "get_setup_agent", setup_agent)
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
    setup_agent.assert_awaited_once()
    assert (
        here_card_module.load_agent_details.await_args.kwargs["preloaded_agent"]
        is setup_agent.return_value
    )
    assert card.configuration_target_name == "target"
    assert "Alex" not in card.text
    assert "set_by_account_id" not in card.model_dump()
    resolve_display.assert_awaited_once_with("123")
    assert "outside" not in card.text
    assert not any(item.kind == "personal OAuth" for item in card.credentials)
    assert "Routines:" not in card.text
    assert rule_load.await_args.kwargs["is_admin"] is False
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
    assert unknown.set_by_label == "unknown"
    assert "set by" not in unknown.text
    assert "<@123>" not in unknown.text


async def test_loader_names_a_teams_setter_from_the_stored_name_never_a_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Teams has no `<@id>` mention: the setter is named from their stored name."""
    entra_id = str(uuid.UUID(int=7))
    channel = "19:ops@thread.tacv2"
    monkeypatch.setattr(
        here_card_module, "load_access_policy", AsyncMock(return_value=TenantAccessPolicy())
    )
    monkeypatch.setattr(here_card_module, "load_rule_viewer", AsyncMock(return_value=None))
    responder = RosterAgent(
        name="helper", ma_agent_id="agent-id", model_id="test-model", is_built_in=False
    )
    monkeypatch.setattr(
        here_card_module, "load_roster", AsyncMock(return_value=Roster(answering=responder))
    )
    monkeypatch.setattr(
        here_card_module.scoped_config_read,
        "resolve",
        AsyncMock(return_value=ResolvedConfig(agent_name="helper", agent_name_tier="channel")),
    )
    row = ChannelConfigRow(
        tenant_id=TENANT,
        channel_id=channel,
        agent_name="helper",
        agent_name_set_by_account_id=SETTER,
        agent_name_set_at=NOW,
    )
    monkeypatch.setattr(
        here_card_module.scoped_config_read, "get_scope", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        here_card_module, "load_agent_details", AsyncMock(return_value=_details("helper"))
    )
    monkeypatch.setattr(
        here_card_module,
        "get_setup_agent",
        AsyncMock(return_value=SimpleNamespace(name="helper", metadata={})),
    )
    monkeypatch.setattr(
        here_card_module.agent_mcp_credentials, "list_credentials", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(here_card_module, "get_binding", AsyncMock(return_value=None))
    teams_lookup = AsyncMock(return_value=entra_id)
    monkeypatch.setattr(here_card_module, "get_teams_principal_for_account", teams_lookup)
    stored = AsyncMock(return_value={entra_id: KnownName(display_name="Ada <Lovelace>")})
    monkeypatch.setattr(here_card_module, "get_user_names", stored)

    async def load() -> str:
        card = await load_here_card(
            MagicMock(),
            MagicMock(),
            tenant_id=TENANT,
            platform="teams",
            channel_id=channel,
            thread_id=f"{channel};messageid=1",
            default=DeploymentDefault(agent_name="other"),
            github=GitHubDeploymentFacts(has_fallback_pat=False, app_configured=False),
            public_mcp_url=None,
            is_admin=False,
            caller_account_id=None,
        )
        # The compact card text omits provenance; the structured facts keep it.
        return card.set_by_label or ""

    named = await load()
    stored.return_value = {}
    unnamed = await load()

    assert named == "Ada ‹Lovelace›", "the stored name, made inert, names the setter"
    teams_lookup.assert_awaited_with(ANY, account_id=SETTER)
    assert unnamed == "unknown", "someone never named to us reads unknown"
    assert entra_id not in named + unnamed, "the Entra object id never reaches the card"
