from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    SkillDeleteResponse,
    SkillListResponse,
)
from daimon.core.defaults.ma_index import _SKILLS_PAGE_LIMIT  # pyright: ignore[reportPrivateUsage]
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_TENANT,
    tenant_scoped_display_title,
)
from daimon.core.defaults.report import Action
from daimon.core.defaults.sweep import (
    sweep_removed_agents,
    sweep_removed_environments,
    sweep_removed_skills,
)
from daimon.core.errors import SkillsListTruncatedError
from daimon.testing.ma import EMPTY_CLOUD_CONFIG, MARouter, list_response
from daimon.testing.ma import build_fake_anthropic as build_fake_anthropic_http

TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_T8 = str(TENANT_ID)[:8]


def _tagged_agent(
    *, id_: str, name: str, managed: bool = True, skill_ids: list[str] | None = None
) -> dict[str, Any]:
    metadata: dict[str, str] = {
        MA_METADATA_KEY_TENANT: str(TENANT_ID),
        MA_METADATA_KEY_NAME: name,
    }
    if managed:
        metadata[MA_METADATA_KEY_MANAGED] = "true"
    skills = [{"type": "custom", "skill_id": sid, "version": "latest"} for sid in (skill_ids or [])]
    return BetaManagedAgentsAgent.model_validate(
        {
            "id": id_,
            "type": "agent",
            "name": name,
            "model": {"id": "claude-opus-4-7"},
            "metadata": metadata,
            "description": None,
            "archived_at": None,
            "created_at": "2026-04-21T00:00:00Z",
            "updated_at": "2026-04-21T00:00:00Z",
            "version": 1,
            "mcp_servers": [],
            "skills": skills,
            "tools": [],
            "system": None,
        }
    ).model_dump(mode="json")


def _tagged_env(*, id_: str, name: str, managed: bool = True) -> dict[str, Any]:
    metadata: dict[str, str] = {
        MA_METADATA_KEY_TENANT: str(TENANT_ID),
        MA_METADATA_KEY_NAME: name,
    }
    if managed:
        metadata[MA_METADATA_KEY_MANAGED] = "true"
    return BetaEnvironment(
        id=id_,
        type="environment",
        name=name,
        config=EMPTY_CLOUD_CONFIG,
        metadata=metadata,
        description="",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    ).model_dump(mode="json")


def _skill_row(*, id_: str, display_title: str, source: str = "custom") -> dict[str, Any]:
    return SkillListResponse(
        id=id_,
        type="skill",
        display_title=display_title,
        source=source,
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    ).model_dump(mode="json")


