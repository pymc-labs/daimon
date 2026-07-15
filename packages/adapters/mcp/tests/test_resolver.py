"""Tests for auth.resolver — resolve_role is a pure sync function mapping claim strings."""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity, resolve_role
from daimon.core.stores.domain import Role

pytestmark = pytest.mark.asyncio


def test_resolve_role_admin_claim_returns_admin() -> None:
    assert resolve_role("admin") is Role.ADMIN


def test_resolve_role_user_claim_returns_user() -> None:
    assert resolve_role("user") is Role.USER


def test_resolve_role_none_returns_user() -> None:
    assert resolve_role(None) is Role.USER


def test_resolve_role_unknown_string_returns_user() -> None:
    assert resolve_role("superuser") is Role.USER


def test_auth_identity_defaults_platform_and_external_id_to_none() -> None:
    identity = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
    )
    assert identity.platform is None, "platform should default to None when not provided"
    assert identity.external_id is None, "external_id should default to None when not provided"


def test_auth_identity_accepts_platform_and_external_id() -> None:
    identity = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        external_id="g_42",
    )
    assert identity.platform == "discord", "platform should round-trip when provided"
    assert identity.external_id == "g_42", "external_id should round-trip when provided"


def test_auth_identity_is_frozen() -> None:
    identity = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform="discord",
        external_id="g_42",
    )
    with pytest.raises(FrozenInstanceError):
        identity.platform = "cli"  # type: ignore[misc]
