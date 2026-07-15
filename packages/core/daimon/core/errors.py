"""Pure error taxonomy for daimon-core. No I/O, no state, stdlib-only.

Adapters catch `DaimonError | anthropic.APIError` at their edge. The SDK's
`APIError` hierarchy (`APIStatusError` with `.status_code` / `.type`,
`APIConnectionError` for network failures) is already a good taxonomy — we
don't re-wrap it. The one conversion boundary is the turn driver, which maps
`anthropic.APIError` to `TurnError(kind="upstream")` so the state machine has
a renderable block; the SDK exception is preserved as `__cause__`.
"""

from __future__ import annotations

from typing import Literal

TurnKind = Literal[
    "interrupted",
    "interrupt_timeout",
    "connection_lost",
    "upstream",
    "reducer_bug",
    "requires_action",
]


class DaimonError(Exception):
    """Base class for all daimon-raised errors. Adapters catch this at the edge."""


class ConfigError(DaimonError):
    """Bad env vars or missing required keys at command start."""


class SpecError(DaimonError):
    """Failure to read, parse, or validate a spec file (defaults/ or user-authored)."""


class DefaultsError(DaimonError):
    """YAML-authoring or skill-packaging validation failure during
    `apply_defaults`. Raised before any MA or DB write when the `defaults/`
    tree is internally inconsistent.
    """


class SkillsListTruncatedError(DefaultsError):
    """Raised when ``skills.list`` returns a full page of results.

    MA never populates ``next_page`` for skills at any page boundary (live probe
    2026-06-10, ``scripts/probes/managed_agents/list_pagination.py``), so a full
    page means the org skill view is truncated. Any create/delete decision made on
    a truncated view is unsafe — callers in write contexts must treat this as a
    hard failure rather than silently proceeding (D-13).
    """


class StoreError(DaimonError):
    """DB constraint violation, expected row missing, uniqueness violation."""


class TurnError(DaimonError):
    """A renderable turn failure. Carried on `TurnState.error`."""

    def __init__(
        self,
        *,
        kind: TurnKind,
        message: str = "",
        cause: object | None = None,
    ) -> None:
        super().__init__(message)
        self.kind: TurnKind = kind
        self.message: str = message
        self.cause: object | None = cause

    def __str__(self) -> str:
        # Surface kind in tracebacks/logs; default Exception.__str__ returns
        # only self.args[0] (the message), which can be empty.
        if self.message:
            return f"{self.kind}: {self.message}"
        return self.kind


class BootstrapError(DaimonError):
    """Startup / factory validation failure: required setting missing, secret
    too short, refusing to mint a daimon-mcp credential for the system account.
    Raised from `create_mcp_app` factory and `ensure_mcp_vault` guard rails.
    """


class GitHubOAuthError(DaimonError):
    """Raised when GitHub returns an error payload from OAuth token exchange."""


class SlackOAuthError(DaimonError):
    """Raised when Slack oauth.v2.access returns an ok:false payload."""


class OAuthCallbackPrincipalUnconfigured(DaimonError):
    """Raised by the OAuth callback when no `account_id_for_state` resolver
    was injected. Phase 18 ships the OAuth contract; Phase 19 (CLI) / Phase 25
    (Discord) wire the real (platform, platform_user_id) → principal_id mapping.
    """
