"""Compatibility decisions for a live session whose configuration drifted."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from daimon.core.session_compat import (
    DEFAULT_MA_CAPABILITIES,
    ChangeReason,
    MaCapabilities,
    RemirrorVaultCredentials,
    ReplaceEnvFile,
    ReplaceSession,
    ReplaceToolsAndMcpServers,
    ReuseAsIs,
    RotateAppTokens,
    RotateRepoToken,
    UpdateInPlace,
    decide_session_compatibility,
    identity_change_reasons,
)
from daimon.core.session_snapshot import SessionSnapshot

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)


def make_snapshot(**overrides: Any) -> SessionSnapshot:
    """A baseline snapshot with every axis set, overridden field by field."""
    fields: dict[str, Any] = {
        "ma_agent_id": "agent_baseline",
        "model_id": "claude-sonnet-5",
        "system_sha256": "system-hash",
        "skills_sha256": "skills-hash",
        "environment_id": "env_baseline",
        "repo_url": "https://github.com/acme/data",
        "repo_branch": "main",
        "memory_store_id": "memstore_baseline",
        "vault_id": None,
        "tools_sha256": "tools-hash",
        "mcp_servers_sha256": "mcp-hash",
        "env_sha256": "env-hash",
        "agent_version": 3,
        "agent_name": "research-bot",
    }
    fields.update(overrides)
    return SessionSnapshot(**fields)


def test_decision_is_reuse_when_recorded_and_desired_are_identical() -> None:
    recorded = make_snapshot()
    decision = decide_session_compatibility(
        recorded=recorded,
        desired=make_snapshot(),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == ReuseAsIs(), "identical snapshots must reuse the session untouched"


def test_predeploy_legacy_snapshot_with_bound_repo_is_reused() -> None:
    recorded = SessionSnapshot.model_validate(make_snapshot().model_dump(exclude={"repo_urls"}))
    desired = make_snapshot(repo_urls=())
    assert recorded.repo_url == "https://github.com/acme/data"
    assert (
        decide_session_compatibility(
            recorded=recorded,
            desired=desired,
            capabilities=DEFAULT_MA_CAPABILITIES,
            now=NOW,
        )
        == ReuseAsIs()
    )


def test_app_token_swap_keeps_session_but_repo_set_change_replaces_it() -> None:
    url = "https://github.com/example/one"
    recorded = make_snapshot(
        github_mode="app",
        repo_url=None,
        repo_branch=None,
        repo_urls=(url,),
        repo_resource_ids={url: "repo-resource"},
        vault_id="session-vault",
    )
    desired = recorded.model_copy(update={"repo_resource_ids": {}})
    decision = decide_session_compatibility(
        recorded=recorded,
        desired=desired,
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == UpdateInPlace(
        ops=(
            RotateAppTokens(resource_ids={url: "repo-resource"}),
            RemirrorVaultCredentials(),
        ),
        reasons=("repo_token_age",),
    )
    fresh = recorded.model_copy(update={"repo_token_issued_at": int(NOW.timestamp())})
    assert (
        decide_session_compatibility(
            recorded=fresh,
            desired=desired,
            capabilities=DEFAULT_MA_CAPABILITIES,
            now=NOW,
        )
        == ReuseAsIs()
    )
    expanded = desired.model_copy(update={"repo_urls": (url, "https://github.com/example/two")})
    assert decide_session_compatibility(
        recorded=recorded,
        desired=expanded,
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    ) == ReplaceSession(reasons=("repo_set",))


def test_decision_ignores_handles_and_diagnostics_when_only_those_differ() -> None:
    recorded = make_snapshot(
        env_resource_id="res_env_1",
        env_file_id="file_env_1",
        repo_resource_id="res_repo_1",
        repo_mount_path="/mnt/repo",
        agent_version=9,
        agent_name="renamed-bot",
    )
    decision = decide_session_compatibility(
        recorded=recorded,
        desired=make_snapshot(),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == ReuseAsIs(), "handles and diagnostics are not configuration differences"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("ma_agent_id", "agent_other", "agent_identity"),
        ("model_id", "claude-haiku-4-5", "model"),
        ("system_sha256", "other-system-hash", "system_prompt"),
        ("skills_sha256", "other-skills-hash", "skills"),
        ("environment_id", "env_other", "environment"),
        ("repo_url", "https://github.com/acme/other", "repo_url"),
        ("repo_branch", "topic", "repo_branch"),
        ("memory_read_only", True, "memory_access"),
        ("memory_store_id", "memstore_other", "memory_store"),
        ("vault_id", "vault_other", "vault"),
    ],
)
def test_decision_replaces_session_when_one_identity_field_differs(
    field: str, value: str, reason: ChangeReason
) -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(),
        desired=make_snapshot(**{field: value}),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == ReplaceSession(reasons=(reason,)), (
        f"a change to {field} is only reachable by replacing the session, reported as {reason!r}"
    )


def test_identity_reasons_are_in_fixed_order_when_several_fields_differ() -> None:
    reasons = identity_change_reasons(
        make_snapshot(),
        make_snapshot(
            vault_id="vault_other",
            model_id="claude-haiku-4-5",
            repo_branch="topic",
            ma_agent_id="agent_other",
        ),
    )
    assert reasons == ("agent_identity", "model", "repo_branch", "vault"), (
        "identity reasons follow the declared order, not the order the fields were set"
    )


def test_decision_replaces_without_ops_when_identity_and_mutable_both_differ() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(vault_id="vault_1", env_resource_id="res_env_1"),
        desired=make_snapshot(
            vault_id="vault_1",
            model_id="claude-haiku-4-5",
            tools_sha256="other-tools-hash",
            env_sha256="other-env-hash",
        ),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == ReplaceSession(reasons=("model",)), (
        "an identity change short-circuits: the replacement carries the mutable changes anyway"
    )


def test_decision_replaces_env_file_when_env_hash_differs_and_swap_is_supported() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(env_resource_id="res_env_1", env_file_id="file_env_1"),
        desired=make_snapshot(env_sha256="other-env-hash"),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == UpdateInPlace(
        ops=(ReplaceEnvFile(old_resource_id="res_env_1", old_file_id="file_env_1"),),
        reasons=("env_file",),
    ), "the env swap carries the recorded handles, which is what delete-then-add needs"


def test_replace_env_file_op_defaults_to_the_dot_env_mount_path() -> None:
    op = ReplaceEnvFile(old_resource_id=None, old_file_id=None)
    assert op.mount_path == ".env", "the env file is mounted at .env unless a caller says otherwise"


def test_decision_replaces_session_when_env_differs_and_swap_is_unsupported() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(env_resource_id="res_env_1"),
        desired=make_snapshot(env_sha256="other-env-hash"),
        capabilities=MaCapabilities(env_replaceable_in_place=False),
        now=NOW,
    )
    assert decision == ReplaceSession(reasons=("env_file",)), (
        "without an in-place swap, new credentials are only reachable by a new session"
    )


def test_decision_refreshes_env_when_recorded_hash_is_unknown_and_desired_is_set() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(env_sha256=None, env_resource_id="res_env_1"),
        desired=make_snapshot(env_sha256="env-hash"),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == UpdateInPlace(
        ops=(ReplaceEnvFile(old_resource_id="res_env_1", old_file_id=None),),
        reasons=("env_file",),
    ), "a row with no recorded env hash is unknown, not equal — it must be refreshed"


def test_decision_reuses_when_neither_recorded_nor_desired_has_an_env_file() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(env_sha256=None),
        desired=make_snapshot(env_sha256=None),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == ReuseAsIs(), "no env file on either side is not a change"


@pytest.mark.parametrize(
    ("overrides", "expected_reasons"),
    [
        ({"tools_sha256": "other-tools-hash"}, ("tools",)),
        ({"mcp_servers_sha256": "other-mcp-hash"}, ("mcp_servers",)),
        (
            {"tools_sha256": "other-tools-hash", "mcp_servers_sha256": "other-mcp-hash"},
            ("tools", "mcp_servers"),
        ),
    ],
)
def test_decision_updates_agent_arrays_when_tools_or_mcp_servers_differ(
    overrides: dict[str, str], expected_reasons: tuple[ChangeReason, ...]
) -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(),
        desired=make_snapshot(**overrides),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == UpdateInPlace(
        ops=(ReplaceToolsAndMcpServers(),), reasons=expected_reasons
    ), "tools and mcp_servers are sent as one update, with a reason per changed array"


def test_decision_replaces_session_when_tools_differ_and_agent_update_is_unsupported() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(),
        desired=make_snapshot(tools_sha256="other-tools-hash"),
        capabilities=MaCapabilities(session_agent_updatable=False),
        now=NOW,
    )
    assert decision == ReplaceSession(reasons=("tools",)), (
        "without sessions.update, a tool change needs a new session"
    )


@pytest.mark.parametrize("age_s", [0, 2399, 2400])
def test_decision_keeps_repo_token_when_it_is_no_older_than_the_threshold(age_s: int) -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(
            repo_resource_id="res_repo_1",
            repo_token_issued_at=int(NOW.timestamp()) - age_s,
        ),
        desired=make_snapshot(),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == ReuseAsIs(), f"a token {age_s}s old is still within the 2400s window"


def test_decision_rotates_repo_token_when_it_is_older_than_the_threshold() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(
            repo_resource_id="res_repo_1",
            repo_token_issued_at=int(NOW.timestamp()) - 2401,
        ),
        desired=make_snapshot(),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == UpdateInPlace(
        ops=(RotateRepoToken(resource_id="res_repo_1"),), reasons=("repo_token_age",)
    ), "an expired clone token is rotated on the resource that holds it"


def test_decision_ignores_token_age_when_no_repository_is_mounted() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(repo_resource_id=None, repo_token_issued_at=0),
        desired=make_snapshot(),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == ReuseAsIs(), "with no repo resource there is no token to rotate"


def test_decision_ignores_token_age_when_the_issue_time_was_never_recorded() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(repo_resource_id="res_repo_1", repo_token_issued_at=None),
        desired=make_snapshot(),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == ReuseAsIs(), "an unknown issue time is not evidence that the token expired"


def test_decision_replaces_session_when_token_is_stale_and_rotation_is_unsupported() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(
            repo_resource_id="res_repo_1",
            repo_token_issued_at=int(NOW.timestamp()) - 9000,
        ),
        desired=make_snapshot(),
        capabilities=MaCapabilities(repo_token_rotatable=False),
        now=NOW,
    )
    assert decision == ReplaceSession(reasons=("repo_token_age",)), (
        "a stale token that cannot be rotated leaves the session unable to reach the repo"
    )


def test_decision_appends_vault_remirror_last_when_a_vault_is_attached_and_ops_exist() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(vault_id="vault_1", env_resource_id="res_env_1"),
        desired=make_snapshot(vault_id="vault_1", tools_sha256="other-tools-hash"),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == UpdateInPlace(
        ops=(ReplaceToolsAndMcpServers(), RemirrorVaultCredentials()), reasons=("tools",)
    ), "the vault re-mirror rides along with the other ops and never adds a reason"


def test_decision_reuses_when_a_vault_is_attached_and_nothing_else_differs() -> None:
    decision = decide_session_compatibility(
        recorded=make_snapshot(vault_id="vault_1"),
        desired=make_snapshot(vault_id="vault_1"),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == ReuseAsIs(), (
        "an attached vault alone is not a change; the caller re-mirrors every turn as it does today"
    )


def test_ops_and_reasons_are_in_a_fixed_order_whatever_order_the_differences_were_set() -> None:
    recorded = make_snapshot(
        vault_id="vault_1",
        env_resource_id="res_env_1",
        env_file_id="file_env_1",
        repo_resource_id="res_repo_1",
        repo_token_issued_at=int(NOW.timestamp()) - 9000,
    )
    expected = UpdateInPlace(
        ops=(
            ReplaceEnvFile(old_resource_id="res_env_1", old_file_id="file_env_1"),
            ReplaceToolsAndMcpServers(),
            RotateRepoToken(resource_id="res_repo_1"),
            RemirrorVaultCredentials(),
        ),
        reasons=("env_file", "tools", "mcp_servers", "repo_token_age"),
    )
    first = decide_session_compatibility(
        recorded=recorded,
        desired=make_snapshot(
            vault_id="vault_1",
            env_sha256="other-env-hash",
            tools_sha256="other-tools-hash",
            mcp_servers_sha256="other-mcp-hash",
        ),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    second = decide_session_compatibility(
        recorded=recorded,
        desired=make_snapshot(
            mcp_servers_sha256="other-mcp-hash",
            tools_sha256="other-tools-hash",
            env_sha256="other-env-hash",
            vault_id="vault_1",
        ),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert first == expected, "ops and reasons follow the declared order, not the difference order"
    assert second == expected, "the same differences decide the same way whatever order they arrive"


def test_decision_raises_when_now_is_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        decide_session_compatibility(
            recorded=make_snapshot(),
            desired=make_snapshot(),
            capabilities=DEFAULT_MA_CAPABILITIES,
            now=datetime(2026, 9, 13, 12, 0, 0),
        )


def test_default_capabilities_match_the_probed_managed_agents_behaviour() -> None:
    assert (
        MaCapabilities(
            env_replaceable_in_place=True,
            session_agent_updatable=True,
            transcript_readable_after_archive=True,
            repo_token_rotatable=True,
        )
        == DEFAULT_MA_CAPABILITIES
    ), "the default capabilities are the probe results; flipping one degrades to replacement"


def test_token_only_app_session_is_reused_and_still_rotates() -> None:
    # No working repo: nothing mounted, so recorded and desired repo sets are both
    # empty and the session is reused; its vault tokens still refresh by age.
    recorded = make_snapshot(
        github_mode="app",
        repo_url=None,
        repo_branch=None,
        repo_urls=(),
        repo_resource_ids={},
        vault_id="session-vault",
        repo_token_issued_at=int(NOW.timestamp()),
    )
    desired = recorded.model_copy(update={"repo_token_issued_at": None})
    assert (
        decide_session_compatibility(
            recorded=recorded, desired=desired, capabilities=DEFAULT_MA_CAPABILITIES, now=NOW
        )
        == ReuseAsIs()
    )
    stale = recorded.model_copy(update={"repo_token_issued_at": int(NOW.timestamp()) - 2400})
    decision = decide_session_compatibility(
        recorded=stale, desired=desired, capabilities=DEFAULT_MA_CAPABILITIES, now=NOW
    )
    assert isinstance(decision, UpdateInPlace)
    assert RotateAppTokens(resource_ids={}) in decision.ops


def test_app_session_without_tokens_never_rotates() -> None:
    # Zero grants: no mounted repo and no issued tokens, so no rotation however old.
    recorded = make_snapshot(
        github_mode="app",
        repo_url=None,
        repo_branch=None,
        repo_urls=(),
        repo_resource_ids={},
        vault_id="session-vault",
        repo_token_issued_at=None,
    )
    assert (
        decide_session_compatibility(
            recorded=recorded, desired=recorded, capabilities=DEFAULT_MA_CAPABILITIES, now=NOW
        )
        == ReuseAsIs()
    )
