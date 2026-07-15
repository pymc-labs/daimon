"""Unit tests for daimon.core.defaults.mcp_merge.

Eight cases for merge_default_mcp_server (server-side) and
eight cases for merge_default_mcp_toolset (toolset-side). Pure-function tests:
no I/O, no fixtures, no transport.
"""

from __future__ import annotations

from typing import cast

from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.core.defaults.mcp_merge import (
    get_reserved_mcp_rejection,
    is_corrupted_daimon_mcp_entry,
    merge_default_mcp_server,
    merge_default_mcp_toolset,
)

_DEFAULT_URL = "https://daimon.example/mcp"
_OTHER_URL = "https://other.example/mcp"


def _entry(url: str, name: str = "other-mcp") -> BetaManagedAgentsURLMCPServerParams:
    return cast(BetaManagedAgentsURLMCPServerParams, {"name": name, "type": "url", "url": url})


# ---------------------------------------------------------------------------
# get_reserved_mcp_rejection tests
# ---------------------------------------------------------------------------


def test_get_reserved_mcp_rejection_rejects_reserved_name_when_public_url_is_none() -> None:
    """Reserved server name is rejected regardless of public_url being None."""
    result = get_reserved_mcp_rejection(server_name="daimon-mcp", url=_OTHER_URL, public_url=None)
    assert result is not None, (
        "reserved name must return a rejection reason even when public_url is None"
    )
    assert "reserved built-in daimon server" in result, (
        "rejection reason must contain the reserved-name substring"
    )


def test_get_reserved_mcp_rejection_rejects_reserved_name_when_public_url_is_set() -> None:
    """Reserved server name is rejected even when public_url is set and url is unrelated."""
    result = get_reserved_mcp_rejection(
        server_name="daimon-mcp", url=_OTHER_URL, public_url=_DEFAULT_URL
    )
    assert result is not None, "reserved name must return a rejection reason when public_url is set"
    assert "reserved built-in daimon server" in result, (
        "rejection reason must contain the reserved-name substring"
    )


def test_get_reserved_mcp_rejection_rejects_url_equal_to_public_url_exact_match() -> None:
    """URL equal to public_url under a non-reserved name is rejected (exact match)."""
    result = get_reserved_mcp_rejection(
        server_name="my-server", url=_DEFAULT_URL, public_url=_DEFAULT_URL
    )
    assert result is not None, "own-endpoint URL must return a rejection reason"
    assert "deployment's own MCP endpoint" in result, (
        "rejection reason must contain the own-endpoint substring"
    )


def test_get_reserved_mcp_rejection_rejects_url_with_trailing_slash_vs_bare_public_url() -> None:
    """URL with trailing slash matches bare public_url (slash-insensitive)."""
    result = get_reserved_mcp_rejection(
        server_name="my-server", url=_DEFAULT_URL + "/", public_url=_DEFAULT_URL
    )
    assert result is not None, "url with trailing slash must match bare public_url"
    assert "deployment's own MCP endpoint" in result, (
        "rejection reason must contain the own-endpoint substring"
    )


def test_get_reserved_mcp_rejection_rejects_bare_url_vs_public_url_with_trailing_slash() -> None:
    """Bare URL matches public_url with trailing slash (slash-insensitive)."""
    result = get_reserved_mcp_rejection(
        server_name="my-server", url=_DEFAULT_URL, public_url=_DEFAULT_URL + "/"
    )
    assert result is not None, "bare url must match public_url with trailing slash"
    assert "deployment's own MCP endpoint" in result, (
        "rejection reason must contain the own-endpoint substring"
    )


def test_get_reserved_mcp_rejection_returns_none_for_unrelated_name_and_url() -> None:
    """Unrelated server name and URL returns None (attach is allowed)."""
    result = get_reserved_mcp_rejection(
        server_name="context7", url=_OTHER_URL, public_url=_DEFAULT_URL
    )
    assert result is None, "unrelated name+url must return None (no rejection)"


def test_get_reserved_mcp_rejection_returns_none_when_public_url_is_none_and_name_non_reserved() -> (
    None
):
    """Non-reserved name with public_url=None returns None (no URL check fires)."""
    result = get_reserved_mcp_rejection(server_name="context7", url=_OTHER_URL, public_url=None)
    assert result is None, "non-reserved name with public_url=None must return None"


# ---------------------------------------------------------------------------
# merge_default_mcp_server tests
# ---------------------------------------------------------------------------


