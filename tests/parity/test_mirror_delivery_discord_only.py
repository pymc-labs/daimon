"""Executable record that mirrors post only into Discord for now.

Slack and Teams have no mirror poster: a Slack destination needs the bot's
thread replies and a Teams one its conversation references, neither built.
If a poster appears there, replace this record rather than deleting it. No
database: `create_mirror` refuses before reading any.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import daimon.adapters.slack
import daimon.adapters.teams
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.mirrors import (
    _create_mirror_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import MirrorsSettings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError


@pytest.mark.parametrize("package", [daimon.adapters.slack, daimon.adapters.teams])
def test_no_mirror_poster_outside_discord(package: object) -> None:
    root = Path(package.__file__).parent  # type: ignore[attr-defined]
    offenders = sorted(
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if any(term in path.read_text() for term in ("run_mirror_poller", "MirrorPoster"))
    )
    assert offenders == [], f"{root.name} posts mirrors: {offenders}; replace this record"


@pytest.mark.parametrize("platform", ["slack", "teams"])
async def test_create_mirror_refuses_other_platforms(platform: str) -> None:
    settings = MagicMock()
    settings.mirrors = MirrorsSettings(enabled=True)
    runtime = McpRuntime(
        session_factory=MagicMock(),  # type: ignore[arg-type]  # refused before any read
        client=MagicMock(),  # type: ignore[arg-type]
        settings=settings,
        deployment_default=DeploymentDefault(),
    )
    auth = AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.USER,
        platform=platform,
        is_admin=True,
    )

    with pytest.raises(ToolError, match="only into Discord"):
        await _create_mirror_impl(
            runtime,
            auth,
            name="qa",
            agent_name="helper",
            destination_id="C0123",
            destination_kind="channel",
            consent_note=None,
            max_answer_chars=None,
        )
