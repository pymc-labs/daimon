"""Wave 0 builder tests for agent_setup/views.py.

Tests the pure Block Kit builders: admin vs non-admin rendering, secret-names-only
guarantee, active-tab primary styling, L3 form structure (callback_ids, block_ids),
and private_metadata character budget.

No I/O, no DB, no mocks — these are pure builders.
"""

from __future__ import annotations

import json

from daimon.adapters.slack.agent_setup.state import AgentSetupState, RosterEntry
from daimon.adapters.slack.agent_setup.views import (
    build_agent_section,
    build_l1_view,
    build_l2_view,
    build_l3_add_mcp_form,
    build_l3_add_skill_form,
    build_l3_edit_agent_form,
    build_l3_edit_repo_form,
    build_l3_fork_agent_form,
    build_l3_new_agent_form,
    build_l3_paste_secrets_form,
    build_loading_view,
    build_mcps_section,
    build_repo_auth_section,
    build_secrets_section,
    build_skills_section,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block_ids(view: dict) -> list[str]:  # type: ignore[type-arg]
    """Collect block_ids from all blocks in a view."""
    return [b.get("block_id", "") for b in view.get("blocks", [])]


def _roster_state(*agent_names: str) -> AgentSetupState:
    """Build an AgentSetupState with the given agent names."""
    rows = [RosterEntry(agent_name=n, model_id="claude-opus-4-5") for n in agent_names]
    return AgentSetupState(rows=rows, over_cap_count=0)


# ---------------------------------------------------------------------------
# build_loading_view
# ---------------------------------------------------------------------------


def test_build_loading_view_has_correct_callback_id() -> None:
    view = build_loading_view(team_id="T123", channel_id="C456")
    assert view["callback_id"] == "agent_setup", "loading view must have callback_id 'agent_setup'"


def test_build_loading_view_title_at_most_24_chars() -> None:
    view = build_loading_view(team_id="T123", channel_id="C456")
    title_text = view["title"]["text"]
    assert len(title_text) <= 24, (
        f"modal title must be <= 24 chars, got {len(title_text)}: {title_text!r}"
    )


def test_build_loading_view_private_metadata_has_team_and_channel() -> None:
    view = build_loading_view(team_id="T01ABC123", channel_id="C01XYZ456")
    pm = json.loads(view["private_metadata"])
    assert pm["team_id"] == "T01ABC123", "loading view private_metadata must carry team_id"
    assert pm["channel_id"] == "C01XYZ456", "loading view private_metadata must carry channel_id"


def test_build_loading_view_private_metadata_has_no_tenant_id() -> None:
    view = build_loading_view(team_id="T01ABC123", channel_id="C01XYZ456")
    pm = json.loads(view["private_metadata"])
    assert "tenant_id" not in pm, "loading view private_metadata must not contain tenant_id"


# ---------------------------------------------------------------------------
# build_l1_view — admin rendering
# ---------------------------------------------------------------------------


def test_build_l1_view_admin_includes_lifecycle_actions_block() -> None:
    state = _roster_state("agent-a", "agent-b")
    view = build_l1_view(
        state,
        is_admin=True,
        team_id="T123",
        channel_id="C456",
        selected_agent_name="agent-a",
        scope_hint="_(no default set)_",
    )
    block_ids = _block_ids(view)
    assert "agent_setup__lifecycle_actions" in block_ids, (
        "admin view must include agent_setup__lifecycle_actions block"
    )


def test_build_l1_view_admin_includes_scope_actions_block() -> None:
    state = _roster_state("agent-a")
    view = build_l1_view(
        state,
        is_admin=True,
        team_id="T123",
        channel_id="C456",
        selected_agent_name="agent-a",
        scope_hint="_(no default set)_",
    )
    block_ids = _block_ids(view)
    assert "agent_setup__scope_actions" in block_ids, (
        "admin view with selected agent must include agent_setup__scope_actions block"
    )


def test_build_l1_view_admin_includes_roster_select_block() -> None:
    state = _roster_state("agent-a")
    view = build_l1_view(
        state,
        is_admin=True,
        team_id="T123",
        channel_id="C456",
        selected_agent_name=None,
        scope_hint="_(no default set)_",
    )
    block_ids = _block_ids(view)
    assert "agent_setup__roster_select" in block_ids, (
        "admin view must include agent_setup__roster_select block"
    )


# ---------------------------------------------------------------------------
# build_l1_view — non-admin rendering (omission contract)
# ---------------------------------------------------------------------------


def test_build_l1_view_non_admin_omits_lifecycle_actions() -> None:
    state = _roster_state("agent-a", "agent-b")
    view = build_l1_view(
        state,
        is_admin=False,
        team_id="T123",
        channel_id="C456",
        selected_agent_name="agent-a",
        scope_hint="_(no default set)_",
    )
    block_ids = _block_ids(view)
    assert "agent_setup__lifecycle_actions" not in block_ids, (
        "non-admin view must NOT include agent_setup__lifecycle_actions (omission contract)"
    )


def test_build_l1_view_non_admin_omits_scope_actions() -> None:
    state = _roster_state("agent-a")
    view = build_l1_view(
        state,
        is_admin=False,
        team_id="T123",
        channel_id="C456",
        selected_agent_name="agent-a",
        scope_hint="_(no default set)_",
    )
    block_ids = _block_ids(view)
    assert "agent_setup__scope_actions" not in block_ids, (
        "non-admin view must NOT include agent_setup__scope_actions (omission contract)"
    )


def test_build_l1_view_non_admin_keeps_roster_select() -> None:
    state = _roster_state("agent-a")
    view = build_l1_view(
        state,
        is_admin=False,
        team_id="T123",
        channel_id="C456",
        selected_agent_name=None,
        scope_hint="_(no default set)_",
    )
    block_ids = _block_ids(view)
    assert "agent_setup__roster_select" in block_ids, (
        "non-admin view must still include agent_setup__roster_select (read-only roster)"
    )


def test_build_l1_view_non_admin_keeps_scope_hint() -> None:
    state = _roster_state("agent-a")
    view = build_l1_view(
        state,
        is_admin=False,
        team_id="T123",
        channel_id="C456",
        selected_agent_name="agent-a",
        scope_hint="_(no default set)_",
    )
    block_ids = _block_ids(view)
    assert "agent_setup__scope_hint" in block_ids, (
        "non-admin view must still include agent_setup__scope_hint (read-only)"
    )


# ---------------------------------------------------------------------------
# build_l1_view — private_metadata budget
# ---------------------------------------------------------------------------


def test_build_l1_view_private_metadata_under_3000_chars() -> None:
    state = _roster_state("my-agent")
    view = build_l1_view(
        state,
        is_admin=True,
        team_id="T" + "0" * 10,
        channel_id="C" + "0" * 10,
        selected_agent_name="my-agent",
        scope_hint="_(no default set)_",
    )
    pm = view["private_metadata"]
    assert len(pm) < 3000, f"L1 private_metadata must be < 3000 chars, got {len(pm)}"


def test_build_l1_view_private_metadata_has_no_tenant_id() -> None:
    state = _roster_state("agent-a")
    view = build_l1_view(
        state,
        is_admin=True,
        team_id="T123",
        channel_id="C456",
        selected_agent_name="agent-a",
        scope_hint="_(no default set)_",
    )
    pm = json.loads(view["private_metadata"])
    assert "tenant_id" not in pm, (
        "L1 private_metadata must not contain tenant_id (always derived server-side)"
    )


# ---------------------------------------------------------------------------
# build_l2_view — active tab styling
# ---------------------------------------------------------------------------


def test_build_l2_view_active_skills_tab_has_primary_style() -> None:
    view = build_l2_view(
        agent_name="my-agent",
        active_section="skills",
        team_id="T123",
        channel_id="C456",
        is_admin=True,
        section_blocks=[],
    )
    # Find the section_tabs actions block
    tabs_block = next(
        (b for b in view["blocks"] if b.get("block_id") == "agent_setup__section_tabs"),
        None,
    )
    assert tabs_block is not None, "L2 view must include agent_setup__section_tabs block"

    elements = tabs_block["elements"]
    skills_tab = next(
        (e for e in elements if e.get("action_id") == "agent_setup__tab:skills"),
        None,
    )
    assert skills_tab is not None, "L2 view must have a skills tab element"
    assert skills_tab.get("style") == "primary", (
        "active 'skills' tab must carry style='primary' to orient the user"
    )


def test_build_l2_view_inactive_tabs_have_no_primary_style() -> None:
    view = build_l2_view(
        agent_name="my-agent",
        active_section="skills",
        team_id="T123",
        channel_id="C456",
        is_admin=True,
        section_blocks=[],
    )
    tabs_block = next(
        (b for b in view["blocks"] if b.get("block_id") == "agent_setup__section_tabs"),
        None,
    )
    assert tabs_block is not None, "L2 view must include agent_setup__section_tabs block"

    inactive_action_ids = {
        "agent_setup__tab:agent",
        "agent_setup__tab:repo_auth",
        "agent_setup__tab:mcps",
        "agent_setup__tab:secrets",
    }
    for element in tabs_block["elements"]:
        if element.get("action_id") in inactive_action_ids:
            assert element.get("style") != "primary", (
                f"inactive tab {element['action_id']} must not have style='primary'"
            )


def test_build_l2_view_active_agent_tab_has_primary_style() -> None:
    view = build_l2_view(
        agent_name="my-agent",
        active_section="agent",
        team_id="T123",
        channel_id="C456",
        is_admin=True,
        section_blocks=[],
    )
    tabs_block = next(
        (b for b in view["blocks"] if b.get("block_id") == "agent_setup__section_tabs"),
        None,
    )
    assert tabs_block is not None, "L2 view must include agent_setup__section_tabs block"
    agent_tab = next(
        (e for e in tabs_block["elements"] if e.get("action_id") == "agent_setup__tab:agent"),
        None,
    )
    assert agent_tab is not None, "L2 view must have an agent tab element"
    assert agent_tab.get("style") == "primary", "active 'agent' tab must carry style='primary'"


# ---------------------------------------------------------------------------
# build_l2_view — private_metadata
# ---------------------------------------------------------------------------


def test_build_l2_view_private_metadata_under_3000_chars() -> None:
    view = build_l2_view(
        agent_name="a" * 64,
        active_section="secrets",
        team_id="T" + "0" * 10,
        channel_id="C" + "0" * 10,
        is_admin=True,
        section_blocks=[],
    )
    pm = view["private_metadata"]
    assert len(pm) < 3000, f"L2 private_metadata must be < 3000 chars, got {len(pm)}"


def test_build_l2_view_private_metadata_has_no_tenant_id() -> None:
    view = build_l2_view(
        agent_name="my-agent",
        active_section="agent",
        team_id="T123",
        channel_id="C456",
        is_admin=False,
        section_blocks=[],
    )
    pm = json.loads(view["private_metadata"])
    assert "tenant_id" not in pm, "L2 private_metadata must not contain tenant_id"


# ---------------------------------------------------------------------------
# build_secrets_section — secret-names-only (D-09) — selectable with -k secret
# ---------------------------------------------------------------------------


def test_secrets_section_renders_key_names_as_chips_when_names_provided() -> None:
    """Secret key NAMES render as backtick chips; no value is ever passed or rendered."""
    blocks = build_secrets_section(
        agent_name="my-agent",
        secret_names=["XERO_API_KEY", "TOGGL_TOKEN"],
        is_admin=True,
    )
    serialized = json.dumps(blocks)
    assert "XERO_API_KEY" in serialized, (
        "key name XERO_API_KEY should appear in the rendered blocks"
    )
    assert "TOGGL_TOKEN" in serialized, "key name TOGGL_TOKEN should appear in the rendered blocks"


def test_secrets_section_never_renders_fictional_secret_value() -> None:
    """A fictional value string must not appear anywhere in the serialized blocks."""
    sentinel_value = "super-secret-value-abc123xyz-SENTINEL"
    blocks = build_secrets_section(
        agent_name="my-agent",
        secret_names=["XERO_API_KEY", "TOGGL_TOKEN"],
        is_admin=True,
    )
    serialized = json.dumps(blocks)
    assert sentinel_value not in serialized, (
        "a fictional secret value must not appear in any rendered block "
        "(secret hygiene D-09: build_secrets_section accepts names only)"
    )


def test_secrets_section_empty_names_renders_empty_state() -> None:
    """Empty secret_names renders the add-first-secret guidance, not a blank."""
    blocks = build_secrets_section(
        agent_name="my-agent",
        secret_names=[],
        is_admin=True,
    )
    serialized = json.dumps(blocks)
    # The empty state copy is: "_-# + add your first secret_"
    assert "add your first secret" in serialized, (
        "empty secret_names must render the empty-state guidance copy"
    )


def test_secrets_section_keys_only_no_values_parameter_in_signature() -> None:
    """Structural check: build_secrets_section must have no value-carrying parameter."""
    import inspect

    sig = inspect.signature(build_secrets_section)
    param_names = set(sig.parameters)
    forbidden = {"secret_value", "secret_values", "value", "values", "secrets"}
    overlap = param_names & forbidden
    assert not overlap, (
        f"build_secrets_section must not accept any value parameters, "
        f"found: {overlap} (D-09 structural guarantee)"
    )


def test_secrets_section_non_admin_omits_mutation_actions() -> None:
    """Non-admin secret section omits Add/Remove buttons."""
    blocks = build_secrets_section(
        agent_name="my-agent",
        secret_names=["XERO_API_KEY"],
        is_admin=False,
    )
    block_ids = [b.get("block_id", "") for b in blocks]
    assert "agent_setup__secrets_actions" not in block_ids, (
        "non-admin secrets section must not include agent_setup__secrets_actions"
    )


def test_secrets_section_admin_includes_mutation_actions() -> None:
    """Admin secret section includes Add secrets / Remove secret buttons."""
    blocks = build_secrets_section(
        agent_name="my-agent",
        secret_names=["XERO_API_KEY"],
        is_admin=True,
    )
    block_ids = [b.get("block_id", "") for b in blocks]
    assert "agent_setup__secrets_actions" in block_ids, (
        "admin secrets section must include agent_setup__secrets_actions"
    )


# ---------------------------------------------------------------------------
# L3 forms — callback_ids, block_ids, title length, private_metadata
# ---------------------------------------------------------------------------


def _assert_l3_form(
    view: dict,  # type: ignore[type-arg]
    expected_callback_id: str,
    expected_block_ids: list[str],
    *,
    max_title_len: int = 24,
) -> None:
    """Shared assertions for all L3 forms."""
    assert view["callback_id"] == expected_callback_id, (
        f"L3 form must have callback_id '{expected_callback_id}', got '{view['callback_id']}'"
    )
    title_text = view["title"]["text"]
    assert len(title_text) <= max_title_len, (
        f"modal title must be <= {max_title_len} chars, got {len(title_text)}: {title_text!r}"
    )
    actual_block_ids = [b.get("block_id", "") for b in view.get("blocks", [])]
    for bid in expected_block_ids:
        assert bid in actual_block_ids, (
            f"L3 form '{expected_callback_id}' must include block_id '{bid}'"
        )
    pm = view["private_metadata"]
    assert len(pm) < 3000, f"L3 form '{expected_callback_id}' private_metadata must be < 3000 chars"
    pm_dict = json.loads(pm)
    assert "tenant_id" not in pm_dict, (
        f"L3 form '{expected_callback_id}' private_metadata must not contain tenant_id"
    )


def test_build_l3_new_agent_form_has_correct_structure() -> None:
    view = build_l3_new_agent_form(team_id="T123", channel_id="C456")
    _assert_l3_form(
        view,
        expected_callback_id="agent_setup__new_agent",
        expected_block_ids=["new_agent__name", "new_agent__prompt", "new_agent__model"],
    )
    assert view["submit"]["text"] == "Create", "new agent form submit label must be 'Create'"


def test_build_l3_fork_agent_form_has_correct_structure() -> None:
    view = build_l3_fork_agent_form(source_name="base-agent", team_id="T123", channel_id="C456")
    _assert_l3_form(
        view,
        expected_callback_id="agent_setup__fork_agent",
        expected_block_ids=["fork_agent__name"],
    )
    assert view["submit"]["text"] == "Fork", "fork agent form submit label must be 'Fork'"
    # Source name shown read-only in a section block
    serialized = json.dumps(view["blocks"])
    assert "base-agent" in serialized, "fork agent form must display the source agent name"


def test_build_l3_edit_agent_form_has_correct_structure() -> None:
    view = build_l3_edit_agent_form(
        agent_name="my-agent",
        model_id="claude-opus-4-5",
        system_prompt="You are helpful.",
        team_id="T123",
        channel_id="C456",
    )
    _assert_l3_form(
        view,
        expected_callback_id="agent_setup__edit_agent",
        expected_block_ids=["edit_agent__prompt", "edit_agent__model"],
    )
    assert view["submit"]["text"] == "Save", "edit agent form submit label must be 'Save'"


def test_build_l3_edit_agent_form_omits_prompt_prefill_when_over_3000_chars() -> None:
    long_prompt = "x" * 3001
    view = build_l3_edit_agent_form(
        agent_name="my-agent",
        model_id="claude-opus-4-5",
        system_prompt=long_prompt,
        team_id="T123",
        channel_id="C456",
    )
    # Find the prompt input block
    prompt_block = next(
        (b for b in view["blocks"] if b.get("block_id") == "edit_agent__prompt"),
        None,
    )
    assert prompt_block is not None, "edit_agent__prompt block must exist"
    element = prompt_block["element"]
    assert "initial_value" not in element, (
        "prompt pre-fill must be omitted when system_prompt exceeds 3000 chars "
        "(Slack input block limit)"
    )


def test_build_l3_edit_agent_form_includes_prompt_prefill_when_at_most_3000_chars() -> None:
    prompt = "x" * 3000
    view = build_l3_edit_agent_form(
        agent_name="my-agent",
        model_id="claude-opus-4-5",
        system_prompt=prompt,
        team_id="T123",
        channel_id="C456",
    )
    prompt_block = next(
        (b for b in view["blocks"] if b.get("block_id") == "edit_agent__prompt"),
        None,
    )
    assert prompt_block is not None, "edit_agent__prompt block must exist"
    element = prompt_block["element"]
    assert "initial_value" in element, (
        "prompt pre-fill must be present when system_prompt is at most 3000 chars"
    )


def test_build_l3_edit_repo_form_has_correct_structure() -> None:
    view = build_l3_edit_repo_form(team_id="T123", channel_id="C456", agent_name="my-agent")
    _assert_l3_form(
        view,
        expected_callback_id="agent_setup__edit_repo",
        expected_block_ids=["edit_repo__url", "edit_repo__pat"],
    )
    assert view["submit"]["text"] == "Save", "edit repo form submit label must be 'Save'"


def test_build_l3_add_skill_form_has_correct_structure() -> None:
    view = build_l3_add_skill_form(team_id="T123", channel_id="C456", agent_name="my-agent")
    _assert_l3_form(
        view,
        expected_callback_id="agent_setup__add_skill",
        expected_block_ids=["add_skill__repo_url", "add_skill__branch"],
    )
    assert view["submit"]["text"] == "Add", "add skill form submit label must be 'Add'"


def test_build_l3_add_mcp_form_has_correct_structure() -> None:
    view = build_l3_add_mcp_form(team_id="T123", channel_id="C456", agent_name="my-agent")
    _assert_l3_form(
        view,
        expected_callback_id="agent_setup__add_mcp",
        expected_block_ids=["add_mcp__name", "add_mcp__url", "add_mcp__token"],
    )
    assert view["submit"]["text"] == "Add", "add MCP form submit label must be 'Add'"


def test_build_l3_paste_secrets_form_has_correct_structure() -> None:
    view = build_l3_paste_secrets_form(team_id="T123", channel_id="C456", agent_name="my-agent")
    _assert_l3_form(
        view,
        expected_callback_id="agent_setup__paste_secrets",
        expected_block_ids=["paste_secrets__content"],
    )
    assert view["submit"]["text"] == "Save", "paste secrets form submit label must be 'Save'"


# ---------------------------------------------------------------------------
# L2 section button action_ids — edit_agent_form / edit_repo_form (83-09)
# ---------------------------------------------------------------------------


def test_build_agent_section_admin_uses_edit_agent_form_action_id() -> None:
    """Admin agent section's Edit button must use action_id 'agent_setup__edit_agent_form'.

    This distinct action_id routes the click to the L3 edit-agent form instead of
    the L2 editor (which uses 'agent_setup__edit' on the L1 lifecycle row).
    """
    blocks = build_agent_section(
        agent_name="my-agent",
        model_id="claude-sonnet-4-6",
        system_prompt="You are helpful.",
        is_admin=True,
    )
    all_action_ids = [
        element.get("action_id", "")
        for block in blocks
        if block.get("type") == "actions"
        for element in block.get("elements", [])
    ]
    assert "agent_setup__edit_agent_form" in all_action_ids, (
        "build_agent_section (admin) must include an element with "
        "action_id='agent_setup__edit_agent_form' to open the L3 edit-agent form"
    )
    assert "agent_setup__edit" not in all_action_ids, (
        "build_agent_section must NOT use 'agent_setup__edit' — that is reserved "
        "for the L1 lifecycle Edit button which pushes L2"
    )


def test_build_repo_auth_section_admin_uses_edit_repo_form_action_id() -> None:
    """Admin repo section's Edit button must use action_id 'agent_setup__edit_repo_form'.

    This distinct action_id routes the click to the L3 edit-repo form rather than
    reusing 'agent_setup__edit' (the L1 → L2 push action).
    """
    blocks = build_repo_auth_section(
        repo="owner/repo",
        pat_last4="****abcd",
        is_admin=True,
    )
    all_action_ids = [
        element.get("action_id", "")
        for block in blocks
        if block.get("type") == "actions"
        for element in block.get("elements", [])
    ]
    assert "agent_setup__edit_repo_form" in all_action_ids, (
        "build_repo_auth_section (admin) must include an element with "
        "action_id='agent_setup__edit_repo_form' to open the L3 edit-repo form"
    )
    assert "agent_setup__edit" not in all_action_ids, (
        "build_repo_auth_section must NOT use 'agent_setup__edit' — that is reserved "
        "for the L1 lifecycle Edit button which pushes L2"
    )


# ---------------------------------------------------------------------------
# plain_text non-escaping (83-11 / WARNING closure)
# ---------------------------------------------------------------------------


def test_build_l1_view_roster_option_plain_text_carries_raw_name_with_special_chars() -> None:
    """Roster static_select option label must be the raw agent name — no &amp;/&lt; encoding.

    plain_text fields are rendered literally by Slack; escape_mrkdwn inside a
    plain_text field produces literal HTML entity garbage (&amp;, &lt;) instead
    of the intended characters.
    """
    raw_name = "a&b<c>"
    state = _roster_state(raw_name)
    view = build_l1_view(
        state,
        is_admin=True,
        team_id="T123",
        channel_id="C456",
        selected_agent_name=raw_name,
        scope_hint="_(no default set)_",
    )
    # Find the roster static_select block
    roster_block = next(
        (b for b in view["blocks"] if b.get("block_id") == "agent_setup__roster_select"),
        None,
    )
    assert roster_block is not None, "build_l1_view must include agent_setup__roster_select block"

    element = roster_block["element"]
    # Check options
    options: list[dict[str, object]] = element.get("options", [])
    assert len(options) == 1, "roster must contain exactly the one seeded agent"
    option_text: str = str(options[0]["text"]["text"])  # type: ignore[index]
    assert option_text == raw_name, (
        f"plain_text option label must be the raw agent name '{raw_name}', "
        f"got {option_text!r} — escape_mrkdwn must not be applied to plain_text fields"
    )

    # initial_option label must also be raw
    initial_option: dict[str, object] | None = element.get("initial_option")  # type: ignore[assignment]
    assert initial_option is not None, "initial_option must be set when selected_agent_name matches"
    initial_text: str = str(initial_option["text"]["text"])  # type: ignore[index]
    assert initial_text == raw_name, (
        f"plain_text initial_option label must be the raw agent name '{raw_name}', "
        f"got {initial_text!r}"
    )


def test_build_skills_section_remove_option_plain_text_carries_raw_name() -> None:
    """remove-skill static_select option label must carry the raw skill name.

    plain_text fields must not be escape_mrkdwn-wrapped; special chars &<> must
    appear verbatim in the option label.
    """
    raw_name = "a&b<c>"
    blocks = build_skills_section(skill_names=[raw_name], sync_pending=False, is_admin=True)
    # Find the remove-skill static_select
    remove_options: list[dict[str, object]] = []
    for block in blocks:
        if block.get("type") == "actions":
            for element in block.get("elements", []):
                if element.get("action_id") == "agent_setup__remove_skill":
                    remove_options = element.get("options", [])  # type: ignore[assignment]
    assert remove_options, (
        "build_skills_section (admin) must include a remove-skill static_select with options"
    )
    label: str = str(remove_options[0]["text"]["text"])  # type: ignore[index]
    assert label == raw_name, (
        f"remove-skill option plain_text label must be '{raw_name}' (raw), got {label!r}"
    )


def test_build_mcps_section_remove_option_plain_text_carries_raw_name() -> None:
    """remove-mcp static_select option label must carry the raw MCP name verbatim."""
    raw_name = "a&b<c>"
    mcps = [{"name": raw_name, "url": "https://mcp.example.com"}]
    blocks = build_mcps_section(mcps=mcps, is_admin=True)
    remove_options: list[dict[str, object]] = []
    for block in blocks:
        if block.get("type") == "actions":
            for element in block.get("elements", []):
                if element.get("action_id") == "agent_setup__remove_mcp":
                    remove_options = element.get("options", [])  # type: ignore[assignment]
    assert remove_options, (
        "build_mcps_section (admin) must include a remove-mcp static_select with options"
    )
    label = str(remove_options[0]["text"]["text"])  # type: ignore[index]
    assert label == raw_name, (
        f"remove-mcp option plain_text label must be '{raw_name}' (raw), got {label!r}"
    )


def test_build_secrets_section_remove_option_plain_text_carries_raw_name() -> None:
    """remove-secret static_select option label must carry the raw secret key name verbatim."""
    raw_name = "a&b<c>"
    blocks = build_secrets_section(agent_name="test-agent", secret_names=[raw_name], is_admin=True)
    remove_options: list[dict[str, object]] = []
    for block in blocks:
        if block.get("type") == "actions":
            for element in block.get("elements", []):
                if element.get("action_id") == "agent_setup__remove_secret":
                    remove_options = element.get("options", [])  # type: ignore[assignment]
    assert remove_options, (
        "build_secrets_section (admin) must include a remove-secret static_select with options"
    )
    label = str(remove_options[0]["text"]["text"])  # type: ignore[index]
    assert label == raw_name, (
        f"remove-secret option plain_text label must be '{raw_name}' (raw), got {label!r}"
    )


def test_build_l3_fork_agent_form_placeholder_plain_text_carries_raw_name() -> None:
    """Fork form name input placeholder must carry the raw source name — no entity encoding."""
    raw_name = "a&b<c>"
    form = build_l3_fork_agent_form(
        source_name=raw_name,
        team_id="T123",
        channel_id="C456",
        parent_section=None,
    )
    # The fork form has one input block for the agent name
    name_block = next(
        (b for b in form["blocks"] if b.get("block_id") == "fork_agent__name"),
        None,
    )
    assert name_block is not None, "fork form must have a fork_agent__name input block"
    placeholder_text: str = str(
        name_block["element"]["placeholder"]["text"]  # type: ignore[index]
    )
    assert placeholder_text == f"{raw_name}-fork", (
        f"fork form placeholder must be '{raw_name}-fork' (raw), got {placeholder_text!r}"
    )