def test_merge_default_mcp_server_returns_input_when_public_url_is_none() -> None:
    """public_url=None -> returns the same list object (no copy, no append)."""
    existing = [_entry(_OTHER_URL)]
    result = merge_default_mcp_server(existing, None)
    assert result is existing, "public_url=None must return the input object unchanged"


def test_merge_default_mcp_server_returns_none_when_input_is_none_and_url_is_none() -> None:
    """input=None, public_url=None -> returns None."""
    result = merge_default_mcp_server(None, None)
    assert result is None, "None input with None public_url must return None"


def test_merge_default_mcp_server_appends_default_to_empty_list() -> None:
    """Empty list + non-None url -> [default entry with name and url]."""
    result = merge_default_mcp_server([], _DEFAULT_URL)
    assert result is not None, "non-None public_url must return a list"
    assert len(result) == 1, "result must have exactly one entry"
    assert result[0].get("url") == _DEFAULT_URL, "appended entry must have the default url"
    assert result[0].get("type") == "url", "appended entry must have type='url'"


def test_merge_default_mcp_server_appends_default_when_input_is_none_and_url_is_set() -> None:
    """input=None, non-None url -> [default entry]."""
    result = merge_default_mcp_server(None, _DEFAULT_URL)
    assert result is not None, "None input with non-None public_url must return a list"
    assert len(result) == 1, "result must have exactly one entry"
    assert result[0].get("url") == _DEFAULT_URL, (
        "None input with non-None public_url must return entry with default url"
    )


def test_merge_default_mcp_server_preserves_author_entries_and_appends_default() -> None:
    """Author entry present, different url -> returns [author, default] in order."""
    existing = [_entry(_OTHER_URL)]
    result = merge_default_mcp_server(existing, _DEFAULT_URL)
    assert result is not None, "result must be a list"
    assert len(result) == 2, "result must have author entry plus default entry"
    assert result[0].get("url") == _OTHER_URL, "first entry must be the author entry"
    assert result[1].get("url") == _DEFAULT_URL, "second entry must be the default"


def test_merge_default_mcp_server_is_idempotent_when_default_already_present() -> None:
    """Default URL already in list -> returns input unchanged (no duplicate, same object)."""
    existing = [_entry(_DEFAULT_URL, name="daimon-mcp")]
    result = merge_default_mcp_server(existing, _DEFAULT_URL)
    assert result is existing, "default already present must return the same input object"
    assert len(existing) == 1, "idempotent call must not grow the list"


def test_merge_default_mcp_server_does_not_mutate_input_when_appending() -> None:
    """Appending default must not mutate the original list."""
    existing = [_entry(_OTHER_URL)]
    original_len = len(existing)
    merge_default_mcp_server(existing, _DEFAULT_URL)
    assert len(existing) == original_len, "merge_default_mcp_server must not mutate the input list"


def test_merge_default_mcp_server_running_twice_does_not_grow_output() -> None:
    """Calling merge twice on the same url produces a list of length 1, not 2."""
    first = merge_default_mcp_server([], _DEFAULT_URL)
    assert first is not None, "first call must return a non-None list"
    second = merge_default_mcp_server(first, _DEFAULT_URL)
    assert second is not None, "second call must return a non-None list"
    result: list[BetaManagedAgentsURLMCPServerParams] = second
    assert len(result) == 1, "re-merging on the output must not grow the list beyond 1"


# ---------------------------------------------------------------------------
# Self-heal + predicate tests (D-06)
# ---------------------------------------------------------------------------


def test_is_corrupted_daimon_mcp_entry_returns_true_when_name_matches_and_url_differs() -> None:
    """Predicate is true when name is daimon-mcp and url does not match public_url."""
    assert is_corrupted_daimon_mcp_entry(
        name="daimon-mcp", url=_OTHER_URL, public_url=_DEFAULT_URL
    ), "daimon-mcp entry with foreign url must be detected as corrupted"


def test_is_corrupted_daimon_mcp_entry_returns_false_for_canonical_entry() -> None:
    """Predicate is false when name is daimon-mcp and url matches public_url exactly."""
    assert not is_corrupted_daimon_mcp_entry(
        name="daimon-mcp", url=_DEFAULT_URL, public_url=_DEFAULT_URL
    ), "canonical daimon-mcp entry must not be flagged as corrupted"


