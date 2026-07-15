"""Pure builder for the MA `github_repository` session resource (dev-agent port).

This is the runtime consumer of `agent_repo_binding` that was never shipped: it
turns a persisted binding + a resolved PAT into the `github_repository` resource
that `create_session` mounts, so a bound repo actually clones into the agent's
workspace (verified live in spike 033).

Pure and I/O-free by design — `create_session` does the `get_binding` /
`get_pat` reads and feeds the results here.
"""

from __future__ import annotations

from anthropic.types.beta import BetaManagedAgentsGitHubRepositoryResourceParams
from daimon.core.stores.domain import AgentRepoBindingRow

__all__ = ["build_repo_resource"]


def build_repo_resource(
    binding: AgentRepoBindingRow | None,
    pat: str | None,
) -> BetaManagedAgentsGitHubRepositoryResourceParams | None:
    """Build the `github_repository` resource for a bound agent, or None.

    Returns None when the agent is unbound (``binding is None``) or has no
    resolvable PAT (``pat is None``) — both mean "nothing to clone", never an
    error.

    The binding store normalizes ``repo_url`` to canonical ``owner/repo``
    (``agent_repo_binding._normalize_owner_repo``), so the clonable URL is
    reconstructed as ``https://github.com/<owner/repo>`` to match the verified
    spike-033 shape.
    """
    if binding is None or pat is None:
        return None
    return {
        "type": "github_repository",
        "url": f"https://github.com/{binding.repo_url}",
        "authorization_token": pat,
        "checkout": {"type": "branch", "name": binding.default_branch},
    }
