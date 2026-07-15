"""Unit tests for SyncRepoFailure DTO and sync_report_failures translation.

No DB, no I/O — pure construction and mapping assertions.
"""

from __future__ import annotations

from daimon.core.skill_sync import SyncRepoFailure, sync_report_failures
from daimon.core.skill_sync.orchestrator import SyncReport


def test_sync_repo_failure_constructs_and_round_trips() -> None:
    """SyncRepoFailure constructs with required fields and round-trips via model_dump."""
    failure = SyncRepoFailure(
        repo_url="https://github.com/o/r",
        reason="bad SKILL.md",
        phase="fetch",
    )
    assert failure.repo_url == "https://github.com/o/r", "repo_url must be stored as-is"
    assert failure.reason == "bad SKILL.md", "reason must be stored as-is"
    assert failure.phase == "fetch", "phase must be stored as-is"

    dumped = failure.model_dump()
    assert dumped["repo_url"] == "https://github.com/o/r", "round-trip via model_dump"
    assert dumped["reason"] == "bad SKILL.md", "round-trip via model_dump"
    assert dumped["phase"] == "fetch", "round-trip via model_dump"


def test_sync_report_failures_maps_skipped_repos_to_fetch_phase() -> None:
    """skipped_repos tuple → SyncRepoFailure with phase='fetch'."""
    report = SyncReport(
        skipped_repos=[("https://github.com/o/r", "fetch failed")],
    )
    failures = sync_report_failures(report)
    assert len(failures) == 1, "one skipped_repo must produce one SyncRepoFailure"
    assert failures[0].repo_url == "https://github.com/o/r", (
        "repo_url must match the skipped_repos tuple's first element"
    )
    assert failures[0].reason == "fetch failed", (
        "reason must match the skipped_repos tuple's second element"
    )
    assert failures[0].phase == "fetch", "phase must be 'fetch' for skipped_repos entries"


def test_sync_report_failures_maps_failed_uploads_to_upload_phase() -> None:
    """failed_uploads tuple → SyncRepoFailure with phase='upload'; skill name in repo_url.

    Pitfall 4: failed_uploads is keyed by skill_name, not repo URL.  The repo URL
    is not cleanly derivable from the skill name, so we carry the skill name in
    repo_url and the detail in reason rather than fabricating a URL.
    """
    report = SyncReport(
        failed_uploads=[("my-skill", "upload timeout")],
    )
    failures = sync_report_failures(report)
    assert len(failures) == 1, "one failed_upload must produce one SyncRepoFailure"
    assert failures[0].phase == "upload", "phase must be 'upload' for failed_uploads entries"
    # repo_url carries the skill name since the actual repo URL is not derivable.
    assert "my-skill" in failures[0].repo_url or "my-skill" in failures[0].reason, (
        "skill name must appear in either repo_url or reason for operator triage"
    )
    assert "upload timeout" in failures[0].reason, "failure reason must be preserved"


def test_sync_report_failures_returns_empty_list_for_clean_report() -> None:
    """A SyncReport with only counters produces no failures."""
    report = SyncReport(synced=5, updated=2, deleted=1)
    failures = sync_report_failures(report)
    assert failures == [], (
        "a clean report (no skipped_repos, no failed_uploads) must yield an empty list"
    )


def test_sync_report_failures_maps_attach_failures_to_attach_phase() -> None:
    """attach_failures tuple (agent_name, reason) → SyncRepoFailure with phase='attach'."""
    report = SyncReport(
        synced=3,
        attach_failures=[("replicate-agent", "skills: 26 exceeds maximum of 20")],
    )
    failures = sync_report_failures(report)
    assert len(failures) == 1, "one attach_failure must produce one SyncRepoFailure"
    assert failures[0].phase == "attach", "phase must be 'attach' for attach_failures entries"
    assert failures[0].repo_url == "replicate-agent", "repo_url carries the agent name (no repo)"
    assert "exceeds maximum" in failures[0].reason, "the MA cap message must be preserved"
