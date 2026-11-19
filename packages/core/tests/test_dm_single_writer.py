"""Every write of the DM on/off switch and the DM default agent has one path each.

On/off is the `direct_messages` feature, written by the features service. The default
agent (the one value cold DMs and `daimon dm default` read) is written by
`dm_settings.set_default`, which checks the name and leaves an audit row. A second
writer in any adapter would skip the check or the audit, so this scans every package. The stored row itself is private to the stores by the
import contract on the ORM module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from daimon.core.stores.channel_changes import WORKSPACE_ID, list_changes
from daimon.core.stores.direct_messages import dm_enabled, set_dm_enabled
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession

_PACKAGES = Path(__file__).resolve().parents[2]
_FEATURE_WRITERS = {"set_workspace_feature", "change_feature"}
# Who may call each writer, as `package/daimon/...` paths. Anything else is a second path.
_DEFAULT_AGENT_ALLOWED = {"core/daimon/core/dm_settings.py"}
_ON_OFF_ALLOWED = {
    "core/daimon/core/dm_import.py",  # turning DMs off when an env policy says disabled
    "testing/daimon/testing/factories.py",  # fixtures
}
# Callers of the features writer itself that may name `direct_messages`.
_FEATURE_KEY_ALLOWED = {
    "core/daimon/core/workspace_settings.py",  # set_dms: chat, web and MCP admins
    "core/daimon/core/stores/direct_messages.py",  # set_dm_enabled
}


def _sources() -> list[Path]:
    return [
        path
        for path in _PACKAGES.glob("*/daimon/**/*.py")
        if "/tests/" not in path.as_posix() and "alembic" not in path.parts
    ]


def _called(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _callers(names: set[str]) -> set[str]:
    return {
        path.relative_to(_PACKAGES).as_posix()
        for path in _sources()
        if names & _called(ast.parse(path.read_text()))
    }


def test_only_the_audited_writer_sets_the_default_agent() -> None:
    found = _callers({"set_dm_agent_name"}) - {"core/daimon/core/stores/direct_messages.py"}
    assert found == _DEFAULT_AGENT_ALLOWED, (
        f"the DM default agent is written by dm_settings.set_default only, found {sorted(found)}"
    )


def test_only_the_features_writer_turns_dms_on_or_off() -> None:
    found = _callers({"set_dm_enabled"})
    assert found == _ON_OFF_ALLOWED, f"DM on/off has one writer, found {sorted(found)}"
    named: set[str] = set()
    for path in _sources():
        tree = ast.parse(path.read_text())
        if not _called(tree) & _FEATURE_WRITERS:
            continue
        keys = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
        if "direct_messages" in keys:
            named.add(path.relative_to(_PACKAGES).as_posix())
    unexpected = named - _FEATURE_KEY_ALLOWED - {"core/daimon/core/features/service.py"}
    assert not unexpected, (
        f"another module writes the direct_messages feature: {sorted(unexpected)}"
    )


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_on_off_is_recorded_at_the_workspaces_own_platform(
    db_session: AsyncSession, platform: str
) -> None:
    """The writer reads the platform from the workspace instead of assuming Discord."""
    tenant = await make_tenant(db_session, platform=platform)  # pyright: ignore[reportArgumentType]
    await set_dm_enabled(
        db_session,
        tenant_id=tenant.id,
        enabled=not await dm_enabled(db_session, tenant_id=tenant.id),
    )
    changes = await list_changes(db_session, tenant_id=tenant.id, channel_id=WORKSPACE_ID)
    assert [(c.platform, c.setting) for c in changes] == [(platform, "direct_messages")], changes
