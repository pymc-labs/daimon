"""Tests for permission self-check pure function."""

from __future__ import annotations

import discord
from daimon.adapters.discord.permissions import REQUIRED_PERMISSIONS, check_missing_permissions


class TestCheckMissingPermissions:
    def test_all_present(self) -> None:
        perms = discord.Permissions(**{name: True for name in REQUIRED_PERMISSIONS})
        missing = check_missing_permissions(perms)
        assert missing == [], "should return empty list when all permissions present"

    def test_some_missing(self) -> None:
        perms = discord.Permissions(send_messages=True)
        missing = check_missing_permissions(perms)
        assert "send_messages" not in missing, "send_messages should not be in missing list"
        assert len(missing) == len(REQUIRED_PERMISSIONS) - 1, "should have N-1 missing permissions"

    def test_none_present(self) -> None:
        perms = discord.Permissions.none()
        missing = check_missing_permissions(perms)
        assert set(missing) == REQUIRED_PERMISSIONS, "should return all required permissions"

    def test_required_permissions_count(self) -> None:
        assert len(REQUIRED_PERMISSIONS) == 6, "should have exactly 6 required permissions"
