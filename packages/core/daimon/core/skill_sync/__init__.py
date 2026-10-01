"""Skill sync subsystem.

Multi-repo, PAT-authenticated, content-hash-deduped skill upload pipeline.
Public entrypoint (Wave 3): `sync_agent_skills`.

Co-exists with `daimon.core.skills` (existing single-URL ad-hoc sync).
The plural-vs-singular distinction is intentional:
  - `daimon.core.skills`     — single URL, no PAT, no DB tracking
  - `daimon.core.skill_sync` — multi-repo, PAT-auth, dedup, orphan-delete
"""

from daimon.core.skill_sync.fetcher import (
    GitHubAuthError,
    GitHubTarballFetcher,
    GitHubUnreachable,
    PATMissingError,
)
from daimon.core.skill_sync.orchestrator import (
    SyncRepoFailure,
    SyncReport,
    sync_agent_skills,
    sync_report_failures,
)

__all__ = [
    "GitHubAuthError",
    "GitHubTarballFetcher",
    "GitHubUnreachable",
    "PATMissingError",
    "SyncReport",
    "SyncRepoFailure",
    "sync_agent_skills",
    "sync_report_failures",
]
