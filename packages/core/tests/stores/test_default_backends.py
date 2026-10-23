"""A tenant nobody configured may use every provider the deployment holds a key for."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.provider_keys import (
    DEFAULT_KEY_ENV,
    default_enabled_backends,
    deployment_keyed_providers,
)
from daimon.core.stores.access_policy import load_access_policy, set_access_policy
from daimon.testing.factories import make_tenant
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ROWS = [
    {"tenant_id": "t", "profile": "openai.persistent_workspace", "api_key_env": "NC_TEST_OAI"},
    {"tenant_id": "t", "profile": "gemini.inline_reuse", "api_key_env": "NC_TEST_GEM"},
]


@pytest.fixture(autouse=True)
def no_deployment_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in DEFAULT_KEY_ENV.values():
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(_ROWS))
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(path))
    monkeypatch.delenv("NC_TEST_OAI", raising=False)
    monkeypatch.delenv("NC_TEST_GEM", raising=False)
    return path


def test_without_a_manifest_only_anthropic_is_keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", raising=False)
    assert deployment_keyed_providers() == {"anthropic"}


def test_a_provider_counts_only_when_its_key_is_set(
    manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert default_enabled_backends() == ("anthropic",), "a row without its key is not consent"
    monkeypatch.setenv("NC_TEST_OAI", "set")
    assert default_enabled_backends() == ("anthropic", "openai")
    monkeypatch.setenv("NC_TEST_GEM", "set")
    assert default_enabled_backends() == ("anthropic", "openai", "gemini")


def test_an_unreadable_manifest_keys_nothing(
    manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NC_TEST_OAI", "set")
    manifest.write_text("not json")
    assert deployment_keyed_providers() == {"anthropic"}


async def test_a_fresh_tenant_gets_the_keyed_providers(
    db_session: AsyncSession, manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NC_TEST_OAI", "set")
    monkeypatch.setenv("NC_TEST_GEM", "set")
    tenant = await make_tenant(db_session)

    policy = await load_access_policy(db_session, tenant_id=tenant.id)

    assert policy.enabled_backends == ("anthropic", "openai", "gemini")
    assert policy.backends_explicit is False


async def test_an_unkeyed_provider_is_never_in_the_default(
    db_session: AsyncSession, manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NC_TEST_OAI", "set")
    tenant = await make_tenant(db_session)

    policy = await load_access_policy(db_session, tenant_id=tenant.id)

    assert "gemini" not in policy.enabled_backends
    assert policy.enabled_backends == ("anthropic", "openai")


async def test_an_admin_restriction_wins_over_the_default(
    db_session: AsyncSession, manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NC_TEST_OAI", "set")
    monkeypatch.setenv("NC_TEST_GEM", "set")
    tenant = await make_tenant(db_session)
    chosen = TenantAccessPolicy(enabled_backends=("anthropic",))
    assert chosen.backends_explicit is True, "a given list is an admin choice"

    await set_access_policy(db_session, tenant_id=tenant.id, policy=chosen)
    db_session.expire_all()

    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.enabled_backends == ("anthropic",)
    assert policy.backends_explicit is True


async def test_editing_other_rules_never_freezes_the_default_into_the_row(
    db_session: AsyncSession, manifest: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No silent backfill: a defaulted tenant stays on the default after any policy edit."""
    monkeypatch.setenv("NC_TEST_OAI", "set")
    tenant = await make_tenant(db_session)
    loaded = await load_access_policy(db_session, tenant_id=tenant.id)
    edited = TenantAccessPolicy.model_validate({**loaded.model_dump(), "invoker_user_ids": ("u1",)})

    await set_access_policy(db_session, tenant_id=tenant.id, policy=edited)
    stored = (
        await db_session.execute(
            text(
                "SELECT enabled_backends, enabled_backends_explicit FROM tenant_access_policies "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant.id},
        )
    ).one()
    assert (stored.enabled_backends, stored.enabled_backends_explicit) == (["anthropic"], False)

    monkeypatch.setenv("NC_TEST_GEM", "set")
    db_session.expire_all()
    later = await load_access_policy(db_session, tenant_id=tenant.id)
    assert later.enabled_backends == ("anthropic", "openai", "gemini"), (
        "it still follows the key set"
    )
    assert later.invoker_user_ids == ("u1",)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_deployment_key_keys_its_provider_without_any_row(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", raising=False)
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", blank)
    assert deployment_keyed_providers() == {"anthropic"}, "a blank key is no key"
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")
    assert default_enabled_backends() == ("anthropic", "gemini")
