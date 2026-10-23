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


@pytest.fixture(autouse=True)
def no_deployment_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for env in DEFAULT_KEY_ENV.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", raising=False)


def test_without_deployment_keys_only_anthropic_is_keyed() -> None:
    assert deployment_keyed_providers() == {"anthropic"}


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_provider_counts_only_when_its_deployment_key_is_set(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", blank)
    assert default_enabled_backends() == ("anthropic",), "a blank key is no key"
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")
    assert default_enabled_backends() == ("anthropic", "gemini")
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "set")
    assert default_enabled_backends() == ("anthropic", "openai", "gemini")


async def test_another_tenants_runtime_row_never_turns_a_provider_on(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenant A's own Gemini row and key must leave unconfigured tenant B unchanged."""
    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)
    manifest = tmp_path / "runtime.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "tenant_id": str(tenant_a.id),
                    "profile": "gemini.inline_reuse",
                    "api_key_env": "TENANT_A_GEMINI_KEY",
                }
            ]
        )
    )
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", str(manifest))
    monkeypatch.setenv("TENANT_A_GEMINI_KEY", "set")

    policy = await load_access_policy(db_session, tenant_id=tenant_b.id)

    assert policy.enabled_backends == ("anthropic",)


async def test_a_fresh_tenant_gets_the_keyed_providers(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "set")
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")
    tenant = await make_tenant(db_session)

    policy = await load_access_policy(db_session, tenant_id=tenant.id)

    assert policy.enabled_backends == ("anthropic", "openai", "gemini")
    assert policy.backends_explicit is False


async def test_an_unkeyed_provider_is_never_in_the_default(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "set")
    tenant = await make_tenant(db_session)

    policy = await load_access_policy(db_session, tenant_id=tenant.id)

    assert policy.enabled_backends == ("anthropic", "openai")


async def test_an_admin_restriction_wins_over_the_default(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "set")
    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")
    tenant = await make_tenant(db_session)
    chosen = TenantAccessPolicy(enabled_backends=("anthropic",))
    assert chosen.backends_explicit is True, "a given list is an admin choice"

    await set_access_policy(db_session, tenant_id=tenant.id, policy=chosen)
    db_session.expire_all()

    policy = await load_access_policy(db_session, tenant_id=tenant.id)
    assert policy.enabled_backends == ("anthropic",)
    assert policy.backends_explicit is True


async def test_editing_other_rules_never_freezes_the_default_into_the_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No silent backfill: a defaulted tenant stays on the default after any policy edit."""
    monkeypatch.setenv("DAIMON_CHANNEL_OPENAI_KEY", "set")
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

    monkeypatch.setenv("DAIMON_CHANNEL_GEMINI_KEY", "set")
    db_session.expire_all()
    later = await load_access_policy(db_session, tenant_id=tenant.id)
    assert later.enabled_backends == ("anthropic", "openai", "gemini"), "it still follows keys"
    assert later.invoker_user_ids == ("u1",)
