"""The GitHub MCP router resolves each call to repos and owners, then picks one token.

Pure tests: the tool rule table, and token choice over two installations,
permission chunks, narrowed and removed grants, expiry, supersession, the
owner-free fallback and batches.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core.github_app_auth import PERMISSION_PROFILES, PermissionProfile
from daimon.core.github_router_tokens import (
    ONE_SCOPE_MESSAGE,
    REFRESHING_MESSAGE,
    TOOL_RULES,
    GrantedRepo,
    GrantView,
    NamedTargets,
    NeedsOneScope,
    NotUsable,
    OwnerFree,
    RepoScope,
    RouterToken,
    TokenChoice,
    TokenDecision,
    Unsupported,
    UnsupportedCall,
    choose_batch_token,
    choose_token,
    resolve_call,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)

A_ONE = GrantedRepo(101, "a/one", 771, "write")
B_TWO = GrantedRepo(102, "b/two", 772, "write")
B_THREE = GrantedRepo(103, "b/three", 772, "write")
VIEW = GrantView((A_ONE, B_TWO, B_THREE))


def _row(
    installation_id: int,
    repo_ids: set[int],
    *,
    chunk: int = 0,
    profile: PermissionProfile = "write",
    permissions: Mapping[str, str] | None = None,
    expires_in: timedelta = timedelta(minutes=50),
    superseded: bool = False,
    status: str = "delivered",
) -> RouterToken:
    token_id = uuid.uuid4()
    return RouterToken(
        token_id=token_id,
        installation_id=installation_id,
        chunk=chunk,
        repo_ids=frozenset(repo_ids),
        permissions=dict(permissions or PERMISSION_PROFILES[profile]),
        status=status,
        superseded_at=NOW - timedelta(minutes=1) if superseded else None,
        expires_at=NOW + expires_in,
        token=f"ghs_{token_id.hex}",
    )


def _choose(
    scope: RepoScope,
    rows: list[RouterToken],
    view: GrantView = VIEW,
    working_repo: int | None = None,
) -> TokenDecision:
    return choose_token(scope, rows, view, working_repo, now=NOW)


def _chosen(decision: TokenDecision) -> RouterToken:
    assert isinstance(decision, TokenChoice), f"expected a token, got {decision!r}"
    return decision.token


def _file(owner: str, repo: str) -> RepoScope:
    return resolve_call("get_file_contents", {"owner": owner, "repo": repo, "path": "README.md"})


# --- resolve_call -------------------------------------------------------------


def test_owner_and_repo_name_one_repo_case_insensitively() -> None:
    assert _file("A", "One") == NamedTargets(repos=frozenset({"a/one"}))


def test_owner_without_repo_is_refused_where_repo_is_required() -> None:
    scope = resolve_call("get_file_contents", {"owner": "a", "path": "README.md"})
    assert isinstance(scope, Unsupported)


def test_owner_alone_names_the_owner_where_repo_is_optional() -> None:
    assert resolve_call("list_discussions", {"owner": "b"}) == NamedTargets(owners=frozenset({"b"}))
    assert resolve_call("projects_list", {"owner": "B", "method": "list_projects"}) == (
        NamedTargets(owners=frozenset({"b"}))
    )


def test_org_and_organization_arguments_name_an_owner() -> None:
    assert resolve_call("get_team_members", {"org": "b", "team_slug": "x"}) == NamedTargets(
        owners=frozenset({"b"})
    )
    assert resolve_call("create_repository", {"name": "x", "organization": "b"}) == (
        NamedTargets(owners=frozenset({"b"}))
    )
    assert resolve_call("create_repository", {"name": "x"}) == OwnerFree()


def test_source_and_destination_are_both_named() -> None:
    assert resolve_call(
        "fork_repository", {"owner": "a", "repo": "one", "organization": "b"}
    ) == NamedTargets(repos=frozenset({"a/one"}), owners=frozenset({"b"}))
    assert resolve_call(
        "issue_write",
        {"owner": "a", "repo": "one", "parent_owner": "b", "parent_repo": "two"},
    ) == NamedTargets(repos=frozenset({"a/one", "b/two"}))
    assert resolve_call(
        "projects_write", {"owner": "a", "item_owner": "b", "item_repo": "two", "method": "x"}
    ) == NamedTargets(repos=frozenset({"b/two"}), owners=frozenset({"a"}))


def test_pull_request_head_from_another_owner_names_that_owner() -> None:
    base = {"owner": "a", "repo": "one", "base": "main", "title": "t"}
    assert resolve_call("create_pull_request", {**base, "head": "b:feature"}) == NamedTargets(
        repos=frozenset({"a/one"}), owners=frozenset({"b"})
    )
    assert resolve_call("create_pull_request", {**base, "head": "a:feature"}) == NamedTargets(
        repos=frozenset({"a/one"})
    )
    assert resolve_call("create_pull_request", {**base, "head": "feature"}) == NamedTargets(
        repos=frozenset({"a/one"})
    )


def test_query_qualifiers_name_repos_and_owners_and_negations_do_not() -> None:
    scope = resolve_call(
        "search_code", {"query": 'needle repo:A/One org:b user:"c" -repo:x/y -org:z'}
    )
    assert scope == NamedTargets(repos=frozenset({"a/one"}), owners=frozenset({"b", "c"}))


def test_unqualified_search_is_owner_free() -> None:
    assert resolve_call("search_code", {"query": "needle language:python"}) == OwnerFree()


def test_search_with_owner_and_repo_arguments_adds_them_to_the_query() -> None:
    scope = resolve_call("search_issues", {"owner": "a", "repo": "one", "query": "org:b bug"})
    assert scope == NamedTargets(repos=frozenset({"a/one"}), owners=frozenset({"b"}))


def test_malformed_repo_qualifier_is_refused() -> None:
    assert isinstance(resolve_call("search_code", {"query": "repo:noslash"}), Unsupported)


def test_owner_free_tools_name_nothing() -> None:
    assert resolve_call("get_me", {}) == OwnerFree()
    assert resolve_call("get_me", None) == OwnerFree()
    assert resolve_call("list_notifications", {}) == OwnerFree()


def test_unknown_tool_with_repo_like_arguments_is_refused() -> None:
    scope = resolve_call("get_repository_secrets", {"owner": "a", "repo": "one"})
    assert scope == Unsupported("get_repository_secrets", "tool is not in the verified tool table")
    assert isinstance(resolve_call("brand_new_tool", {"target_repo": "a/one"}), Unsupported)


def test_unknown_tool_without_repo_like_arguments_is_owner_free() -> None:
    assert resolve_call("brand_new_tool", {"id": 7}) == OwnerFree()


def test_non_string_and_enterprise_arguments_are_refused() -> None:
    assert isinstance(resolve_call("get_file_contents", {"owner": 1, "repo": "one"}), Unsupported)
    assert isinstance(
        resolve_call("custom_properties_read", {"enterprise": "corp", "level": "enterprise"}),
        Unsupported,
    )
    assert resolve_call("custom_properties_read", {"org": "b", "level": "organization"}) == (
        NamedTargets(owners=frozenset({"b"}))
    )


def test_table_covers_the_common_repository_tools() -> None:
    for tool in (
        "get_file_contents",
        "create_or_update_file",
        "push_files",
        "list_branches",
        "issue_read",
        "pull_request_read",
        "search_code",
        "get_me",
    ):
        assert tool in TOOL_RULES, tool


# --- choose_token -------------------------------------------------------------


def test_two_installations_each_get_their_own_token() -> None:
    row_a, row_b = _row(771, {101}), _row(772, {102, 103})
    assert _chosen(_choose(_file("a", "one"), [row_a, row_b])) == row_a
    assert _chosen(_choose(_file("b", "two"), [row_a, row_b])) == row_b


def test_two_chunks_in_one_installation_route_by_repo() -> None:
    chunk_0, chunk_1 = _row(772, {102}, chunk=0), _row(772, {103}, chunk=1)
    assert _chosen(_choose(_file("b", "two"), [chunk_0, chunk_1])) == chunk_0
    assert _chosen(_choose(_file("b", "three"), [chunk_0, chunk_1])) == chunk_1


def test_owner_search_across_two_chunks_needs_one_scope() -> None:
    chunk_0, chunk_1 = _row(772, {102}, chunk=0), _row(772, {103}, chunk=1)
    scope = resolve_call("search_code", {"query": "needle org:b"})
    assert _choose(scope, [_row(771, {101}), chunk_0, chunk_1]) == NeedsOneScope()


def test_owner_search_served_by_the_chunk_covering_all_of_the_owners_repos() -> None:
    row_a, row_b = _row(771, {101}), _row(772, {102, 103})
    scope = resolve_call("search_code", {"query": "needle org:B"})
    assert _chosen(_choose(scope, [row_a, row_b])) == row_b


def test_repos_in_two_installations_in_one_call_need_one_scope() -> None:
    scope = resolve_call("search_code", {"query": "repo:a/one repo:b/two"})
    decision = _choose(scope, [_row(771, {101}), _row(772, {102, 103})])
    assert decision == NeedsOneScope()
    assert isinstance(decision, NeedsOneScope)
    assert decision.message == ONE_SCOPE_MESSAGE


def test_ungranted_public_repo_gets_the_owner_free_token() -> None:
    # The direct path reads public repos such as pymc-devs/pymc-marketing with
    # the working token; GitHub keeps a private ungranted repo a 404.
    row_a, row_b = _row(771, {101}), _row(772, {102, 103})
    public = _file("pymc-devs", "pymc-marketing")
    assert _chosen(_choose(public, [row_a, row_b], working_repo=102)) == row_b
    assert _chosen(_choose(public, [row_a, row_b])) == row_a
    org = resolve_call("search_code", {"query": "needle org:pymc-devs"})
    assert _chosen(_choose(org, [row_a, row_b], working_repo=102)) == row_b


def test_ungranted_target_rides_on_the_token_the_granted_repo_needs() -> None:
    row_a, row_b = _row(771, {101}), _row(772, {102, 103})
    mixed = resolve_call("search_code", {"query": "repo:b/two repo:pymc-devs/pymc-marketing"})
    assert _chosen(_choose(mixed, [row_a, row_b], working_repo=101)) == row_b
    fork = resolve_call("fork_repository", {"owner": "a", "repo": "one", "organization": "c"})
    assert _chosen(_choose(fork, [row_a, row_b], working_repo=102)) == row_a


def test_row_broader_than_a_narrowed_grant_is_not_used() -> None:
    narrowed = GrantView((GrantedRepo(101, "a/one", 771, "read"), B_TWO, B_THREE))
    write_a, row_b = _row(771, {101}, profile="write"), _row(772, {102, 103})
    decision = _choose(_file("a", "one"), [write_a, row_b], narrowed)
    assert decision == NotUsable()
    assert isinstance(decision, NotUsable)
    assert decision.message == REFRESHING_MESSAGE
    # The other installation still works, and a read token serves the read grant.
    assert _chosen(_choose(_file("b", "two"), [write_a, row_b], narrowed)) == row_b
    read_a = _row(771, {101}, profile="read")
    assert _chosen(_choose(_file("a", "one"), [write_a, read_a], narrowed)) == read_a


def test_unknown_permission_level_counts_as_broader() -> None:
    odd = _row(771, {101}, permissions={"contents": "maintain"})
    assert _choose(_file("a", "one"), [odd]) == NotUsable()


def test_row_holding_a_removed_repo_is_not_used() -> None:
    without_three = GrantView((A_ONE, B_TWO))
    rows = [_row(771, {101}), _row(772, {102, 103})]
    assert _choose(_file("b", "two"), rows, without_three) == NotUsable()
    # A removed repo is outside the grants: it rides on the owner-free token.
    assert _chosen(_choose(_file("b", "three"), rows, without_three)) == rows[0]


def test_row_whose_repo_moved_installation_is_not_used() -> None:
    moved = GrantView((GrantedRepo(101, "a/one", 999, "write"), B_TWO, B_THREE))
    assert _choose(_file("a", "one"), [_row(771, {101})], moved) == NotUsable()


def test_token_expiring_within_a_minute_is_not_used() -> None:
    soon = _row(771, {101}, expires_in=timedelta(seconds=30))
    assert _choose(_file("a", "one"), [soon]) == NotUsable()
    later = _row(771, {101}, expires_in=timedelta(minutes=2))
    assert _chosen(_choose(_file("a", "one"), [soon, later])) == later


def test_superseded_and_undelivered_rows_are_not_used() -> None:
    old = _row(771, {101}, superseded=True)
    stored = _row(771, {101}, status="stored")
    assert _choose(_file("a", "one"), [old, stored]) == NotUsable()
    new = _row(771, {101})
    assert _chosen(_choose(_file("a", "one"), [old, stored, new])) == new


def test_rotation_overlap_prefers_the_token_that_expires_last() -> None:
    old = _row(771, {101}, expires_in=timedelta(minutes=10))
    new = _row(771, {101}, expires_in=timedelta(minutes=55))
    assert _chosen(_choose(_file("a", "one"), [old, new])) == new


def test_owner_free_uses_the_working_repo_token() -> None:
    row_a, row_b = _row(771, {101}), _row(772, {102, 103})
    assert _chosen(_choose(OwnerFree(), [row_a, row_b], working_repo=103)) == row_b


def test_owner_free_without_working_repo_uses_lowest_installation_and_chunk() -> None:
    rows = [_row(772, {103}, chunk=1), _row(772, {102}, chunk=0), _row(771, {101})]
    assert _chosen(_choose(OwnerFree(), rows)) == rows[2]
    assert _chosen(_choose(OwnerFree(), rows[:2])) == rows[1]


def test_owner_free_falls_back_when_the_working_repo_has_no_usable_token() -> None:
    row_a = _row(771, {101})
    expired_b = _row(772, {102, 103}, expires_in=timedelta(seconds=10))
    assert _chosen(_choose(OwnerFree(), [row_a, expired_b], working_repo=102)) == row_a


def test_owner_free_with_no_usable_token_is_refreshing() -> None:
    assert _choose(OwnerFree(), []) == NotUsable()


def test_batch_served_only_by_one_token() -> None:
    row_a, row_b = _row(771, {101}), _row(772, {102, 103})
    rows = [row_a, row_b]
    same = [_file("b", "two"), _file("b", "three")]
    assert _chosen(choose_batch_token(same, rows, VIEW, None, now=NOW)) == row_b
    mixed = [_file("a", "one"), _file("b", "two")]
    assert choose_batch_token(mixed, rows, VIEW, None, now=NOW) == NeedsOneScope()


def test_batch_with_owner_free_call_uses_the_fallback_token() -> None:
    row_a, row_b = _row(771, {101}), _row(772, {102, 103})
    rows = [row_a, row_b]
    with_me = [_file("a", "one"), resolve_call("get_me", {})]
    assert _chosen(choose_batch_token(with_me, rows, VIEW, 101, now=NOW)) == row_a
    other = [_file("b", "two"), resolve_call("get_me", {})]
    assert choose_batch_token(other, rows, VIEW, 101, now=NOW) == NeedsOneScope()


def test_batch_reports_the_strongest_refusal() -> None:
    rows = [_row(771, {101}), _row(772, {102, 103})]
    calls = [
        _file("a", "one"),
        _file("c", "secret"),
        resolve_call("get_repository_secrets", {"owner": "a", "repo": "one"}),
    ]
    decision = choose_batch_token(calls, rows, VIEW, None, now=NOW)
    assert isinstance(decision, UnsupportedCall)
    stale = [_file("a", "one"), _file("b", "two")]
    refreshing = [_row(771, {101}), _row(772, {102, 103}, expires_in=timedelta(seconds=5))]
    assert choose_batch_token(stale, refreshing, VIEW, None, now=NOW) == NotUsable()
    with pytest.raises(ValueError, match="at least one call"):
        choose_batch_token([], rows, VIEW, None, now=NOW)


def test_unknown_tool_is_refused_with_its_name() -> None:
    scope = resolve_call("get_repository_secrets", {"owner": "a", "repo": "one"})
    decision = _choose(scope, [_row(771, {101})])
    assert isinstance(decision, UnsupportedCall)
    assert "get_repository_secrets" in decision.message


def test_token_value_is_not_in_the_repr() -> None:
    row = _row(771, {101})
    assert row.token not in repr(row)
    assert row.token not in repr(TokenChoice(row))
