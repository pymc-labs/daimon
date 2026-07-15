"""Wave 0 state tests for agent_setup/state.py.

Tests the private_metadata round-trip (L1/L2/L3), the < 3000-char budget,
the rename-forbidden reducer shape, and the remove_*_at reducers.

No I/O, no DB, no mocks — pure unit assertions.
"""

from daimon.adapters.slack.agent_setup.state import (
    apply_agent_modal,
    apply_mcp_modal,
    decode_private_metadata,
    encode_private_metadata,
    remove_mcp_at,
    remove_skill_at,
)

# ---------------------------------------------------------------------------
# encode/decode round-trip — L1
# ---------------------------------------------------------------------------


def test_encode_decode_l1_round_trips_team_and_channel() -> None:
    encoded = encode_private_metadata(team_id="T01ABC123", channel_id="C01XYZ456")
    decoded = decode_private_metadata(encoded)
    assert decoded["team_id"] == "T01ABC123", "L1 round-trip should preserve team_id"
    assert decoded["channel_id"] == "C01XYZ456", "L1 round-trip should preserve channel_id"


def test_encode_decode_l1_omits_none_fields() -> None:
    encoded = encode_private_metadata(team_id="T01ABC123", channel_id="C01XYZ456")
    decoded = decode_private_metadata(encoded)
    assert "selected_agent_name" not in decoded, (
        "L1 without selected_agent_name should not include that key"
    )
    assert "agent_name" not in decoded, "L1 without agent_name should not include that key"


def test_encode_decode_l1_includes_selected_agent_name_when_provided() -> None:
    encoded = encode_private_metadata(
        team_id="T01ABC123", channel_id="C01XYZ456", selected_agent_name="my-agent"
    )
    decoded = decode_private_metadata(encoded)
    assert decoded.get("selected_agent_name") == "my-agent", (
        "L1 with selected_agent_name should round-trip the agent name"
    )


def test_encode_decode_l1_contains_no_workspace_derived_uuid() -> None:
    encoded = encode_private_metadata(
        team_id="T01ABC123", channel_id="C01XYZ456", selected_agent_name="my-agent"
    )
    decoded = decode_private_metadata(encoded)
    assert "tenant_id" not in decoded, (
        "private_metadata must never contain tenant_id — it is always derived server-side"
    )


# ---------------------------------------------------------------------------
# encode/decode round-trip — L2
# ---------------------------------------------------------------------------


def test_encode_decode_l2_round_trips_all_identifiers() -> None:
    encoded = encode_private_metadata(
        team_id="T01ABC123",
        channel_id="C01XYZ456",
        agent_name="my-agent",
        active_section="skills",
    )
    decoded = decode_private_metadata(encoded)
    assert decoded["team_id"] == "T01ABC123", "L2 should preserve team_id"
    assert decoded["channel_id"] == "C01XYZ456", "L2 should preserve channel_id"
    assert decoded["agent_name"] == "my-agent", "L2 should preserve agent_name"
    assert decoded["active_section"] == "skills", "L2 should preserve active_section"


def test_encode_decode_l2_omits_l3_only_fields() -> None:
    encoded = encode_private_metadata(
        team_id="T01ABC123",
        channel_id="C01XYZ456",
        agent_name="my-agent",
        active_section="agent",
    )
    decoded = decode_private_metadata(encoded)
    assert "parent_section" not in decoded, "L2 without parent_section should not include that key"


# ---------------------------------------------------------------------------
# encode/decode round-trip — L3
# ---------------------------------------------------------------------------


def test_encode_decode_l3_round_trips_all_identifiers() -> None:
    encoded = encode_private_metadata(
        team_id="T01ABC123",
        channel_id="C01XYZ456",
        agent_name="my-agent",
        parent_section="mcps",
    )
    decoded = decode_private_metadata(encoded)
    assert decoded["team_id"] == "T01ABC123", "L3 should preserve team_id"
    assert decoded["channel_id"] == "C01XYZ456", "L3 should preserve channel_id"
    assert decoded["agent_name"] == "my-agent", "L3 should preserve agent_name"
    assert decoded["parent_section"] == "mcps", "L3 should preserve parent_section"


# ---------------------------------------------------------------------------
# Character budget — must stay under 3,000 chars
# ---------------------------------------------------------------------------


def test_encode_private_metadata_worst_case_stays_under_3000_chars() -> None:
    """Worst-case: max-length Slack IDs + 64-char agent name."""
    # Slack workspace IDs are typically 9-11 chars (T01ABC123XYZ); Slack channel
    # IDs are similar length. Use a realistic worst case padded to be generous.
    long_agent_name = "a" * 64
    encoded = encode_private_metadata(
        team_id="T" + "0" * 10,
        channel_id="C" + "0" * 10,
        agent_name=long_agent_name,
        active_section="repo_auth",
        parent_section="secrets",
    )
    assert len(encoded) < 3000, "private_metadata must stay under the Slack 3000-char limit"


# ---------------------------------------------------------------------------
# decode — malformed / empty input
# ---------------------------------------------------------------------------


