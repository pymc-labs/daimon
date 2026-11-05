"""DB-free stand-ins for the parity tools: a runtime whose sessions need no database."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.stores.domain import Role
from daimon.core.tool_safety import ToolSafetyPolicy


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _Factory:
    def __call__(self) -> _Session:
        return _Session()

    def begin(self) -> _Session:
        return _Session()


def fake_runtime(*, tool_safety: bool = True) -> McpRuntime:
    settings = SimpleNamespace(tool_safety=ToolSafetyPolicy(enabled=tool_safety))
    return cast(
        McpRuntime,
        SimpleNamespace(session_factory=_Factory(), client=object(), settings=settings),
    )


def make_auth(*, admin: bool = True, platform: str = "discord", **extra: Any) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN if admin else Role.USER,
        platform=platform,
        platform_user_id="u1",
        is_admin=admin,
        is_platform_admin=admin,
        **extra,
    )
