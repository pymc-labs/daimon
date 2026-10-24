"""Synthetic staffing facts and backend contract; never uses the network."""

from datetime import UTC, datetime
from uuid import UUID

import httpx
from daimon.core.recruitment import (
    Candidate,
    RecruitmentConfig,
    SourceMessage,
    enrollment_source,
    may_decide,
)
from daimon.core.recruitment_api import RecruitmentAPI, SyntheticRecruitmentTransport
from pydantic import SecretStr


def test_enrollment_and_live_recruiter_boundary() -> None:
    config = RecruitmentConfig(guild_id="1", recruiter_role_ids=("7",), channel_ids=("2",))
    assert enrollment_source("  !LABS-Staffing: Synthetic", "2", config)
    assert not enrollment_source("!labs-staffing:   ", "2", config)
    assert not enrollment_source("Staffing request", "2", config)
    assert not may_decide(config, "9", ("7",), can_access=False)
    assert may_decide(config, "9", ("7",), can_access=True)
    assert not may_decide(config, "9", (), can_access=True)


async def test_fake_matches_and_idempotent_registration() -> None:
    fake = SyntheticRecruitmentTransport()
    async with httpx.AsyncClient(transport=fake) as http:
        api = RecruitmentAPI(http, "https://synthetic.invalid", SecretStr("test"))
        payload = {
            "discord_guild_id": "1",
            "discord_thread_id": "2",
            "candidate": {"name": "Synthetic"},
        }
        assert (await api.match(payload)).status == "none"
        registration = {
            **payload,
            "action": "create",
            "discord_channel_id": "3",
            "discord_thread_url": "https://synthetic.invalid/2",
            "source": "staffing",
            "confirmation": {
                "discord_user_id": "7",
                "discord_interaction_id": "i",
                "confirmed_at": datetime.now(UTC).isoformat(),
            },
        }
        first = await api.register(registration, "stable")
        assert await api.register(registration, "stable") == first
        assert len(fake.effects) == 1
        assert (await api.match(payload)).status == "linked"
        assert UUID(str(first.candidate_id))


def test_source_revision_uses_real_timestamp() -> None:
    source = SourceMessage(
        id="1", content="Synthetic", observed_at=datetime.now(UTC), url="https://example.test/1"
    )
    assert source.observed_at.tzinfo is not None
    assert Candidate(name="Synthetic").name == "Synthetic"


async def test_synthetic_runtime_extracts_updates_without_provider_calls() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from daimon.core.config import Settings
    from daimon.core.recruitment_runtime import backend_client, build_service

    settings = Settings(
        database={"url": "postgresql+asyncpg://unused:unused@localhost/daimon_test_nc_staffing"},
        _env_file=None,
    )
    settings.recruitment.enabled = True
    settings.recruitment.synthetic = True
    model = AsyncMock()
    async with backend_client(settings) as http:
        service = build_service(
            settings, SimpleNamespace(tenant_id=uuid4()), SimpleNamespace(), http, model
        )
        source = SourceMessage(
            id="10",
            content="Synthetic phone: 555-0100",
            observed_at=datetime.now(UTC),
            url="https://synthetic.invalid/10",
        )
        extraction = await service.extract([source], Candidate(name="Synthetic"), {})
        assert extraction.changes[0].value == "555-0100"
        assert extraction.changes[0].source_message_id == "10"
        model.messages.parse.assert_not_awaited()


def test_recruitment_migration_has_no_diff_whitespace_errors() -> None:
    from pathlib import Path

    migration = Path(__file__).parents[1] / "alembic/versions/0145_recruitment.py"
    assert [
        i for i, line in enumerate(migration.read_text().splitlines(), 1) if line.rstrip() != line
    ] == []


def test_settings_require_explicit_parent_channels_and_recruiters():
    import pytest
    from daimon.core.config import RecruitmentSettings
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RecruitmentSettings(tenants={"101": {"channel_ids": [], "recruiter_user_ids": ["7"]}})
    with pytest.raises(ValidationError):
        RecruitmentSettings(tenants={"101": {"channel_ids": ["2"]}})
    config = RecruitmentConfig(guild_id="101", channel_ids=("2",))
    assert not enrollment_source("!labs-staffing: Synthetic", "3", config)


def test_settings_backed_tenants_are_disabled_until_explicitly_enabled():
    from daimon.core.config import RecruitmentSettings

    settings = RecruitmentSettings(
        tenants={"101": {"channel_ids": ["2"], "recruiter_user_ids": ["7"]}}
    )
    assert not settings.enabled and not settings.tenants["101"].enabled
    assert settings.is_recruitment_channel("101", "20", "2")
