"""Contract tests for MA skill lifecycle: create, version list, delete ordering."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from anthropic import APIStatusError, AsyncAnthropic
from daimon.core.ma import delete_skill_and_versions

pytestmark = pytest.mark.contract


def _skill_name() -> str:
    return f"contract-test-skill-{uuid.uuid4().hex[:8]}"


async def test_skill_create_from_zip_returns_expected_shape(
    anthropic_client: AsyncAnthropic, skill_zip_path: Path
) -> None:
    name = _skill_name()
    with open(skill_zip_path, "rb") as f:
        skill = await anthropic_client.beta.skills.create(
            display_title=name,
            files=[("skill.zip", f, "application/zip")],
        )
    assert skill.id.startswith("skill_"), f"skill id must have 'skill_' prefix, got {skill.id!r}"


async def test_skill_version_list_returns_created(
    anthropic_client: AsyncAnthropic, skill_zip_path: Path
) -> None:
    name = _skill_name()
    with open(skill_zip_path, "rb") as f:
        skill = await anthropic_client.beta.skills.create(
            display_title=name,
            files=[("skill.zip", f, "application/zip")],
        )
    # Create a second version
    with open(skill_zip_path, "rb") as f:
        await anthropic_client.beta.skills.versions.create(
            skill_id=skill.id,
            files=[("skill.zip", f, "application/zip")],
        )
    versions = [v async for v in anthropic_client.beta.skills.versions.list(skill.id)]
    assert len(versions) >= 1, (
        f"skill must have at least 1 version after create, got {len(versions)}"
    )


async def test_skill_delete_without_versions_fails_with_400(
    anthropic_client: AsyncAnthropic, skill_zip_path: Path
) -> None:
    name = _skill_name()
    with open(skill_zip_path, "rb") as f:
        skill = await anthropic_client.beta.skills.create(
            display_title=name,
            files=[("skill.zip", f, "application/zip")],
        )
    # Create a version so the skill definitely has versions
    with open(skill_zip_path, "rb") as f:
        await anthropic_client.beta.skills.versions.create(
            skill_id=skill.id,
            files=[("skill.zip", f, "application/zip")],
        )
    # Do NOT delete versions first — MA must reject this with 400
    with pytest.raises(APIStatusError) as exc_info:
        await anthropic_client.beta.skills.delete(skill.id)
    assert exc_info.value.status_code == 400, (
        f"MA must reject skill delete when versions exist, got status {exc_info.value.status_code}"
    )
    # Clean up properly using the helper
    await delete_skill_and_versions(anthropic_client, skill.id)


async def test_skill_delete_after_version_cleanup_succeeds(
    anthropic_client: AsyncAnthropic, skill_zip_path: Path
) -> None:
    name = _skill_name()
    with open(skill_zip_path, "rb") as f:
        skill = await anthropic_client.beta.skills.create(
            display_title=name,
            files=[("skill.zip", f, "application/zip")],
        )
    # Use helper to delete versions then skill
    await delete_skill_and_versions(anthropic_client, skill.id)
    ids = [s.id async for s in anthropic_client.beta.skills.list()]
    assert skill.id not in ids, (
        f"deleted skill {skill.id!r} must not appear in list after version cleanup"
    )