async def test_sweep_archives_removed_agents() -> None:
    archived: list[str] = []

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response(
            [_tagged_agent(id_="ag_1", name="gone"), _tagged_agent(id_="ag_2", name="kept")]
        ),
    )
    router.add(
        "POST",
        r"/v1/agents/ag_1/archive",
        lambda req, _m: (
            archived.append("ag_1")
            or httpx.Response(
                200,
                json=BetaManagedAgentsAgent(
                    id="ag_1",
                    type="agent",
                    name="gone",
                    model={"id": "claude-opus-4-7"},
                    metadata={},
                    description=None,
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                    version=1,
                    mcp_servers=[],
                    skills=[],
                    tools=[],
                    system=None,
                ).model_dump(mode="json"),
            )
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sweep_removed_agents(
        client, present_names={"kept"}, tenant_id=TENANT_ID, dry_run=False
    )
    assert [o.name for o in outcomes] == ["gone"]
    assert outcomes[0].action is Action.ARCHIVED
    assert archived == ["ag_1"], "agents.archive must have been called with ag_1"


async def test_sweep_preserves_user_fork_agents() -> None:
    """A user fork (created via `daimon agents fork`) carries no
    daimon_managed=true marker. The sweep must ignore it — otherwise every
    user fork is nuked on the next scheduler boot (smoke matrix #21 FAIL,
    reproduced 2026-05-21).
    """
    archived: list[str] = []

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response(
            [
                _tagged_agent(id_="ag_kept", name="daimon"),
                _tagged_agent(id_="ag_fork", name="my-fork", managed=False),
            ]
        ),
    )
    router.add(
        "POST",
        r"/v1/agents/ag_fork/archive",
        lambda req, _m: (
            archived.append("ag_fork")
            or httpx.Response(500, json={"error": "should not have been called"})
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sweep_removed_agents(
        client, present_names={"daimon"}, tenant_id=TENANT_ID, dry_run=False
    )
    assert outcomes == [], "sweep must skip user forks (no daimon_managed marker)"
    assert archived == [], "agents.archive must NOT have been called for the user fork"


async def test_sweep_preserves_user_fork_environments() -> None:
    """Parity with agents: a user-forked env without daimon_managed must
    survive the sweep — no archive (or delete) request reaches the fork.
    """
    mutated: list[str] = []

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments",
        lambda req, _m: list_response(
            [
                _tagged_env(id_="env_kept", name="default"),
                _tagged_env(id_="env_fork", name="my-env", managed=False),
            ]
        ),
    )
    router.add(
        "POST",
        r"/v1/environments/env_fork/archive",
        lambda req, _m: (
            mutated.append("env_fork")
            or httpx.Response(500, json={"error": "should not have been called"})
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sweep_removed_environments(
        client, present_names={"default"}, tenant_id=TENANT_ID, dry_run=False
    )
    assert outcomes == [], "sweep must skip user-forked envs"
    assert mutated == [], "environments.archive must NOT have been called for the fork"


async def test_sweep_archives_removed_environments() -> None:
    """A managed env absent from present_names is ARCHIVED, not hard-deleted (ENVC-01).
    Removing the YAML for an env must never physically destroy it on MA."""
    archived: list[str] = []

    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments",
        lambda req, _m: list_response(
            [_tagged_env(id_="env_1", name="gone"), _tagged_env(id_="env_2", name="kept")]
        ),
    )
    router.add(
        "POST",
        r"/v1/environments/env_1/archive",
        lambda req, _m: (
            archived.append("env_1")
            or httpx.Response(
                200,
                json=BetaEnvironment(
                    id="env_1",
                    type="environment",
                    name="gone",
                    config=EMPTY_CLOUD_CONFIG,
                    metadata={},
                    description="",
                    created_at="2026-04-21T00:00:00Z",
                    updated_at="2026-04-21T00:00:00Z",
                ).model_dump(mode="json"),
            )
        ),
    )
    # Fail loudly if a hard-delete is ever attempted.
    router.add(
        "DELETE",
        r"/v1/environments/env_1",
        lambda req, _m: httpx.Response(500, json={"error": "delete must not be called"}),
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sweep_removed_environments(
        client, present_names={"kept"}, tenant_id=TENANT_ID, dry_run=False
    )
    assert [o.name for o in outcomes] == ["gone"]
    assert outcomes[0].action is Action.ARCHIVED, (
        "removed env outcome must be ARCHIVED, not DELETED"
    )
    assert archived == ["env_1"], "environments.archive must have been called with env_1"


async def test_sweep_dry_run_environments() -> None:
    """dry_run sweep reports Action.ARCHIVED and issues no archive/delete request."""
    router = MARouter()
    router.add(
        "GET",
        r"/v1/environments",
        lambda req, _m: list_response([_tagged_env(id_="env_1", name="gone")]),
    )
    # No archive/delete handler — MARouter raises if either is called.
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sweep_removed_environments(
        client, present_names=set(), tenant_id=TENANT_ID, dry_run=True
    )
    assert [o.name for o in outcomes] == ["gone"]
    assert outcomes[0].action is Action.ARCHIVED, "dry_run env outcome must be ARCHIVED"


async def test_sweep_deletes_removed_skills() -> None:
    """Own-tenant skill not in present_names and unreferenced must be deleted.

    present_names carries canonical (tenant-prefixed) display titles.
    The 'kept' skill is spared because its prefixed title appears in present_names.
    The 'gone' skill is deleted because it is not in present_names and unreferenced.
    """
    deleted: list[str] = []

    gone_title = tenant_scoped_display_title(tenant_id=TENANT_ID, name="gone")
    kept_title = tenant_scoped_display_title(tenant_id=TENANT_ID, name="kept")

    router = MARouter()
    # No agent references sk_1, so the reference-guard does not spare it.
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_tagged_agent(id_="ag_1", name="daimon")]),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response(
            [
                _skill_row(id_="sk_1", display_title=gone_title),
                _skill_row(id_="sk_2", display_title=kept_title),
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/skills/sk_1/versions",
        lambda req, _m: httpx.Response(200, json={"data": [], "next_page": None}),
    )
    router.add(
        "DELETE",
        r"/v1/skills/sk_1",
        lambda req, _m: (
            deleted.append("sk_1")
            or httpx.Response(
                200,
                json=SkillDeleteResponse(
                    id="sk_1",
                    type="skill_deleted",
                ).model_dump(mode="json"),
            )
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sweep_removed_skills(
        client, present_names={kept_title}, tenant_id=TENANT_ID, dry_run=False
    )
    assert [o.name for o in outcomes] == [gone_title], (
        "sweep must delete the removed canonical skill"
    )
    assert outcomes[0].action is Action.DELETED
    assert deleted == ["sk_1"], "skills.delete must have been called with sk_1"


async def test_sweep_skips_non_daimon_skills() -> None:
    """Skills with source='anthropic' are never sweep candidates."""
    other_skill = SkillListResponse(
        id="sk_other",
        type="skill",
        display_title="other-tool",
        source="anthropic",
        created_at="2026-04-21T00:00:00Z",
        updated_at="2026-04-21T00:00:00Z",
    ).model_dump(mode="json")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_tagged_agent(id_="ag_1", name="daimon")]),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response([other_skill]),
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sweep_removed_skills(
        client, present_names=set(), tenant_id=TENANT_ID, dry_run=False
    )
    assert outcomes == [], "non-daimon-system skills must not be swept"


async def test_sweep_spares_skill_pinned_by_live_agent() -> None:
    """An own-tenant skill not in present_names but still pinned by an agent must be spared.

    The reference-guard (list_referenced_skill_ids) is the second belt: even if
    a skill passes the prefix filter and is absent from present_names, we do NOT
    delete it while any non-archived agent still pins it (smoke-matrix #19).
    """
    deleted: list[str] = []

    pinned_title = tenant_scoped_display_title(tenant_id=TENANT_ID, name="writing-skills")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        # A user fork pins sk_pinned even though it is not in present_names.
        lambda req, _m: list_response(
            [_tagged_agent(id_="ag_fork", name="my-fork", managed=False, skill_ids=["sk_pinned"])]
        ),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response([_skill_row(id_="sk_pinned", display_title=pinned_title)]),
    )
    router.add(
        "DELETE",
        r"/v1/skills/sk_pinned",
        lambda req, _m: (
            deleted.append("sk_pinned")
            or httpx.Response(500, json={"error": "should not have been called"})
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sweep_removed_skills(
        client, present_names=set(), tenant_id=TENANT_ID, dry_run=False
    )
    assert outcomes == [], "sweep must spare a skill an agent still pins"
    assert deleted == [], "skills.delete must NOT be called for an agent-pinned skill"


async def test_sweep_deletes_orphaned_skill_not_pinned_by_any_agent() -> None:
    """The guard is reference-scoped: an own-tenant skill that no agent references is swept."""
    deleted: list[str] = []

    orphan_title = tenant_scoped_display_title(tenant_id=TENANT_ID, name="stale")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        # The fork pins a DIFFERENT skill — sk_orphan is referenced by nobody.
        lambda req, _m: list_response(
            [_tagged_agent(id_="ag_fork", name="my-fork", managed=False, skill_ids=["sk_other"])]
        ),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response([_skill_row(id_="sk_orphan", display_title=orphan_title)]),
    )
    router.add(
        "GET",
        r"/v1/skills/sk_orphan/versions",
        lambda req, _m: httpx.Response(200, json={"data": [], "next_page": None}),
    )
    router.add(
        "DELETE",
        r"/v1/skills/sk_orphan",
        lambda req, _m: (
            deleted.append("sk_orphan")
            or httpx.Response(
                200,
                json=SkillDeleteResponse(id="sk_orphan", type="skill_deleted").model_dump(
                    mode="json"
                ),
            )
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sweep_removed_skills(
        client, present_names=set(), tenant_id=TENANT_ID, dry_run=False
    )
    assert [o.name for o in outcomes] == [orphan_title], (
        "an unreferenced orphan skill is still swept"
    )
    assert deleted == ["sk_orphan"], "an unreferenced orphan skill is still swept"


async def test_sweep_dry_run_agents() -> None:
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_tagged_agent(id_="ag_1", name="gone")]),
    )
    # No archive handler — router raises if archive is called
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sweep_removed_agents(
        client, present_names=set(), tenant_id=TENANT_ID, dry_run=True
    )
    assert [o.name for o in outcomes] == ["gone"]
    assert outcomes[0].action is Action.ARCHIVED


async def test_sweep_noop_when_all_present() -> None:
    """Skills in present_names (canonical prefixed titles) are never sweep candidates."""
    brainstorming_title = tenant_scoped_display_title(tenant_id=TENANT_ID, name="brainstorming")

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([_tagged_agent(id_="ag_1", name="daimon")]),
    )
    router.add(
        "GET",
        r"/v1/environments",
        lambda req, _m: list_response([_tagged_env(id_="env_1", name="default")]),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response([_skill_row(id_="sk_1", display_title=brainstorming_title)]),
    )
    client = build_fake_anthropic_http(router.dispatch)

    agent_outcomes = await sweep_removed_agents(
        client, present_names={"daimon"}, tenant_id=TENANT_ID, dry_run=False
    )
    env_outcomes = await sweep_removed_environments(
        client, present_names={"default"}, tenant_id=TENANT_ID, dry_run=False
    )
    skill_outcomes = await sweep_removed_skills(
        client, present_names={brainstorming_title}, tenant_id=TENANT_ID, dry_run=False
    )
    assert agent_outcomes == []
    assert env_outcomes == []
    assert skill_outcomes == []


# ---------------------------------------------------------------------------
# SC-2: Cross-tenant sweep proof + truncation-abort test
# ---------------------------------------------------------------------------

# Two tenant UUIDs for cross-tenant tests.
_TENANT_A = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
_TENANT_B = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
_T8_A = str(_TENANT_A)[:8]
_T8_B = str(_TENANT_B)[:8]


async def test_sweep_skips_cross_tenant_and_synced_and_legacy_skills_when_tenant_a_sweeps() -> None:
    """SC-2: Tenant A's sweep is structurally incapable of deleting any skill except its
    own removed seeded skills.

    Skills in the org:
      (a) A's seeded present skill — canonical title, IN present_names → spared by present_names
      (b) A's just-created seeded skill (same run) — canonical, IN present_names → spared
      (c) A's removed seeded skill — canonical, NOT in present_names, unreferenced → DELETED
      (d) Tenant B's seeded skill — B's prefix → strip_tenant_prefix returns None → never candidate
      (e) A's synced-shaped skill (f"{_T8_A}-daimon/cli-auth") → "/" in stripped → never candidate
      (f) Unprefixed legacy skill ("brainstorming") → no prefix → never candidate

    Assert: exactly one DELETE request reaches the router — for (c) only.
    """
    deleted: list[str] = []

    title_a_present = tenant_scoped_display_title(tenant_id=_TENANT_A, name="kept-skill")
    title_a_just_created = tenant_scoped_display_title(tenant_id=_TENANT_A, name="new-skill")
    title_a_removed = tenant_scoped_display_title(tenant_id=_TENANT_A, name="removed-skill")
    title_b_seeded = tenant_scoped_display_title(tenant_id=_TENANT_B, name="b-skill")
    title_a_synced = tenant_scoped_display_title(
        tenant_id=_TENANT_A, name="cli-auth", agent_name="daimon"
    )
    title_legacy = "brainstorming"

    router = MARouter()
    # No agent references any of these skills.
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([]),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response(
            [
                _skill_row(id_="sk_a_present", display_title=title_a_present),
                _skill_row(id_="sk_a_just_created", display_title=title_a_just_created),
                _skill_row(id_="sk_a_removed", display_title=title_a_removed),
                _skill_row(id_="sk_b_seeded", display_title=title_b_seeded),
                _skill_row(id_="sk_a_synced", display_title=title_a_synced),
                _skill_row(id_="sk_legacy", display_title=title_legacy),
            ]
        ),
    )
    # Only (c) should be deleted. Wire the endpoint; verify it fires exactly once.
    router.add(
        "GET",
        r"/v1/skills/sk_a_removed/versions",
        lambda req, _m: httpx.Response(200, json={"data": [], "next_page": None}),
    )
    router.add(
        "DELETE",
        r"/v1/skills/sk_a_removed",
        lambda req, _m: (
            deleted.append("sk_a_removed")
            or httpx.Response(
                200,
                json=SkillDeleteResponse(id="sk_a_removed", type="skill_deleted").model_dump(
                    mode="json"
                ),
            )
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    present_names = {title_a_present, title_a_just_created}
    outcomes = await sweep_removed_skills(
        client, present_names=present_names, tenant_id=_TENANT_A, dry_run=False
    )

    assert len(outcomes) == 1, (
        f"sweep must produce exactly one outcome (the removed skill); got {[o.name for o in outcomes]}"
    )
    assert outcomes[0].name == title_a_removed, (
        f"the only deleted skill must be A's own removed seeded skill (c); got {outcomes[0].name!r}"
    )
    assert outcomes[0].action is Action.DELETED, "outcome action must be DELETED"
    assert deleted == ["sk_a_removed"], (
        "exactly one HTTP DELETE must reach the router — only for skill (c); "
        f"got deletes for: {deleted}"
    )


async def test_sweep_spares_removed_own_skill_still_pinned_by_agent_when_tenant_a_sweeps() -> None:
    """SC-2 second belt: a removed own-tenant skill that an agent still pins is spared.

    (c) from the previous test is absent from present_names but referenced by an agent —
    the reference-guard (list_referenced_skill_ids) spares it. Zero deletes.
    """
    deleted: list[str] = []

    title_a_removed = tenant_scoped_display_title(tenant_id=_TENANT_A, name="removed-skill")
    title_b_seeded = tenant_scoped_display_title(tenant_id=_TENANT_B, name="b-skill")

    router = MARouter()
    # An agent in ANY tenant pins sk_a_removed — the reference guard is org-wide.
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response(
            [_tagged_agent(id_="ag_1", name="daimon", skill_ids=["sk_a_removed"])]
        ),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response(
            [
                _skill_row(id_="sk_a_removed", display_title=title_a_removed),
                _skill_row(id_="sk_b_seeded", display_title=title_b_seeded),
            ]
        ),
    )
    router.add(
        "DELETE",
        r"/v1/skills/sk_a_removed",
        lambda req, _m: (
            deleted.append("sk_a_removed")
            or httpx.Response(500, json={"error": "should not have been called"})
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    outcomes = await sweep_removed_skills(
        client, present_names=set(), tenant_id=_TENANT_A, dry_run=False
    )

    assert outcomes == [], (
        "sweep must produce zero outcomes when the only candidate is pinned by an agent; "
        f"got {[o.name for o in outcomes]}"
    )
    assert deleted == [], (
        "no HTTP DELETE must reach the router when the candidate is agent-referenced"
    )


async def test_sweep_aborts_with_zero_deletes_when_skills_list_is_truncated() -> None:
    """SC-5 sweep half: a full _SKILLS_PAGE_LIMIT-row response causes sweep_removed_skills
    to raise SkillsListTruncatedError BEFORE issuing any delete requests.

    Making delete decisions on a truncated view is unsafe — the sweep must
    hard-fail rather than silently deleting skills it cannot fully enumerate.
    """
    deleted: list[str] = []

    # Build exactly _SKILLS_PAGE_LIMIT rows to trigger the truncation detection.
    full_page = [
        _skill_row(id_=f"sk_{i}", display_title=f"{_T8_A}-skill-{i:04d}")
        for i in range(_SKILLS_PAGE_LIMIT)
    ]

    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents",
        lambda req, _m: list_response([]),
    )
    router.add(
        "GET",
        r"/v1/skills",
        lambda req, _m: list_response(full_page),
    )
    router.add(
        "DELETE",
        r"/v1/skills/[^/]+",
        lambda req, _m: (
            deleted.append(req.url.path)
            or httpx.Response(500, json={"error": "delete must not be called on truncated view"})
        ),
    )
    client = build_fake_anthropic_http(router.dispatch)

    with pytest.raises(SkillsListTruncatedError):
        await sweep_removed_skills(client, present_names=set(), tenant_id=_TENANT_A, dry_run=False)

    assert deleted == [], (
        "sweep must not issue any HTTP DELETE requests when the skills list is truncated; "
        f"got delete requests for: {deleted}"
    )