def test_is_corrupted_daimon_mcp_entry_returns_false_for_canonical_entry_slash_variant() -> None:
    """Predicate is false when url matches public_url with trailing-slash difference."""
    assert not is_corrupted_daimon_mcp_entry(
        name="daimon-mcp", url=_DEFAULT_URL + "/", public_url=_DEFAULT_URL
    ), "daimon-mcp entry with trailing-slash canonical url must not be flagged as corrupted"


def test_is_corrupted_daimon_mcp_entry_returns_false_for_foreign_name() -> None:
    """Predicate is false when name is not daimon-mcp, even if url differs from public_url."""
    assert not is_corrupted_daimon_mcp_entry(
        name="context7", url=_OTHER_URL, public_url=_DEFAULT_URL
    ), "non-reserved name entry must not be flagged as corrupted"


def test_merge_default_mcp_server_replaces_corrupted_entry_not_appends() -> None:
    """A daimon-mcp-named entry with a foreign URL is REPLACED by the canonical entry,
    not appended beside it. Result has exactly one daimon-mcp entry with the canonical url."""
    corrupted = _entry(_OTHER_URL, name="daimon-mcp")
    existing = [corrupted]
    result = merge_default_mcp_server(existing, _DEFAULT_URL)
    assert result is not None, "result must be a list"
    daimon_entries = [e for e in result if e.get("name") == "daimon-mcp"]
    assert len(daimon_entries) == 1, (
        "corrupted entry must be replaced: exactly one daimon-mcp entry in result"
    )
    assert daimon_entries[0].get("url") == _DEFAULT_URL, (
        "the sole daimon-mcp entry must carry the canonical url"
    )


def test_merge_default_mcp_server_healthy_list_returns_same_object() -> None:
    """Healthy list with canonical daimon-mcp entry returns the same input object (no-churn)."""
    existing = [_entry(_DEFAULT_URL, name="daimon-mcp")]
    result = merge_default_mcp_server(existing, _DEFAULT_URL)
    assert result is existing, (
        "healthy list must return the same input object (identity no-churn contract)"
    )


def test_merge_default_mcp_server_author_entries_pass_through_alongside_heal() -> None:
    """Author (non-daimon-mcp) entries survive alongside a heal of a corrupted daimon-mcp entry."""
    author = _entry(_OTHER_URL, name="context7")
    corrupted = _entry("https://corrupt.example/mcp", name="daimon-mcp")
    existing = [author, corrupted]
    result = merge_default_mcp_server(existing, _DEFAULT_URL)
    assert result is not None, "result must be a list"
    names = [e.get("name") for e in result]
    assert "context7" in names, "author entry must survive the heal"
    assert names.count("daimon-mcp") == 1, "exactly one daimon-mcp entry after heal"
    canonical = next(e for e in result if e.get("name") == "daimon-mcp")
    assert canonical.get("url") == _DEFAULT_URL, (
        "the sole daimon-mcp entry must carry the canonical url"
    )


def test_merge_default_mcp_server_degenerate_double_daimon_mcp_collapses_to_one() -> None:
    """Degenerate input with two daimon-mcp-named entries (both corrupted) collapses to
    exactly one canonical entry."""
    corrupt1 = _entry("https://corrupt1.example/mcp", name="daimon-mcp")
    corrupt2 = _entry("https://corrupt2.example/mcp", name="daimon-mcp")
    existing = [corrupt1, corrupt2]
    result = merge_default_mcp_server(existing, _DEFAULT_URL)
    assert result is not None, "result must be a list"
    daimon_entries = [e for e in result if e.get("name") == "daimon-mcp"]
    assert len(daimon_entries) == 1, (
        "degenerate double-daimon-mcp input must collapse to exactly one canonical entry"
    )
    assert daimon_entries[0].get("url") == _DEFAULT_URL, (
        "the collapsed entry must carry the canonical url"
    )


# ---------------------------------------------------------------------------
# Toolset-side helpers for merge_default_mcp_toolset tests
# ---------------------------------------------------------------------------

_DEFAULT_TOOLSET_NAME = "daimon-mcp"
_OTHER_TOOLSET_NAME = "other-mcp"


def _toolset(server_name: str) -> Tool:
    """Build a minimal mcp_toolset Tool entry referencing `server_name`."""
    return cast(Tool, {"type": "mcp_toolset", "mcp_server_name": server_name})


def _agent_toolset() -> Tool:
    """A non-mcp toolset for 'preserves other tool types' coverage."""
    return cast(Tool, {"type": "agent_toolset_20260401", "configs": [{"name": "bash"}]})


