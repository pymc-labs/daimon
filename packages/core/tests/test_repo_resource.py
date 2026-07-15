"""Unit tests for the pure `build_repo_resource` builder (Task 1, dev-agent port).

The binding store normalizes `repo_url` to canonical `owner/repo` (see
`agent_repo_binding._normalize_owner_repo`), so the builder must reconstruct a
clonable `https://github.com/<owner/repo>` URL — matching the verified spike-033
resource shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from daimon.core.repo_resource import build_repo_resource
from daimon.core.stores.domain import AgentRepoBindingRow


def _binding(
    *,
    repo_url: str = "example-org/example-repo",
    default_branch: str = "main",
    ma_secret_ref: str = "anon:",
) -> AgentRepoBindingRow:
    now = datetime(2026, 6, 2, tzinfo=UTC)
    return AgentRepoBindingRow(
        tenant_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        agent_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        repo_url=repo_url,
        default_branch=default_branch,
        ma_secret_ref=ma_secret_ref,
        created_at=now,
        updated_at=now,
    )


def test_build_repo_resource_returns_github_repository_shape() -> None:
    resource = build_repo_resource(_binding(), "ghp_realtoken")
    assert resource == {
        "type": "github_repository",
        "url": "https://github.com/example-org/example-repo",
        "authorization_token": "ghp_realtoken",
        "checkout": {"type": "branch", "name": "main"},
    }


def test_build_repo_resource_reconstructs_url_from_normalized_owner_repo() -> None:
    # Binding stores canonical owner/repo, never a full URL — builder must prefix.
    resource = build_repo_resource(_binding(repo_url="octocat/hello-world"), "tok")
    assert resource is not None
    assert resource["url"] == "https://github.com/octocat/hello-world"


def test_build_repo_resource_honors_default_branch() -> None:
    resource = build_repo_resource(_binding(default_branch="develop"), "tok")
    assert resource is not None
    assert resource.get("checkout") == {"type": "branch", "name": "develop"}


def test_build_repo_resource_none_when_pat_is_none() -> None:
    assert build_repo_resource(_binding(), None) is None


def test_build_repo_resource_none_when_binding_is_none() -> None:
    assert build_repo_resource(None, "ghp_realtoken") is None
