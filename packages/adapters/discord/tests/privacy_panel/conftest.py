"""Local factories for privacy_panel tests.

Inline construction, no shared mutable state. Mirrors billing_panel/conftest.py.
"""

# pyright: reportUnusedFunction=false
# Test factories are imported by sibling test modules; pyright can't see that
# from the conftest in isolation.

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import discord
from anthropic import AsyncAnthropic
from daimon.adapters.discord.privacy_panel.state import PurgePreview, PurgePreviewRow
from daimon.adapters.discord.runtime import DiscordRuntime
from pydantic import HttpUrl


def _make_preview(**overrides: Any) -> PurgePreview:
    """Build a PurgePreview with sensible defaults; override any field via kwargs."""
    base: dict[str, Any] = {
        "linked_principals": PurgePreviewRow(count=1, example="Discord:1234567890"),
        "principal_links": PurgePreviewRow(count=0, example=None),
        "routines": PurgePreviewRow(count=0, example=None),
        "user_configs": PurgePreviewRow(count=0, example=None),
        "account": PurgePreviewRow(count=1, example=None),
        "user_skills": PurgePreviewRow(count=0, example=None),
        "github_credentials": PurgePreviewRow(count=0, example=None),
        "github_oauth_states": PurgePreviewRow(count=0, example=None),
        "mcp_tokens": PurgePreviewRow(count=0, example=None),
        "agent_github_binding": PurgePreviewRow(count=0, example=None),
    }
    base.update(overrides)
    return PurgePreview(**base)


def _make_runtime(
    *, privacy_policy_url: str = "https://github.com/pymc-labs/daimon/blob/main/PRIVACY.md"
) -> DiscordRuntime:
    """Return a MagicMock spec'd against DiscordRuntime for view construction.

    `DiscordRuntime` is a frozen dataclass with `sessionmaker` and `anthropic`
    fields; spec-MagicMock only auto-mocks methods, so both must be assigned
    explicitly for the modal's `purge_account(sm=..., anthropic=...)` call.
    The runtime no longer carries a tenant_id (D-06) — the privacy modal
    resolves tenant per-interaction. `settings.privacy_policy_url` is set
    explicitly (rather than left auto-mocked) so panel.py's
    `str(runtime.settings.privacy_policy_url)` renders a real URL.
    """
    runtime = MagicMock(spec=DiscordRuntime)
    runtime.sessionmaker = MagicMock(name="sessionmaker")
    runtime.anthropic = MagicMock(spec=AsyncAnthropic, name="anthropic")
    runtime.settings = MagicMock(name="settings")
    runtime.settings.privacy_policy_url = HttpUrl(privacy_policy_url)
    return runtime


def _find_button(
    view: discord.ui.View | discord.ui.LayoutView, label: str
) -> discord.ui.Button[Any]:
    # walk_children() recurses into Container/ActionRow nesting (V2 views nest controls).
    for child in view.walk_children():
        if isinstance(child, discord.ui.Button) and child.label == label:
            return child
    raise AssertionError(f"No button labeled {label!r} found in view {type(view).__name__}")
