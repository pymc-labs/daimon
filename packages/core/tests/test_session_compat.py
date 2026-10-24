"""Compatibility decisions for a live session whose configuration drifted."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from daimon.core.github_app_auth import PermissionProfile, group_repository_access
from daimon.core.github_app_session import (
    AppToken,
    _working_alias,  # pyright: ignore[reportPrivateUsage]
    token_scopes_for_groups,
)
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
    UpdateInPlace,
    decide_session_compatibility,
    identity_change_reasons,
)
from daimon.core.session_snapshot import SessionSnapshot, fingerprint_identity

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


def test_single_installation_fingerprint_matches_pre_m1_golden() -> None:
    snapshot = make_snapshot(
        github_mode="app",
        repo_url=None,
        repo_branch=None,
        repo_urls=("https://github.com/example/one",),
        token_repo_urls=("https://github.com/example/one",),
    )
    assert snapshot.token_scopes == ()
    assert fingerprint_identity(snapshot) == (
        "8c98253a6a1f0b5cdf65465b1c52e7d069f80a12774ec295cf0ce8a81149e623"
    )
    assert "token_scopes" not in snapshot.model_dump(mode="json")
    assert "github_repo_map_sha256" not in snapshot.model_dump(mode="json")
    assert "github_repo_script_sha256" not in snapshot.model_dump(mode="json")


def test_helper_map_changes_replace_a_multi_installation_session() -> None:
    recorded = make_snapshot(
        github_mode="app",
        repo_url=None,
        repo_branch=None,
        token_scopes=((1, (101,)), (2, (201,))),
        github_repo_map_sha256="old-map",
        github_repo_script_sha256="script-v1",
        repo_token_issued_at=int(NOW.timestamp()),
    )
    moved = recorded.model_copy(
        update={
            "token_scopes": ((1, (101, 201)),),
            "github_repo_map_sha256": "new-map",
        }
    )
    assert identity_change_reasons(recorded, moved) == ("repo_set", "github_helper")
    assert isinstance(
        decide_session_compatibility(
            recorded=recorded, desired=moved, capabilities=DEFAULT_MA_CAPABILITIES, now=NOW
        ),
        ReplaceSession,
    )


@pytest.mark.parametrize(
    ("grants", "working_url", "token_urls", "golden"),
    [
        (
            [(71, 101, "read"), (71, 102, "write")],
            "https://github.com/example/write",
            ("https://github.com/example/read", "https://github.com/example/write"),
            "829f05dab0445d71bede9ebfb264d6ca8ec2e6ab39899c4132419c864f8c5737",
        ),
        (
            [(71, repo_id, "write") for repo_id in range(1, 502)],
            "https://github.com/example/repo-001",
            tuple(f"https://github.com/example/repo-{repo_id:03d}" for repo_id in range(1, 502)),
            "834982d92e98d9083755e3d5a5685a5e5406356b497ee226ed1a1b9cc72ce609",
        ),
    ],
)
def test_single_installation_multi_group_fingerprint_and_compat_stay_legacy(
    grants: list[tuple[int, int, PermissionProfile]],
    working_url: str,
    token_urls: tuple[str, ...],
    golden: str,
) -> None:
    groups = group_repository_access(grants)
    assert len(groups) > 1
    assert token_scopes_for_groups(groups) == ()
    snapshot = make_snapshot(
        github_mode="app",
        repo_url=None,
        repo_branch=None,
        repo_urls=(working_url,),
        token_repo_urls=token_urls,
        token_scopes=token_scopes_for_groups(groups),
        repo_token_issued_at=int(NOW.timestamp()),
    )
    assert fingerprint_identity(snapshot) == golden
    assert "token_scopes" not in snapshot.model_dump(mode="json")
    assert (
        decide_session_compatibility(
            recorded=snapshot,
            desired=snapshot,
            capabilities=DEFAULT_MA_CAPABILITIES,
            now=NOW,
        )
        == ReuseAsIs()
    )


def test_single_installation_chunking_keeps_main_alias_for_out_of_order_grants() -> None:
    import uuid

    groups = group_repository_access([(71, repo_id, "write") for repo_id in (501, *range(1, 501))])
    assert groups == [
        (71, "write", (501, *range(1, 500))),
        (71, "write", (500,)),
    ]
    groups.sort(key=lambda group: (group[0], group[1], group[2]))
    assert groups[0][2] == (500,)  # main's first-token fallback when no repo is working
    tokens = tuple(
        AppToken(uuid.uuid4(), f"token-{index}", installation_id, ids, profile, "GH_TOKEN")
        for index, (installation_id, profile, ids) in enumerate(groups)
    )
    assert _working_alias(tokens, None, multi_installation=False) == "token-0"
    assert token_scopes_for_groups(groups) == ()


def test_reassigning_a_repo_between_installations_changes_identity() -> None:
    recorded = make_snapshot(
        github_mode="app",
        repo_urls=("https://github.com/example/a", "https://github.com/example/b"),
        token_repo_urls=("https://github.com/example/a", "https://github.com/example/b"),
        token_scopes=((1, (101,)), (2, (102,))),
    )
    reassigned = recorded.model_copy(update={"token_scopes": ((2, (101, 102)),)})
    assert identity_change_reasons(recorded, reassigned) == ("repo_set",)
    assert fingerprint_identity(recorded) != fingerprint_identity(reassigned)


def test_only_aged_installation_rotates() -> None:
    recorded = make_snapshot(
        github_mode="app",
        repo_urls=("https://github.com/example/a", "https://github.com/example/b"),
        token_repo_urls=("https://github.com/example/a", "https://github.com/example/b"),
        token_scopes=((1, (101,)), (2, (102,))),
        token_issued_at_by_installation={
            1: int(NOW.timestamp()) - 2400,
            2: int(NOW.timestamp()),
        },
        vault_id="session-vault",
    )
    decision = decide_session_compatibility(
        recorded=recorded,
        desired=recorded,
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == UpdateInPlace(
        ops=(RotateAppTokens(resource_ids={}, installation_ids=(1,)), RemirrorVaultCredentials()),
        reasons=("repo_token_age",),
    )
    fresh = recorded.model_copy(
        update={
            "token_issued_at_by_installation": {1: int(NOW.timestamp()), 2: int(NOW.timestamp())}
        }
    )
    forced = decide_session_compatibility(
        recorded=fresh,
        desired=fresh,
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
        force_app_token_rotation=True,
        forced_app_installation_ids=(2,),
    )
    assert forced == UpdateInPlace(
        ops=(RotateAppTokens(resource_ids={}, installation_ids=(2,)), RemirrorVaultCredentials()),
        reasons=("repo_token_age",),
    )


def test_any_aged_chunk_rotates_its_installation_even_when_other_chunks_are_fresh() -> None:
    fresh = int(NOW.timestamp())
    recorded = make_snapshot(
        github_mode="app",
        repo_urls=("https://github.com/example/a",),
        token_repo_urls=(
            "https://github.com/example/a",
            "https://github.com/example/a2",
            "https://github.com/example/b",
        ),
        token_scopes=(
            (1, 0, (101,), fresh, "token-a0"),
            (1, 1, (102,), fresh - 2400, "token-a1"),
            (2, 0, (201,), fresh, "token-b0"),
        ),
        token_issued_at_by_installation={1: fresh, 2: fresh},
        vault_id="session-vault",
    )
    assert decide_session_compatibility(
        recorded=recorded,
        desired=recorded,
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    ) == UpdateInPlace(
        ops=(RotateAppTokens(resource_ids={}, installation_ids=(1,)), RemirrorVaultCredentials()),
        reasons=("repo_token_age",),
    )
    assert fingerprint_identity(recorded) == fingerprint_identity(
        recorded.model_copy(
            update={
                "token_scopes": (
                    (1, 0, (101,), fresh + 1, "new-a0"),
                    (1, 1, (102,), fresh + 1, "new-a1"),
                    (2, 0, (201,), fresh + 1, "new-b0"),
                )
            }
        )
    )


def test_new_unmounted_repo_refreshes_only_its_installation() -> None:
    a = "https://github.com/example/a"
    b = "https://github.com/example/b"
    new_b = "https://github.com/example/new-b"
    recorded = make_snapshot(
        github_mode="app",
        repo_urls=(a,),
        token_repo_urls=(a, b),
        token_scopes=((1, (101,)), (2, (102,))),
        token_issued_at_by_installation={1: int(NOW.timestamp()), 2: int(NOW.timestamp())},
        vault_id="session-vault",
    )
    desired = recorded.model_copy(
        update={
            "token_repo_urls": (a, b, new_b),
            "token_scopes": ((1, (101,)), (2, (102, 103))),
        }
    )
    assert decide_session_compatibility(
        recorded=recorded,
        desired=desired,
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    ) == UpdateInPlace(
        ops=(RotateAppTokens(resource_ids={}, installation_ids=(2,)), RemirrorVaultCredentials()),
        reasons=("repo_set",),
    )


def test_scope_expansion_and_other_installation_refresh_use_one_rotation() -> None:
    a = "https://github.com/example/a"
    b = "https://github.com/example/b"
    recorded = make_snapshot(
        github_mode="app",
        repo_urls=(a,),
        token_repo_urls=(a, b),
        token_scopes=((1, (101,)), (2, (102,))),
        token_issued_at_by_installation={1: int(NOW.timestamp()), 2: int(NOW.timestamp())},
        vault_id="session-vault",
    )
    desired = recorded.model_copy(
        update={
            "token_repo_urls": (a, b, "https://github.com/example/new-b"),
            "token_scopes": ((1, (101,)), (2, (102, 103))),
        }
    )
    assert decide_session_compatibility(
        recorded=recorded,
        desired=desired,
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
        force_app_token_rotation=True,
        forced_app_installation_ids=(1,),
    ) == UpdateInPlace(
        ops=(RotateAppTokens(resource_ids={}, installation_ids=(1, 2)), RemirrorVaultCredentials()),
        reasons=("repo_set", "repo_token_age"),
    )


def test_removed_unmounted_installation_leaves_other_token_current() -> None:
    a = "https://github.com/example/a"
    b = "https://github.com/example/b"
    recorded = make_snapshot(
        github_mode="app",
        repo_urls=(b,),
        token_repo_urls=(a, b),
        token_scopes=((1, (101,)), (2, (102,))),
        token_issued_at_by_installation={1: int(NOW.timestamp()), 2: int(NOW.timestamp())},
        vault_id="session-vault",
    )
    desired = recorded.model_copy(update={"token_repo_urls": (b,), "token_scopes": ()})
    assert decide_session_compatibility(
        recorded=recorded,
        desired=desired,
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
        force_app_token_rotation=True,
        forced_app_installation_ids=(1,),
    ) == UpdateInPlace(
        ops=(RotateAppTokens(resource_ids={}, installation_ids=()), RemirrorVaultCredentials()),
        reasons=("repo_set",),
    )


def test_adding_second_installation_refreshes_all_tokens_into_multi_mode() -> None:
    a = "https://github.com/example/a"
    b = "https://github.com/example/b"
    recorded = make_snapshot(
        github_mode="app",
        repo_urls=(b,),
        token_repo_urls=(b,),
        repo_token_issued_at=int(NOW.timestamp()),
        vault_id="session-vault",
    )
    desired = recorded.model_copy(
        update={"token_repo_urls": (a, b), "token_scopes": ((1, (101,)), (2, (102,)))}
    )
    assert decide_session_compatibility(
        recorded=recorded,
        desired=desired,
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    ) == UpdateInPlace(
        ops=(RotateAppTokens(resource_ids={}, installation_ids=()), RemirrorVaultCredentials()),
        reasons=("repo_set",),
    )


def test_decision_is_reuse_when_recorded_and_desired_are_identical() -> None:
    recorded = make_snapshot()
    decision = decide_session_compatibility(
        recorded=recorded,
        desired=make_snapshot(),
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
    )
    assert decision == ReuseAsIs(), "identical snapshots must reuse the session untouched"


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
    assert decide_session_compatibility(
        recorded=fresh,
        desired=desired,
        capabilities=DEFAULT_MA_CAPABILITIES,
        now=NOW,
        force_app_token_rotation=True,
    ) == UpdateInPlace(
        ops=(
            RotateAppTokens(resource_ids={url: "repo-resource"}),
            RemirrorVaultCredentials(),
        ),
        reasons=("repo_token_age",),
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


def test_new_token_only_grant_replaces_the_session() -> None:
    # No working repo is mounted, but a new grant must still reach the session:
    # its token can only arrive with a fresh session.
    url = "https://github.com/example/new"
    recorded = make_snapshot(
        github_mode="app",
        repo_url=None,
        repo_branch=None,
        repo_urls=(),
        token_repo_urls=(),
        vault_id="session-vault",
    )
    desired = recorded.model_copy(update={"token_repo_urls": (url,)})
    decision = decide_session_compatibility(
        recorded=recorded, desired=desired, capabilities=DEFAULT_MA_CAPABILITIES, now=NOW
    )
    assert decision == ReplaceSession(reasons=("repo_set",))


def test_removed_token_only_grant_replaces_the_session() -> None:
    url = "https://github.com/example/removed"
    recorded = make_snapshot(
        github_mode="app",
        repo_url=None,
        repo_branch=None,
        repo_urls=(),
        token_repo_urls=(url,),
        vault_id="session-vault",
    )
    desired = recorded.model_copy(update={"token_repo_urls": ()})
    assert decide_session_compatibility(
        recorded=recorded, desired=desired, capabilities=DEFAULT_MA_CAPABILITIES, now=NOW
    ) == ReplaceSession(reasons=("repo_set",))


def test_a_live_session_is_not_replaced_only_to_add_the_callers_personal_store() -> None:
    """Every session from before personal memory existed recorded none. The
    caller's store appearing later must not end those conversations: the next
    new session mounts it instead."""
    recorded = SessionSnapshot.model_validate(
        make_snapshot().model_dump(exclude={"personal_memory_store_id"})
    )
    desired = make_snapshot(personal_memory_store_id="memstore_alice")

    assert recorded.personal_memory_store_id is None
    assert identity_change_reasons(recorded, desired) == ()
    assert (
        decide_session_compatibility(
            recorded=recorded, desired=desired, capabilities=DEFAULT_MA_CAPABILITIES, now=NOW
        )
        == ReuseAsIs()
    )


@pytest.mark.parametrize("desired_store", ["memstore_other", None])
def test_a_session_whose_personal_store_changed_or_went_away_is_replaced(
    desired_store: str | None,
) -> None:
    recorded = make_snapshot(personal_memory_store_id="memstore_alice")
    desired = make_snapshot(personal_memory_store_id=desired_store)

    assert decide_session_compatibility(
        recorded=recorded, desired=desired, capabilities=DEFAULT_MA_CAPABILITIES, now=NOW
    ) == ReplaceSession(reasons=("personal_memory_store",))


def test_an_unchanged_personal_store_reuses_the_session() -> None:
    snapshot = make_snapshot(personal_memory_store_id="memstore_alice")
    assert (
        decide_session_compatibility(
            recorded=snapshot, desired=snapshot, capabilities=DEFAULT_MA_CAPABILITIES, now=NOW
        )
        == ReuseAsIs()
    )


def test_a_difference_in_the_mounted_credentials_hold_replaces_the_session() -> None:
    unheld = make_snapshot()
    held = make_snapshot(mcp_hold="slack:C-SYN-1:client")
    other = make_snapshot(mcp_hold="slack:C-SYN-2:client")
    capabilities = DEFAULT_MA_CAPABILITIES

    for recorded, desired in ((unheld, held), (held, unheld), (held, other)):
        decision = decide_session_compatibility(
            recorded=recorded, desired=desired, capabilities=capabilities, now=NOW
        )
        assert isinstance(decision, ReplaceSession)
        assert decision.reasons == ("credential_hold",)
    assert isinstance(
        decide_session_compatibility(
            recorded=held, desired=held, capabilities=capabilities, now=NOW
        ),
        ReuseAsIs,
    )


def test_an_unheld_snapshot_keeps_the_form_and_fingerprint_it_was_recorded_with() -> None:
    unheld = make_snapshot()

    assert "mcp_hold" not in unheld.model_dump(mode="json")
    assert fingerprint_identity(unheld) != fingerprint_identity(
        make_snapshot(mcp_hold="slack:C-SYN-1:client")
    )