def test_decode_private_metadata_empty_string_returns_empty_dict() -> None:
    result = decode_private_metadata("")
    assert result == {}, "decode of empty string should return {} without raising"


def test_decode_private_metadata_malformed_json_returns_empty_dict() -> None:
    result = decode_private_metadata("{not valid json")
    assert result == {}, "decode of malformed JSON should return {} without raising"


def test_decode_private_metadata_partial_json_returns_empty_dict() -> None:
    result = decode_private_metadata('{"team_id": "T123"')  # missing closing brace
    assert result == {}, "decode of truncated JSON should return {} without raising"


# ---------------------------------------------------------------------------
# apply_agent_modal — rename-forbidden shape
# ---------------------------------------------------------------------------


def test_apply_agent_modal_returns_model_and_system_fields() -> None:
    result = apply_agent_modal(
        model_id="claude-3-5-sonnet-20241022", system_prompt="You are helpful."
    )
    assert result.get("model") == "claude-3-5-sonnet-20241022", (
        "apply_agent_modal should include model in the returned dict"
    )
    assert result.get("system") == "You are helpful.", (
        "apply_agent_modal should include system in the returned dict"
    )


def test_apply_agent_modal_has_no_name_path() -> None:
    """Rename-forbidden: the function must not accept or expose a name/agent_name field."""
    import inspect

    sig = inspect.signature(apply_agent_modal)
    param_names = set(sig.parameters)
    assert "name" not in param_names, (
        "apply_agent_modal must not accept a 'name' parameter (rename is forbidden)"
    )
    assert "agent_name" not in param_names, (
        "apply_agent_modal must not accept an 'agent_name' parameter (rename is forbidden)"
    )


def test_apply_agent_modal_omits_none_fields() -> None:
    result = apply_agent_modal(model_id=None, system_prompt=None)
    assert result == {}, "apply_agent_modal with all-None args should return an empty dict"


def test_apply_agent_modal_returns_only_provided_fields() -> None:
    result = apply_agent_modal(model_id="claude-3-5-haiku-20241022", system_prompt=None)
    assert "model" in result, "apply_agent_modal should include model when provided"
    assert "system" not in result, "apply_agent_modal should omit system when system_prompt is None"


# ---------------------------------------------------------------------------
# apply_mcp_modal
# ---------------------------------------------------------------------------


def test_apply_mcp_modal_returns_expected_shape() -> None:
    result = apply_mcp_modal(name="my-server", endpoint="https://mcp.example.com", has_token=True)
    assert result["name"] == "my-server", "apply_mcp_modal should include name"
    assert result["url"] == "https://mcp.example.com", "apply_mcp_modal should include url"
    assert result["has_token"] is True, "apply_mcp_modal should include has_token"


# ---------------------------------------------------------------------------
# remove_skill_at
# ---------------------------------------------------------------------------


def test_remove_skill_at_drops_indexed_element() -> None:
    skills = ["skill-a", "skill-b", "skill-c"]
    result = remove_skill_at(skills, 1)
    assert result == ["skill-a", "skill-c"], (
        "remove_skill_at should drop the element at the given index"
    )


def test_remove_skill_at_out_of_range_returns_list_unchanged() -> None:
    skills = ["skill-a", "skill-b"]
    result = remove_skill_at(skills, 5)
    assert result == skills, (
        "remove_skill_at with out-of-range index should return the list unchanged"
    )


def test_remove_skill_at_negative_index_returns_list_unchanged() -> None:
    skills = ["skill-a", "skill-b"]
    result = remove_skill_at(skills, -1)
    assert result == skills, "remove_skill_at with negative index should return the list unchanged"


def test_remove_skill_at_does_not_mutate_original() -> None:
    skills = ["skill-a", "skill-b"]
    _ = remove_skill_at(skills, 0)
    assert skills == ["skill-a", "skill-b"], "remove_skill_at must not mutate the original list"


# ---------------------------------------------------------------------------
# remove_mcp_at
# ---------------------------------------------------------------------------


def test_remove_mcp_at_drops_indexed_element() -> None:
    mcps = ["mcp-alpha", "mcp-beta", "mcp-gamma"]
    result = remove_mcp_at(mcps, 0)
    assert result == ["mcp-beta", "mcp-gamma"], (
        "remove_mcp_at should drop the element at the given index"
    )


def test_remove_mcp_at_out_of_range_returns_list_unchanged() -> None:
    mcps = ["mcp-alpha", "mcp-beta"]
    result = remove_mcp_at(mcps, 10)
    assert result == mcps, "remove_mcp_at with out-of-range index should return the list unchanged"


def test_remove_mcp_at_negative_index_returns_list_unchanged() -> None:
    mcps = ["mcp-alpha", "mcp-beta"]
    result = remove_mcp_at(mcps, -1)
    assert result == mcps, "remove_mcp_at with negative index should return the list unchanged"


def test_remove_mcp_at_does_not_mutate_original() -> None:
    mcps = ["mcp-alpha", "mcp-beta"]
    _ = remove_mcp_at(mcps, 0)
    assert mcps == ["mcp-alpha", "mcp-beta"], "remove_mcp_at must not mutate the original list"
