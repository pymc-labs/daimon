"""Typed errors raised by the turn admission chokepoint (D-01 stage one).

Split out of `admission.py` to keep that module under the 200-line target.
Both subclass `DaimonError` so the adapters' existing
`except (DaimonError, anthropic.APIError, <platform error>)` edges catch them
unchanged; adapters add specific handlers on top in the cutover plans.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from daimon.core.errors import DaimonError
from daimon.core.scope import ConfigTier

AdmissionDenialReason = Literal[
    "balance_depleted",
    "cap_exceeded",
    "channel_budget_exceeded",
    "invoker_not_allowed",
    "writers_none",
    "runs_elsewhere",
    "own_agents_only",
    "external_participant",
]
MissingConfigPart = Literal["agent", "environment"]


class AdmissionDenied(DaimonError):
    """Raised by `admit()` when the invoker policy, channel protection, balance, cap
    or channel budget gate rejects the turn.

    Carries only a closed reason literal — no rendered user-facing text.
    Denial copy stays adapter-side.
    """

    def __init__(self, *, reason: AdmissionDenialReason) -> None:
        super().__init__(reason)
        self.reason: AdmissionDenialReason = reason


class DmSourceSealedError(DaimonError):
    """Raised by `reauthorize` when a DM's source channel or thread was sealed since admission.

    The DM path catches it and quarantines the conversation (its copied
    context and sessions), exactly as when the seal is found before admission.
    """


class MissingTurnConfigError(DaimonError):
    """Raised by `admit()` when the config cascade resolves no agent and/or
    no environment for the channel/tenant/deployment scope.

    Carries structured data only (which parts are missing, and which tier —
    if any — each part's config resolved from) so an adapter can render its
    own hint text; never rendered text itself.
    """

    def __init__(
        self,
        *,
        missing: tuple[MissingConfigPart, ...],
        agent_name_tier: ConfigTier | None,
        environment_name_tier: ConfigTier | None,
    ) -> None:
        super().__init__(f"missing turn config: {missing!r}")
        self.missing: tuple[MissingConfigPart, ...] = missing
        self.agent_name_tier: ConfigTier | None = agent_name_tier
        self.environment_name_tier: ConfigTier | None = environment_name_tier


class SessionPreparationFailed(DaimonError):
    """A configuration change could not be applied, so the turn did not run.

    Deliberately not a `TurnError`: nothing was attempted upstream. The old
    session was never torn down either, which is what `preserved` asserts and
    what the adapter's copy rests on — the caller's work is where they left it,
    and the change will be retried at their next message once `retry_after`
    has passed.
    """

    def __init__(
        self,
        *,
        reasons: tuple[str, ...],
        stage: str,
        retry_after: datetime,
        preserved: bool = True,
    ) -> None:
        super().__init__(f"session preparation failed at {stage}: {reasons!r}")
        self.reasons: tuple[str, ...] = reasons
        self.stage: str = stage
        self.retry_after: datetime = retry_after
        self.preserved: bool = preserved


class SessionBusyError(DaimonError):
    """A responder change reached a thread whose previous turn is still running.

    Distinct from `SessionPreparationFailed`: nothing failed and nothing is
    wrong with the configuration. The change simply cannot be made yet, and it
    must not be made *around* — the session still in flight belongs to the
    outgoing responder, so running this turn on it would answer as one agent
    inside another agent's workspace. No turn runs; the caller's next message
    after `retry_after` makes the switch.
    """

    def __init__(
        self,
        *,
        pending_reasons: tuple[str, ...],
        retry_after: datetime,
    ) -> None:
        super().__init__(f"session busy, pending {pending_reasons!r}")
        self.pending_reasons: tuple[str, ...] = pending_reasons
        self.retry_after: datetime = retry_after


class NamedAgentRefused(DaimonError):
    """A named request cannot run here; a thread switch may offer Hand over."""

    def __init__(
        self,
        message: str,
        *,
        hand_over_agent_id: str | None = None,
        hand_over_agent_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.hand_over_agent_id = hand_over_agent_id
        self.hand_over_agent_name = hand_over_agent_name


class SessionAgentMismatch(DaimonError):
    """An existing workspace belongs to a different responder; leave it intact."""

    def __init__(
        self,
        *,
        mapping_id: uuid.UUID,
        session_id: str,
        source_agent_id: str,
        destination_agent_id: str,
    ) -> None:
        super().__init__(
            "This conversation's existing session belongs to another agent. "
            "Continuing with a different responder currently requires a new conversation. "
            "Your existing session and workspace have been kept."
        )
        self.mapping_id = mapping_id
        self.session_id = session_id
        self.source_agent_id = source_agent_id
        self.destination_agent_id = destination_agent_id
