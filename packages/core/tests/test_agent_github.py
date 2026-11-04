"""Pure assembly of the agent GitHub panel: release, repos, skills and resync results."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from anthropic.types.beta import SkillListResponse
from daimon.core.agent_github import (
    RepoSource,
    VersionTimeCache,
    panel_repos,
    panel_skills,
    parse_release,
    repo_sources,
    resync_results,
)
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.skill_sync.orchestrator import SyncReport
from daimon.core.stores.domain import UserSkillRow
from daimon.core.stores.github_access import AgentRepo
from daimon.testing import ma_agent

TENANT = uuid.UUID("12345678-0000-0000-0000-000000000000")
AGENT = uuid.uuid4()
USER = uuid.uuid4()
T0 = datetime(2030, 1, 1, tzinfo=UTC)
T1 = datetime(2030, 1, 2, tzinfo=UTC)
URL = "https://github.com/acme/skills"


def _repo(name: str, *, working: bool = False, installation: int | None = 1) -> AgentRepo:
    return AgentRepo(
        repo_id=hash(name) & 0xFFFF,
        full_name=name,
        scope="agent",
        max_access="write",
        baseline_access="none",
        ceiling_access="write",
        status="active",
        added_by_account_id=None,
        added_at=T0,
        granted_by_account_id=USER,
        granted_at=T0,
        is_working_repo=working,
        installation_id=installation,
    )


def _row(
    name: str,
    skill_id: str | None,
    *,
    principal: uuid.UUID = AGENT,
    source: str = "repo",
    content: str = "h1",
    path: str = "skills",
) -> UserSkillRow:
    return UserSkillRow.model_validate(
        {
            "tenant_id": TENANT,
            "principal_id": principal,
            "agent_name": "helper",
            "name": name,
            "source_repo_url": URL if source == "repo" else "",
            "source_repo_branch": "main" if source == "repo" else "",
            "source_path": path if source == "repo" else "",
            "content_hash": content,
            "anthropic_id": skill_id,
            "anthropic_latest_version": "1",
            "source": source,
            "updated_at": T1,
        }
    )


def _listed(skill_id: str, title: str | None, latest: str = "3") -> SkillListResponse:
    return SkillListResponse(
        id=skill_id,
        created_at="2030-01-01T00:00:00Z",
        display_title=title,
        latest_version=latest,
        source="custom",
        type="skill",
        updated_at="2030-01-03T00:00:00Z",
    )


def test_parse_release_when_all_keys_valid_then_returns_release() -> None:
    """A stamped release reads back with its time and https page."""
    release = parse_release(
        {
            "source_release": " v1.2 ",
            "source_release_at": "2030-01-01T10:00:00+00:00",
            "source_release_url": "https://example.com/releases/v1.2",
        }
    )
    assert release is not None, "a named release should parse"
    assert release.name == "v1.2", "the name should be stripped"
    assert release.at == datetime(2030, 1, 1, 10, tzinfo=UTC), "the time should parse"
    assert release.url == "https://example.com/releases/v1.2", "an https url should be kept"


def test_parse_release_when_time_naive_or_url_unsafe_then_drops_those_fields() -> None:
    """A time without an offset and a non-https url read as unset."""
    release = parse_release(
        {
            "source_release": "v2",
            "source_release_at": "2030-01-01T10:00:00",
            "source_release_url": "javascript:alert(1)",
        }
    )
    assert release is not None, "the name alone should still parse"
    assert release.at is None, "a naive time should be dropped"
    assert release.url is None, "a non-https url should be dropped"


def test_parse_release_when_name_missing_then_none() -> None:
    """No release name means no release, whatever else is set."""
    assert parse_release({"source_release_url": "https://example.com"}) is None, (
        "a release needs a name"
    )
    assert parse_release({"source_release": "  "}) is None, "a blank name is no release"


def test_panel_repos_when_mixed_then_working_first_then_by_name() -> None:
    """The working repo leads; the rest sort by name, ignoring case."""
    repos = panel_repos(
        [_repo("acme/zeta"), _repo("Acme/beta"), _repo("acme/work", working=True)],
        {1: "active"},
        {USER: "Ada"},
    )
    assert [r.name for r in repos] == ["acme/work", "Acme/beta", "acme/zeta"], "order"
    assert repos[0].url == "https://github.com/acme/work", "url should point at github"
    assert repos[0].granted_by == "Ada", "the granter should be named"


def test_panel_repos_when_installation_unknown_then_removed() -> None:
    """A repo whose installation record is gone or unset reads as removed."""
    repos = panel_repos(
        [_repo("acme/a", installation=9), _repo("acme/b", installation=None)],
        {1: "suspended"},
        {},
    )
    assert [r.installation for r in repos] == ["removed", "removed"], "unknown is removed"
    assert repos[0].granted_by is None, "an unknown granter should be None"


def test_panel_skills_when_each_origin_then_labels_and_sorts_by_title() -> None:
    """Repo, library, external and Anthropic skills each get their origin."""
    agent = ma_agent(
        skills=[
            {"type": "custom", "skill_id": "skill_repo", "version": "latest"},
            {"type": "custom", "skill_id": "skill_lib", "version": "2"},
            {"type": "custom", "skill_id": "skill_ext", "version": "latest"},
            {"type": "anthropic", "skill_id": "xlsx", "version": "latest"},
        ]
    )
    listing = [
        _listed("skill_repo", tenant_scoped_display_title(tenant_id=TENANT, name="deploy")),
        _listed(
            "skill_lib",
            tenant_scoped_display_title(tenant_id=TENANT, name="notes", agent_name="lib"),
        ),
        _listed("skill_ext", "Outside tool"),
    ]
    skills = panel_skills(
        agent,
        tenant_id=TENANT,
        agent_id=AGENT,
        rows=[_row("deploy", "skill_repo")],
        listing=listing,
        version_times={("skill_lib", "2"): T0},
    )
    by_id = {s.skill_id: s for s in skills}
    assert [s.title for s in skills] == ["deploy", "notes", "Outside tool", "xlsx"], "by title"
    assert by_id["skill_repo"].origin == "repo", "a ledger repo row is a repo skill"
    assert by_id["skill_repo"].repo == "acme/skills", "repo skills carry their repo"
    assert by_id["skill_repo"].version == "3", "an unpinned skill shows the latest version"
    assert by_id["skill_repo"].updated_at == T1, "repo skills show the ledger's update"
    assert by_id["skill_lib"].origin == "library", "a tenant-prefixed title is library"
    assert by_id["skill_lib"].version == "2", "a pin wins over the latest"
    assert by_id["skill_lib"].updated_at == T0, "a pin behind latest uses its version time"
    assert by_id["skill_ext"].origin == "external", "an unknown title is external"
    assert by_id["xlsx"].origin == "built_in", "built-in skills keep their origin"


def test_panel_skills_when_listing_empty_then_degrades_to_ids() -> None:
    """With no provider listing, unknown skills fall back to their id and no version."""
    agent = ma_agent(skills=[{"type": "custom", "skill_id": "skill_x", "version": "latest"}])
    skills = panel_skills(
        agent, tenant_id=TENANT, agent_id=AGENT, rows=[], listing=[], version_times={}
    )
    assert len(skills) == 1, "the attached skill should still show"
    assert skills[0].title == "skill_x", "the title should fall back to the id"
    assert skills[0].version is None, "no listing means no version"
    assert skills[0].origin == "external", "nothing known means external"


def test_panel_skills_when_user_and_agent_rows_then_agent_row_wins() -> None:
    """An older user-keyed ledger row yields to the agent-keyed one."""
    agent = ma_agent(skills=[{"type": "custom", "skill_id": "skill_a", "version": "latest"}])
    rows = [_row("agent-name", "skill_a"), _row("user-name", "skill_a", principal=USER)]
    skills = panel_skills(
        agent, tenant_id=TENANT, agent_id=AGENT, rows=rows, listing=[], version_times={}
    )
    assert skills[0].title == "agent-name", "the agent-keyed row should win"


def test_repo_sources_when_attached_repo_skills_then_distinct_sources() -> None:
    """Only attached repo skills count, once per repo, branch and path."""
    agent = ma_agent(
        skills=[
            {"type": "custom", "skill_id": "s1", "version": "latest"},
            {"type": "custom", "skill_id": "s2", "version": "latest"},
            {"type": "custom", "skill_id": "s3", "version": "latest"},
        ]
    )
    rows = [
        _row("a", "s1"),
        _row("b", "s2"),
        _row("c", "s3", source="upload"),
        _row("d", "s4", path="other"),
    ]
    sources = repo_sources(agent, rows, AGENT)
    assert [(s.repo, s.path) for s in sources] == [("acme/skills", "skills")], "one source"
    assert sources[0].principal_id == AGENT, "the source keeps its ledger principal"


def test_resync_results_when_ledger_changes_then_names_each_action() -> None:
    """Created, updated, unchanged, removed and failed skills each get a result."""
    source = RepoSource(
        repo="acme/skills", url=URL, branch="main", path="skills", principal_id=AGENT
    )
    before = [_row("same", "s1"), _row("changed", "s2"), _row("gone", "s3")]
    after = [_row("same", "s1"), _row("changed", "s2", content="h2"), _row("fresh", "s4")]
    report = SyncReport(failed_uploads=[("broken", "too big")], attach_failures=[("helper", "x")])
    results = {
        (r.name, r.action, r.detail)
        for r in resync_results(source, before, after, report, agent_id=AGENT)
    }
    assert ("fresh", "created", None) in results, "a new name is created"
    assert ("changed", "updated", None) in results, "a changed hash is updated"
    assert ("same", "skipped", "Unchanged.") in results, "an unchanged skill is skipped"
    assert ("gone", "skipped", "Removed from the repo.") in results, "a removed skill"
    assert ("broken", "failed", "too big") in results, "a failed upload keeps its reason"
    assert (
        "acme/skills",
        "failed",
        "The skills could not be added to the agent.",
    ) in results, "an attach failure is named by repo"


def test_resync_results_when_repo_skipped_then_repo_failed() -> None:
    """A repo the sync could not read is one failed result."""
    source = RepoSource(
        repo="acme/skills", url=URL, branch="main", path="skills", principal_id=AGENT
    )
    report = SyncReport(skipped_repos=[(URL, "not found")])
    results = resync_results(source, [], [], report, agent_id=AGENT)
    assert [(r.name, r.action) for r in results] == [("acme/skills", "failed")], "repo failed"


def test_version_time_cache_when_fresh_expired_or_full_then_hits_misses_or_evicts() -> None:
    """A stored time (or a stored unknown) is a hit until it expires; the oldest goes when full."""
    clock = [T0]
    cache = VersionTimeCache(now=lambda: clock[0], ttl=timedelta(minutes=5), cap=2)
    cache.put((TENANT, "skill_a", "1"), T1)
    cache.put((TENANT, "skill_b", "1"), None)
    assert cache.get((TENANT, "skill_a", "1")) == (True, T1), "a fresh time is a hit"
    assert cache.get((TENANT, "skill_b", "1")) == (True, None), "a known unknown is a hit"
    assert cache.get((uuid.uuid4(), "skill_a", "1")) == (False, None), "keyed per workspace"
    cache.put((TENANT, "skill_c", "1"), T1)
    assert cache.get((TENANT, "skill_a", "1"))[0] is False, "the oldest entry goes when full"
    clock[0] = T0 + timedelta(minutes=5)
    assert cache.get((TENANT, "skill_c", "1")) == (False, None), "an expired entry is a miss"