def test_merge_default_mcp_toolset_returns_input_when_public_url_is_none() -> None:
    """public_url=None -> returns the same list object (no copy, no append)."""
    existing = [_toolset(_OTHER_TOOLSET_NAME)]
    result = merge_default_mcp_toolset(existing, None)
    assert result is existing, "public_url=None must return the input object unchanged"


def test_merge_default_mcp_toolset_returns_none_when_input_is_none_and_url_is_none() -> None:
    """input=None, public_url=None -> returns None."""
    result = merge_default_mcp_toolset(None, None)
    assert result is None, "None input with None public_url must return None"


def test_merge_default_mcp_toolset_appends_default_to_empty_list() -> None:
    """Empty list + non-None url -> [default mcp_toolset entry for daimon-mcp]."""
    result = merge_default_mcp_toolset([], _DEFAULT_URL)
    assert result is not None, "non-None public_url must return a list"
    assert len(result) == 1, "result must have exactly one entry"
    assert result[0].get("type") == "mcp_toolset", "appended entry must have type='mcp_toolset'"
    assert result[0].get("mcp_server_name") == "daimon-mcp", (
        "appended entry must reference mcp_server_name='daimon-mcp'"
    )


def test_merge_default_mcp_toolset_appends_default_when_input_is_none_and_url_is_set() -> None:
    """input=None, non-None url -> [default mcp_toolset entry]."""
    result = merge_default_mcp_toolset(None, _DEFAULT_URL)
    assert result is not None, "None input with non-None public_url must return a list"
    assert len(result) == 1, "result must have exactly one entry"
    assert result[0].get("type") == "mcp_toolset", (
        "None input with non-None public_url must return entry with type='mcp_toolset'"
    )
    assert result[0].get("mcp_server_name") == "daimon-mcp", (
        "None input with non-None public_url must return entry with mcp_server_name='daimon-mcp'"
    )


def test_merge_default_mcp_toolset_preserves_author_toolset_for_other_server_and_appends_default() -> (
    None
):
    """existing has toolset for other server + url set -> both preserved, [other, daimon-mcp]."""
    existing = [_toolset(_OTHER_TOOLSET_NAME)]
    result = merge_default_mcp_toolset(existing, _DEFAULT_URL)
    assert result is not None, "result must be a list"
    assert len(result) == 2, "result must have author toolset plus daimon-mcp toolset"
    assert result[0].get("mcp_server_name") == _OTHER_TOOLSET_NAME, (
        "first entry must be the author toolset"
    )
    assert result[1].get("mcp_server_name") == _DEFAULT_TOOLSET_NAME, (
        "second entry must be the default daimon-mcp toolset"
    )


def test_merge_default_mcp_toolset_is_idempotent_when_default_already_present() -> None:
    """daimon-mcp toolset already in list -> returns input unchanged (same object, no growth)."""
    existing = [_toolset(_DEFAULT_TOOLSET_NAME)]
    result = merge_default_mcp_toolset(existing, _DEFAULT_URL)
    assert result is existing, "default already present must return the same input object"
    assert len(existing) == 1, "idempotent call must not grow the list"


def test_merge_default_mcp_toolset_preserves_other_tool_types_and_appends_default() -> None:
    """existing has agent_toolset_20260401 + url set -> both preserved; mcp_toolset appended last."""
    existing = [_agent_toolset()]
    result = merge_default_mcp_toolset(existing, _DEFAULT_URL)
    assert result is not None, "result must be a list"
    assert len(result) == 2, "result must have agent_toolset plus mcp_toolset"
    assert result[0].get("type") == "agent_toolset_20260401", (
        "first entry must be the author agent_toolset_20260401"
    )
    assert result[1].get("type") == "mcp_toolset", "second entry must be the appended mcp_toolset"
    assert result[1].get("mcp_server_name") == "daimon-mcp", (
        "appended mcp_toolset must reference mcp_server_name='daimon-mcp'"
    )


def test_merge_default_mcp_toolset_running_twice_does_not_grow_output() -> None:
    """Calling merge twice on the same url produces a list of length 1, not 2."""
    first = merge_default_mcp_toolset([], _DEFAULT_URL)
    assert first is not None, "first call must return a non-None list"
    second = merge_default_mcp_toolset(first, _DEFAULT_URL)
    assert second is not None, "second call must return a non-None list"
    result: list[Tool] = second
    assert len(result) == 1, "re-merging on the output must not grow the list beyond 1"
