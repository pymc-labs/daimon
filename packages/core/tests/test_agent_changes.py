"""Draft changes: the effect map against the session snapshot, the diff, and the MA write."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core import agent_changes, session_snapshot
from daimon.core.agent_editing import PlaceEdit
from daimon.core.agent_view import (
    AgentConfigDoc,
    AgentFacts,
    BuiltinTool,
    HelperDoc,
    McpServer,
    McpTool,
    ModelChoice,
    PlaceDoc,
    SkillDoc,
)
from daimon.core.stores.agent_tool_rules import AgentToolRule
from daimon.testing.ma_models import ma_agent

_ALLOW = {"type": "always_allow"}
_IDENTITY = frozenset(session_snapshot._IDENTITY_FIELDS)  # pyright: ignore[reportPrivateUsage]
_MUTABLE = frozenset(session_snapshot._MUTABLE_FIELDS)  # pyright: ignore[reportPrivateUsage]


def _config(**update: object) -> AgentConfigDoc:
    base = AgentConfigDoc(
        name="helper",
        purpose="Answers questions",
        avatar="",
        model=ModelChoice(
            id="claude-sonnet-4-6", backend="anthropic", speed="standard", effort="high"
        ),
        system="Be brief.",
        skills=[SkillDoc(type="custom", skill_id="sk_a", title="Notes", version="1")],
        tools=[BuiltinTool(name="bash", enabled=False), BuiltinTool(name="read", enabled=True)],
        mcp_servers=[
            McpServer(
                key="docs",
                name="docs",
                url="https://docs.example.com/mcp",
                auth="none",
                credential=None,
                tools=[McpTool(name="search", description="", effect="read", rule="allow")],
            )
        ],
        places=[],
    )
    return base.model_copy(update=update)


def test_effect_map_names_only_real_snapshot_fields() -> None:
    for key, (axis, fields) in agent_changes.EFFECT_FIELDS.items():
        if axis == "identity":
            assert fields and set(fields) <= _IDENTITY, f"{key} feeds identity fields"
        elif axis == "mutable":
            assert fields and set(fields) <= _MUTABLE, f"{key} feeds mutable fields"
        else:
            assert fields == (), f"{key} touches no session"


def test_every_snapshot_field_a_draft_can_change_is_mapped() -> None:
    mapped = {field for _axis, fields in agent_changes.EFFECT_FIELDS.values() for field in fields}
    editable = {"model_id", "model_speed", "system_sha256", "skills_sha256"}
    editable |= {"tools_sha256", "mcp_servers_sha256"}
    assert mapped == editable, "the effect map covers exactly what a draft can change"


def test_change_effect_by_axis_and_backend() -> None:
    assert agent_changes.change_effect("system", backend="anthropic") == "session"
    assert agent_changes.change_effect("skill:sk_a", backend="anthropic") == "session"
    assert agent_changes.change_effect("mcp:docs/search", backend="anthropic") == "live"
    assert agent_changes.change_effect("tool:bash", backend="anthropic") == "live"
    assert agent_changes.change_effect("place:c1", backend="openai") == "live", (
        "where an agent answers is live on every backend"
    )
    assert agent_changes.change_effect("tool:bash", backend="openai") == "threads", (
        "a neutral backend takes changes on new threads only"
    )


def test_diff_config_lists_each_change_with_its_key() -> None:
    a = _config()
    b = _config(
        system="Be thorough.",
        skills=[],
        tools=[BuiltinTool(name="bash", enabled=True), BuiltinTool(name="read", enabled=True)],
        mcp_servers=[
            a.mcp_servers[0].model_copy(
                update={
                    "tools": [McpTool(name="search", description="", effect="read", rule="ask")]
                }
            )
        ],
    )
    changes = {change.key: change for change in agent_changes.diff_config(a, b)}
    assert set(changes) == {"system", "skill:sk_a", "tool:bash", "mcp:docs/search"}, (
        "prompt, skill, built-in and tool rule changes are listed"
    )
    assert changes["system"].text is True, "the prompt is diffed by line"
    assert changes["skill:sk_a"].kind == "remove", "a dropped skill is a removal"
    assert (changes["mcp:docs/search"].before, changes["mcp:docs/search"].after) == ("Allow", "Ask")
    assert agent_changes.diff_config(a, a) == [], "no change, no entries"


def test_a_new_skill_version_is_an_edit_of_the_pinned_version() -> None:
    newer = SkillDoc(type="custom", skill_id="sk_a", title="Notes", version="2")
    (change,) = agent_changes.diff_config(_config(), _config(skills=[newer]))
    assert (change.key, change.kind, change.before, change.after) == (
        "skill:sk_a",
        "edit",
        "1",
        "2",
    )
    assert not change.text, "a skill's files are not diffed as one text"


def test_model_backend_switch_reaches_new_threads_as_a_provider_setting() -> None:
    """A provider switch applies from the web, as the provider setting, not the MA model."""
    a = _config()
    b = _config(model=ModelChoice(id="gpt-5.5", backend="openai", speed="standard", effort="high"))
    (change,) = agent_changes.diff_config(a, b)
    assert change.effect == "threads", "a backend switch reaches new threads"
    assert agent_changes.unsupported([change], a=a, b=b) is None, "the web may switch providers"
    assert agent_changes.provider_moved(a, b), "the switch writes the provider setting"
    patch = agent_changes.patch_for(ma_agent(id="ag_x", name="helper"), [change], b)
    assert patch.model is None, "another provider's model is not the Managed Agents model"


def _on(config: AgentConfigDoc, **model: object) -> AgentConfigDoc:
    return config.model_copy(update={"model": config.model.model_copy(update=model)})


def test_provider_effort_is_stored_when_chosen_and_unset_reads_as_the_model_default() -> None:
    """One rule for web and chat: unset is the model default; any chosen level is stored."""
    unset = _config(
        model=ModelChoice(id="gpt-5.5", backend="openai", speed="standard", effort=None)
    )
    medium = _on(unset, effort="medium")
    assert (
        agent_changes.unsupported(agent_changes.diff_config(unset, medium), a=unset, b=medium)
        is None
    ), "an offered level saves"
    assert agent_changes.provider_moved(unset, medium), "picking medium writes the provider setting"
    assert agent_changes._stated_effort(unset, medium) == "medium", "an explicit medium is stored"
    assert agent_changes._stated_effort(medium, medium) is None, (
        "an unchanged level is not restated"
    )


def test_claude_top_effort_maps_to_the_provider_highest_on_a_switch() -> None:
    claude = _config(
        model=ModelChoice(
            id="claude-sonnet-4-6", backend="anthropic", speed="standard", effort="max"
        )
    )
    openai = _on(claude, id="gpt-5.5", backend="openai")
    changes = agent_changes.diff_config(claude, openai)
    assert agent_changes.unsupported(changes, a=claude, b=openai) is None, (
        "the switch is not refused"
    )
    assert agent_changes._stated_effort(claude, openai) == "high", (
        "max runs as the provider's highest"
    )


def test_effort_on_a_model_without_one_is_refused_unless_carried() -> None:
    claude = _config(
        model=ModelChoice(
            id="claude-sonnet-4-6", backend="anthropic", speed="standard", effort="high"
        )
    )
    gemini = _on(claude, id="gemini-4-argon", backend="gemini")
    assert (
        agent_changes.unsupported(agent_changes.diff_config(claude, gemini), a=claude, b=gemini)
        is None
    ), "a level carried over unchanged is dropped, not refused"
    assert agent_changes._stated_effort(claude, gemini) is None, "nothing stated on Gemini"
    picked = _on(gemini, effort="low")
    assert (
        agent_changes.unsupported(agent_changes.diff_config(gemini, picked), a=gemini, b=picked)
        == "That effort isn't available for this model. Nothing was saved."
    ), "a level chosen on a model without one is refused at save"


def test_patch_for_writes_only_what_changed() -> None:
    agent = ma_agent(
        id="ag_x",
        name="helper",
        tools=[
            {
                "type": "mcp_toolset",
                "mcp_server_name": "docs",
                "default_config": {"enabled": True, "permission_policy": _ALLOW},
                "configs": [],
            }
        ],
        mcp_servers=[{"name": "docs", "type": "url", "url": "https://docs.example.com/mcp"}],
        updated_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    a = _config()
    b = _config(purpose="Writes reports", mcp_servers=[])
    patch = agent_changes.patch_for(agent, agent_changes.diff_config(a, b), b)
    assert patch.touched() == {"description", "tools", "mcp_servers"}, (
        "a removed server rewrites servers and toolsets; the prompt is untouched"
    )
    assert patch.mcp_servers == [], "the server is dropped"
    assert all(tool.get("type") != "mcp_toolset" for tool in patch.tools or []), (
        "its toolset goes with it"
    )


def test_revert_target_keeps_name_face_and_places() -> None:
    current = _config(name="renamed", avatar="https://x/a.png")
    old = _config(system="Old prompt.")
    target = agent_changes.revert_target(current, old)
    assert (target.name, target.avatar, target.system) == (
        "renamed",
        "https://x/a.png",
        "Old prompt.",
    ), "a revert restores the setup but not the identity people see"


_GENERAL = PlaceDoc(id="c1", kind="channel", platform="discord", name="general")
_BOARD = PlaceDoc(id="c2", kind="channel", platform="discord", name="board", confidential=True)
_WEB = PlaceDoc(id="web", kind="web", platform="web", name="Web chat")


def test_place_changes_are_applicable_and_reach_no_session() -> None:
    a = _config(places=[_GENERAL])
    b = _config(places=[_GENERAL.model_copy(update={"default": True}), _WEB])
    changes = agent_changes.diff_config(a, b)
    assert {c.key for c in changes} == {"place:c1", "place:web"}, "a default and a new place"
    assert all(c.effect == "live" for c in changes), "a place touches no running session"
    assert agent_changes.unsupported(changes, a=a, b=b) is None, "places apply from the web"


def test_place_edits_take_channels_and_web_and_skip_direct_messages() -> None:
    dm = PlaceDoc(id="d1", kind="dm", platform="discord", name="DMs")
    edits = agent_changes.place_edits([_GENERAL.model_copy(update={"default": True}), _WEB, dm])
    assert edits == [PlaceEdit("c1", True), PlaceEdit("web", False)]
    assert agent_changes.place_edits(None) == []


def test_a_non_admins_copy_keeps_the_confidential_places_it_could_not_see() -> None:
    base = _config(places=[_GENERAL, _BOARD])
    seen = _config(places=[_GENERAL])
    kept = agent_changes.with_hidden_places(base, seen, is_admin=False)
    assert [p.id for p in kept.places or []] == ["c1", "c2"], "they are not read as removed"
    assert agent_changes.diff_config(base, kept) == [], "so saving it changes nothing"
    admin = agent_changes.with_hidden_places(base, seen, is_admin=True)
    assert [p.id for p in admin.places or []] == ["c1"], "an admin who removes one removes it"


def test_a_copy_without_places_leaves_them_alone() -> None:
    base = _config(places=[_GENERAL])
    redacted = _config(places=None)
    kept = agent_changes.with_hidden_places(base, redacted, is_admin=False)
    assert agent_changes.diff_config(base, kept) == [], "no places read as no change"


def test_a_tool_the_old_config_did_not_list_ran_under_allow() -> None:
    a = _config()
    server = a.mcp_servers[0]
    b = _config(
        mcp_servers=[
            server.model_copy(
                update={
                    "tools": [
                        *server.tools,
                        McpTool(name="drop", description="", effect="write", rule="deny"),
                    ]
                }
            )
        ]
    )
    (change,) = agent_changes.diff_config(a, b)
    assert (change.key, change.before, change.after) == ("mcp:docs/drop", "Allow", "Deny")
    same = _config(
        mcp_servers=[
            server.model_copy(
                update={
                    "tools": [
                        *server.tools,
                        McpTool(name="drop", description="", effect="write", rule="allow"),
                    ]
                }
            )
        ]
    )
    assert agent_changes.diff_config(a, same) == [], "listing a tool under allow is no change"


def test_a_draft_a_viewer_without_details_reads_carries_no_prompt_or_values() -> None:
    base = _config(places=[_GENERAL])
    edited = _config(system="Be loud.", places=[_GENERAL, _WEB])
    draft = agent_changes.Draft(
        base_version=1, config=edited, changes=agent_changes.diff_config(base, edited), stale=False
    )
    shown = agent_changes.redact_draft(draft, details=False, is_admin=False)
    assert shown.config.system is None, "the prompt is not returned"
    assert shown.config.places is None, "nor the places"
    assert shown.changes, "the fact of a change stays"
    assert all(c.before is None and c.after is None for c in shown.changes), (
        "but the old and new values do not"
    )


def test_a_non_admins_draft_shows_no_change_to_a_confidential_place() -> None:
    base = _config(places=[_GENERAL, _BOARD])
    edited = _config(places=[_GENERAL, _BOARD.model_copy(update={"default": True}), _WEB])
    draft = agent_changes.Draft(
        base_version=1, config=edited, changes=agent_changes.diff_config(base, edited), stale=False
    )
    shown = agent_changes.redact_draft(draft, details=True, is_admin=False)
    assert {c.key for c in shown.changes} == {"place:web"}, "the board change is not listed"
    assert [p.id for p in shown.config.places or []] == ["c1", "web"], "nor the place itself"
    full = agent_changes.redact_draft(draft, details=True, is_admin=True)
    assert {c.key for c in full.changes} == {"place:c2", "place:web"}, "an admin sees both"


def test_a_copy_read_without_details_is_not_an_edit_of_what_it_hid() -> None:
    base = _config(places=[_GENERAL, _BOARD])
    seen = agent_changes.redact_draft(
        agent_changes.Draft(base_version=1, config=base, changes=[], stale=False),
        details=False,
        is_admin=False,
    ).config
    kept = agent_changes.keep_unseen(base, seen, details=False, is_admin=False)
    assert agent_changes.diff_config(base, kept) == [], (
        "saving the redacted copy back erases neither prompt, servers nor places"
    )


def test_db_state_moves_with_places_tool_rules_and_provider_only() -> None:
    base = _config(places=[_GENERAL])
    server = base.mcp_servers[0]
    denied = server.model_copy(
        update={"tools": [server.tools[0].model_copy(update={"rule": "deny"})]}
    )
    assert agent_changes.db_state(base) == agent_changes.db_state(_config(places=[_GENERAL])), (
        "the same state has the same fingerprint"
    )
    assert agent_changes.db_state(base) != agent_changes.db_state(
        _config(places=[_GENERAL, _WEB])
    ), "a new place moves it"
    assert agent_changes.db_state(base) != agent_changes.db_state(
        _config(places=[_GENERAL], mcp_servers=[denied])
    ), "a changed tool rule moves it"
    assert agent_changes.db_state(base) == agent_changes.db_state(
        _config(places=[_GENERAL], system="x")
    ), "the prompt, which lives in the provider's agent, does not"
    elsewhere = base.model.model_copy(update={"backend": "openai", "id": "gpt-6-astra"})
    assert agent_changes.db_state(base) != agent_changes.db_state(
        _config(places=[_GENERAL], model=elsewhere)
    ), "a provider switch moves it"


def _stored_rule(tool: str, rule: str) -> AgentToolRule:
    return AgentToolRule(
        tenant_id=uuid.uuid4(),
        agent_id="ag_x",
        agent_name="helper",
        tool_key=f"docs/{tool}",
        channel_id=None,
        server_url="https://docs.example.com/mcp",
        rule=rule,  # pyright: ignore[reportArgumentType]
        scope="chat",
        revision=1,
        set_by="a",
        set_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_stored_db_state_matches_the_base_from_stored_places_and_rules_alone() -> None:
    """Whether a server was listed when the draft began or is down now, the state agrees."""
    server = _config().mcp_servers[0]
    denied = server.model_copy(
        update={"tools": [server.tools[0].model_copy(update={"rule": "deny"})]}
    )
    agent = ma_agent(id="ag_x", name="helper")
    stored = AgentFacts(agent=agent, places=[_GENERAL], tool_rules=[_stored_rule("search", "deny")])
    assert agent_changes.stored_db_state(
        _config(places=[_GENERAL], mcp_servers=[denied]), stored
    ) == agent_changes.db_state(_config(places=[_GENERAL], mcp_servers=[denied])), (
        "a base that saw the rule matches the stored rule"
    )
    assert agent_changes.stored_db_state(_config(places=[_GENERAL]), stored) != (
        agent_changes.db_state(_config(places=[_GENERAL]))
    ), "a rule set since the base was read moves it"
    extra = AgentFacts(
        agent=agent, places=[_GENERAL], tool_rules=[_stored_rule("not_listed", "deny")]
    )
    assert agent_changes.stored_db_state(_config(places=[_GENERAL]), extra) == (
        agent_changes.db_state(_config(places=[_GENERAL]))
    ), "a recorded rule for a tool the config never listed does not make it stale"
    moved = AgentFacts(agent=agent, places=[_GENERAL, _WEB])
    assert agent_changes.stored_db_state(_config(places=[_GENERAL]), moved) != (
        agent_changes.db_state(_config(places=[_GENERAL]))
    ), "a stored place added since moves it"


def test_confidential_place_changes_are_hidden_across_versions() -> None:
    before = _config(places=[_GENERAL, _BOARD])
    after = _config(places=[_GENERAL])
    changes = agent_changes.diff_config(before, after)
    hidden = agent_changes.confidential_place_ids([before, after])
    assert hidden == {"c2"}, "an earlier version's config still names the removed place"
    assert agent_changes.without_hidden_place_changes(changes, hidden) == [], (
        "removing the confidential place is not listed"
    )


_SCOUT = HelperDoc(id="ag_scout", name="Scout")
_WRITER = HelperDoc(id="ag_writer", name="Writer")


def test_helper_changes_are_listed_by_id_and_apply_live_on_every_backend() -> None:
    a = _config(helpers=[_SCOUT])
    b = _config(helpers=[_WRITER])
    changes = {change.key: change for change in agent_changes.diff_config(a, b)}
    assert set(changes) == {"helper:ag_scout", "helper:ag_writer"}, "one entry per helper id"
    assert (changes["helper:ag_writer"].kind, changes["helper:ag_writer"].label) == (
        "add",
        "Writer helper",
    ), "an added helper is labelled with its name"
    assert changes["helper:ag_scout"].kind == "remove", "a dropped helper is a removal"
    assert {c.node for c in changes.values()} == {"helpers"}, "they sit on the helpers node"
    assert {c.effect for c in changes.values()} == {"live"}, "the list is read each turn"
    assert agent_changes.change_effect("helper:ag_x", backend="openai") == "live", (
        "a neutral backend takes a new helper at once too"
    )
    assert agent_changes.unsupported(list(changes.values()), a=a, b=b) is None, (
        "the web may change helpers"
    )


def test_a_config_without_a_helper_list_is_the_same_as_an_empty_one() -> None:
    assert agent_changes.diff_config(_config(), _config(helpers=[])) == [], (
        "None and [] both mean no helpers"
    )
    renamed = _config(helpers=[HelperDoc(id="ag_scout", name="Scout II")])
    assert agent_changes.diff_config(_config(helpers=[_SCOUT]), renamed) == [], (
        "a helper's new name is not a change to the list"
    )


def test_a_copy_without_helpers_keeps_the_stored_ones() -> None:
    base = _config(helpers=[_SCOUT])
    kept = agent_changes.keep_helpers(base, _config())
    assert kept.helpers == [_SCOUT], "an unread list is not an edit that empties it"
    emptied = agent_changes.keep_helpers(base, _config(helpers=[]))
    assert emptied.helpers == [], "an empty list is"
    unseen = agent_changes.keep_unseen(base, _config(helpers=None), details=False, is_admin=False)
    assert unseen.helpers == [_SCOUT], "a viewer without details cannot have changed them"


def test_db_state_and_stored_db_state_move_with_the_helper_ids() -> None:
    agent = ma_agent(id="ag_x", name="helper")
    base = _config(helpers=[_SCOUT])
    assert agent_changes.db_state(base) != agent_changes.db_state(_config()), (
        "a helper moves the fingerprint"
    )
    assert agent_changes.db_state(base) == agent_changes.db_state(
        _config(helpers=[HelperDoc(id="ag_scout", name="Renamed")])
    ), "a helper's name does not"
    assert agent_changes.db_state(_config(helpers=[_SCOUT, _WRITER])) == agent_changes.db_state(
        _config(helpers=[_WRITER, _SCOUT])
    ), "their order does not"
    same = AgentFacts(agent=agent, helpers=[_SCOUT])
    assert agent_changes.stored_db_state(base, same) == agent_changes.db_state(base), (
        "a draft whose helpers still stand is current"
    )
    moved = AgentFacts(agent=agent, helpers=[_SCOUT, _WRITER])
    assert agent_changes.stored_db_state(base, moved) != agent_changes.db_state(base), (
        "a helper added since the base was read makes the draft stale"
    )
    gone = AgentFacts(agent=agent)
    assert agent_changes.stored_db_state(base, gone) != agent_changes.db_state(base), (
        "a helper removed since does too"
    )


def test_revert_target_carries_the_old_versions_helpers() -> None:
    current = _config(helpers=[_SCOUT])
    old = _config(helpers=[_WRITER])
    assert agent_changes.revert_target(current, old).helpers == [_WRITER], (
        "a revert restores the helpers the version had"
    )
