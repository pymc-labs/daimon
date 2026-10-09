"""Create-or-update discovered skills on MA.

Iterates a list of :class:`~daimon.core.skills.discover.DiscoveredSkill`
records (produced by :func:`~daimon.core.skills.discover.discover_skills`)
and pushes each to MA using the same two-state logic as
:func:`~daimon.core.defaults.reconcile_skills.reconcile_skill` — but as a
*batch* function with per-skill error isolation.

This is independent from ``reconcile_skill``; they share
the same leaf helpers but do not call each other. A failure on one skill
records a ``FAILED`` outcome and the batch continues.

Matching is by CANONICAL tenant-prefixed display_title (``{t8}-{name}``),
produced via :func:`~daimon.core.defaults.metadata.tenant_scoped_display_title`
with ``agent_name=None`` (seeded/registry shape). This ensures stack-B skills
are tenant-isolated and distinct across guilds sharing one MA Workspace.

Seeded skills (`defaults/skills/**`) share that exact title shape, so a
same-named import would push a new version onto the seeded skill. The
reconciler's fingerprint would still match the defaults tree and skip it on
every later `defaults apply`, making the overwrite permanent. So an import
never writes to a seeded skill. One carrying exactly the seeded content (an
agent repo that vendors the default, as client agent repos do with
`pymc-artifact-style`) is already in the library: it is reported SKIPPED with
the seeded skill's id, so the caller attaches it like any other import. One
that differs is refused, since the change belongs in `defaults/`.

Any other library skill may already be attached to agents that answer for
everyone, so only an admin import may push a new version onto it. A member's
same-named import is refused and must be renamed.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence

import structlog
from anthropic import AsyncAnthropic
from daimon.core.defaults.ma_index import find_conflicting_skill_mount, find_skill_by_display_title
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.defaults.report import Action, ResourceOutcome
from daimon.core.errors import DaimonError
from daimon.core.skill_zip import build_skill_zip
from daimon.core.skills.discover import DiscoveredSkill
from daimon.core.stores.domain import SeededSkillRow

_log = structlog.get_logger(__name__)


class _ImportRefusedError(DaimonError):
    """A deliberate refusal; `refusal` is the reason as the person who asked reads it."""

    def __init__(self, error: str, *, refusal: str) -> None:
        super().__init__(error)
        self.refusal = refusal


def _rename_hint(name: str) -> str:
    return f"Rename it in the repo (e.g. {name}-2), then ask again to import."


async def sync_skills(
    client: AsyncAnthropic,
    skills: list[DiscoveredSkill],
    *,
    tenant_id: uuid.UUID,
    seeded_skills: Mapping[str, SeededSkillRow],
    is_admin: bool,
) -> list[ResourceOutcome]:
    """Create or update each skill in *skills* on MA.

    For each skill:

    - If the skill already exists on MA (matched by CANONICAL tenant-prefixed
      ``display_title``), a new version is uploaded via ``skills.versions.create``.
    - If not found, a new skill is created via ``skills.create`` with the canonical
      title.

    The canonical title is ``tenant_scoped_display_title(tenant_id, name, agent_name=None)``
    (seeded/registry shape: ``{t8}-{name}``). This ensures skills from different
    tenants syncing the same-named skill get distinct MA resources.

    Lookup uses ``on_truncation="raise"`` — a full-page response in a create context
    is unsafe (hidden duplicates); the per-skill ``except`` boundary surfaces
    :class:`~daimon.core.errors.SkillsListTruncatedError` as a ``FAILED`` outcome
    rather than silently creating a duplicate or missing the existing skill.

    Any exception raised while processing a single skill is caught; a
    ``FAILED`` outcome is recorded and the batch continues with the next skill.
    A skill named in ``seeded_skills`` never writes to MA: with the seeded
    content it is ``SKIPPED`` and carries the seeded skill's id, otherwise it is
    ``FAILED``. A non-admin import that matches an existing library skill is
    ``FAILED`` too (see the module docstring).

    Args:
        client: Anthropic SDK client for MA API calls.
        skills: Discovered skills to sync.
        tenant_id: Owning tenant — determines the canonical title prefix.
        seeded_skills: This tenant's seeded skill fingerprints by name
            (``list_seeded_skills``), which an import may match but not replace.
        is_admin: Whether the importer may push a new version onto an existing
            library skill.

    Returns:
        One :class:`~daimon.core.defaults.report.ResourceOutcome` per input
        skill, in input order.
    """
    outcomes: list[ResourceOutcome] = []
    for skill in skills:
        seeded = seeded_skills.get(skill.spec.name)
        if seeded is not None:
            outcomes.append(await _match_seeded(client, skill, seeded, tenant_id=tenant_id))
            continue
        try:
            canonical = tenant_scoped_display_title(tenant_id=tenant_id, name=skill.spec.name)
            ma_match = await find_skill_by_display_title(client, canonical, on_truncation="raise")
            pkg = build_skill_zip(skill.skill_dir, name=skill.spec.name)
            try:
                if ma_match is not None and not is_admin:
                    raise _ImportRefusedError(
                        f"skill name {skill.spec.name!r} is already taken in this library, "
                        f"and only an admin can replace it. Rename the skill (e.g. "
                        f"{skill.spec.name}-2) and re-sync.",
                        refusal=(
                            f"`{skill.spec.name}` is already in this library, and only an "
                            f"admin can replace it. {_rename_hint(skill.spec.name)}"
                        ),
                    )
                if ma_match is not None:
                    with pkg.path.open("rb") as fh:
                        await client.beta.skills.versions.create(
                            skill_id=ma_match.id,
                            files=[("SKILL.zip", fh, "application/zip")],
                        )
                    outcomes.append(
                        ResourceOutcome(
                            kind="skill",
                            name=skill.spec.name,
                            action=Action.UPDATED,
                            anthropic_id=ma_match.id,
                        )
                    )
                else:
                    conflict = await find_conflicting_skill_mount(
                        client, tenant_id=tenant_id, name=skill.spec.name, agent_name=None
                    )
                    if conflict is not None:
                        raise _ImportRefusedError(
                            f"skill name {skill.spec.name!r} is already taken by "
                            f"{conflict.display_title!r} — the two would mount at the same "
                            f"path on an agent. Rename the skill (e.g. "
                            f"{skill.spec.name}-2) and re-sync.",
                            refusal=(
                                f"`{skill.spec.name}` would share a path on an agent with "
                                f"another skill. {_rename_hint(skill.spec.name)}"
                            ),
                        )
                    with pkg.path.open("rb") as fh:
                        created = await client.beta.skills.create(
                            display_title=canonical,
                            files=[("SKILL.zip", fh, "application/zip")],
                        )
                    outcomes.append(
                        ResourceOutcome(
                            kind="skill",
                            name=skill.spec.name,
                            action=Action.CREATED,
                            anthropic_id=created.id,
                        )
                    )
            finally:
                pkg.path.unlink(missing_ok=True)
        except Exception as err:
            _log.warning("sync.skill_failed", name=skill.spec.name, error=str(err))
            outcomes.append(
                ResourceOutcome(
                    kind="skill",
                    name=skill.spec.name,
                    action=Action.FAILED,
                    error=str(err),
                    refusal=err.refusal if isinstance(err, _ImportRefusedError) else None,
                )
            )
    return outcomes


async def _match_seeded(
    client: AsyncAnthropic,
    skill: DiscoveredSkill,
    seeded: SeededSkillRow,
    *,
    tenant_id: uuid.UUID,
) -> ResourceOutcome:
    """SKIPPED with the seeded id when the import is the seeded content, else FAILED."""
    name = skill.spec.name
    try:
        pkg = build_skill_zip(skill.skill_dir, name=name)
        pkg.path.unlink(missing_ok=True)
        canonical = tenant_scoped_display_title(tenant_id=tenant_id, name=name)
        ma_match = await find_skill_by_display_title(client, canonical, on_truncation="raise")
    except Exception as err:
        _log.warning("sync.skill_failed", name=name, error=str(err))
        return ResourceOutcome(kind="skill", name=name, action=Action.FAILED, error=str(err))
    # The fingerprint only vouches for the skill id it was recorded against,
    # as in `reconcile_skill`.
    if (
        ma_match is not None
        and ma_match.id == seeded.anthropic_id
        and pkg.content_hash == seeded.content_hash
    ):
        _log.info("sync.seeded_skill_matched", name=name, skill_id=ma_match.id)
        return ResourceOutcome(
            kind="skill", name=name, action=Action.SKIPPED, anthropic_id=ma_match.id
        )
    if ma_match is None or ma_match.id != seeded.anthropic_id:
        # The content may well match; the default itself is missing or was
        # recreated here, so the fingerprint cannot vouch for it.
        _log.warning("sync.seeded_skill_unverified", name=name)
        return ResourceOutcome(
            kind="skill",
            name=name,
            action=Action.FAILED,
            error=(
                f"the default skill {name!r} is missing or out of date on this deployment, "
                "so an import cannot be matched against it. Run `daimon defaults apply`, "
                "then re-sync."
            ),
            refusal=(
                f"`{name}` is a default skill that this server has not finished setting up. "
                "Ask an operator to re-apply the defaults, then ask again to import."
            ),
        )
    _log.warning("sync.seeded_skill_refused", name=name)
    return ResourceOutcome(
        kind="skill",
        name=name,
        action=Action.FAILED,
        error=(
            f"skill name {name!r} belongs to a default skill and differs from it, so an "
            f"import cannot replace it. Change defaults/skills/{name} instead, or rename "
            f"the skill (e.g. {name}-2) and re-sync."
        ),
        refusal=(
            f"`{name}` is a default skill's name, and this copy differs from the default. "
            f"{_rename_hint(name)}"
        ),
    )


def in_library(outcome: ResourceOutcome) -> bool:
    """Whether an import left `outcome`'s skill in the library, ready to attach.

    A SKIPPED import is a seeded skill the repo carries unchanged.
    """
    return outcome.anthropic_id is not None and outcome.action in (
        Action.CREATED,
        Action.UPDATED,
        Action.SKIPPED,
    )


def library_skill_ids(outcomes: Sequence[ResourceOutcome]) -> list[str]:
    """The sorted ids of every skill `outcomes` left in the library (see `in_library`)."""
    return sorted(
        outcome.anthropic_id
        for outcome in outcomes
        if outcome.anthropic_id is not None and in_library(outcome)
    )


def summarize_failed_imports(outcomes: Sequence[ResourceOutcome]) -> str | None:
    """One person-facing line on the skills that did not import, or None.

    Only a deliberate refusal is explained; any other error stays in the logs,
    since provider error bodies do not belong on a channel-visible card.
    """
    failed = [outcome for outcome in outcomes if outcome.action is Action.FAILED]
    if not failed:
        return None
    first = failed[0]
    reason = first.refusal or f"`{first.name}` (upload failed)."
    others = len(failed) - 1
    tail = f" {others} more did not import either." if others else ""
    return f"Not imported: {reason}{tail}"
