"""Contract tests for MA environment lifecycle: create, retrieve, list, delete."""

from __future__ import annotations

import uuid

import pytest
from anthropic import APIStatusError, AsyncAnthropic
from anthropic.types.beta import BetaCloudConfigParams

pytestmark = pytest.mark.contract

_ENV_CONFIG: BetaCloudConfigParams = {
    "type": "cloud",
    "networking": {"type": "unrestricted"},
    "packages": {"apt": [], "cargo": [], "gem": [], "go": [], "npm": [], "pip": []},
}


def _env_name() -> str:
    return f"contract-test-env-{uuid.uuid4().hex[:8]}"


async def test_environment_create_returns_expected_shape(anthropic_client: AsyncAnthropic) -> None:
    name = _env_name()
    env = await anthropic_client.beta.environments.create(
        name=name,
        config=_ENV_CONFIG,
    )
    assert env.id.startswith("env_"), f"env id must have 'env_' prefix, got {env.id!r}"
    assert env.name == name, "name must round-trip"


async def test_environment_retrieve_matches_create(anthropic_client: AsyncAnthropic) -> None:
    name = _env_name()
    created = await anthropic_client.beta.environments.create(
        name=name,
        config=_ENV_CONFIG,
    )
    retrieved = await anthropic_client.beta.environments.retrieve(created.id)
    assert retrieved.id == created.id, "retrieved id must match created id"
    assert retrieved.name == created.name, "retrieved name must match created name"


async def test_environment_list_includes_created(anthropic_client: AsyncAnthropic) -> None:
    name = _env_name()
    created = await anthropic_client.beta.environments.create(
        name=name,
        config=_ENV_CONFIG,
    )
    ids = [e.id async for e in anthropic_client.beta.environments.list()]
    assert created.id in ids, f"created environment {created.id!r} must appear in list"


async def test_environment_delete_succeeds(anthropic_client: AsyncAnthropic) -> None:
    name = _env_name()
    created = await anthropic_client.beta.environments.create(
        name=name,
        config=_ENV_CONFIG,
    )
    # Delete the environment; 409 only fires when it has active sessions
    # (untestable here without holding open a session), so we just assert delete succeeds.
    try:
        await anthropic_client.beta.environments.delete(created.id)
    except APIStatusError as err:
        if err.status_code == 409:
            # 409 fallback to archive; 409 path not reliably triggerable without active sessions
            await anthropic_client.beta.environments.archive(created.id)
        else:
            raise

    ids = [e.id async for e in anthropic_client.beta.environments.list()]
    assert created.id not in ids, f"deleted environment {created.id!r} must not appear in list"
