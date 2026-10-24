"""Typed errors raised by the turn admission chokepoint (stage one).

Split out of `admission.py` to keep that module under the 200-line target.
Both subclass `DaimonError` so the adapters' existing
`except (DaimonError, anthropic.APIError, <platform error>)` edges catch them
unchanged; adapters add specific handlers on top in the cutover plans.
"""

from __future__ import annotations

import re
import sys
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
    # The requester is from another organisation: Daimon answers them only in the
    # channel they share, never in a private conversation.
    "client_requester",
    # The channel's backend configuration (`daimon.core.channel_backend`) names
    # a profile that cannot meet it, or one this release cannot run yet.
    "backend_unsupported",
    # The operator's prepaid hold for a provider's attempts (Gemini Flash) is used
    # up. Nothing to do with a channel budget; an admin releases or raises it.
    "provider_hold_cap_reached",
    # A tenant's daily spend cap on one OpenAI model is used up; it resets at
    # midnight UTC. `AdmissionDenied.provider` carries the model id for the copy.
    "provider_daily_cap_reached",
    # No tariff is current for the model, so its spend can't be counted against the
    # daily cap: refused rather than run uncounted. `provider` carries the model id.
    "provider_price_unavailable",
    # A provider resource this turn needs is still being set up (the tenant's
    # first GPT environment): nothing is wrong; the same request works shortly.
    "provider_setup_pending",
]
# Why a `backend_unsupported` turn refused, for copy that names the fix: the
# channel's own backend pin, a provider the tenant's allowlist doesn't turn on,
# or the agent's backend that this deployment can't run.
# Also carries the one balance cause: a server that never had credit ("no_credit_yet")
# rather than one whose credit ran out.
BackendRefusalCause = Literal[
    "channel_pin",
    "provider_not_allowed",
    "agent_backend",
    "provider_not_configured",
    # A switch's stored consent no longer matches its source: ask for a fresh
    # preview instead of claiming the provider is not set up (J6g).
    "re_confirm",
    "no_credit_yet",
]
MissingConfigPart = Literal["agent", "environment"]


class AdmissionDenied(DaimonError):
    """Raised by `admit()` when the invoker policy, channel protection, balance, cap
    or channel budget gate rejects the turn.

    Carries only closed literals — no rendered user-facing text. Denial copy
    stays adapter-side. A `backend_unsupported` refusal may name its `cause`
    and the `provider` involved, so the copy can name the fix.

    A `backend_unsupported` refusal also records its `site`, the code location
    that raised it ("turn.runtime_manifest.select_worker_row:812"): about 150
    places raise it, and logs need to say which one. It is derived from our own
    module, function and line, so it never carries request data.
    """

    def __init__(
        self,
        *,
        reason: AdmissionDenialReason,
        cause: BackendRefusalCause | None = None,
        provider: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason: AdmissionDenialReason = reason
        self.cause: BackendRefusalCause | None = cause
        self.provider: str | None = provider
        self.site: str | None = raise_site() if reason == "backend_unsupported" else None


_SITE = re.compile(r"[a-z_][a-z0-9_.]*\.[A-Za-z_][A-Za-z0-9_<>]*:\d+")


def raise_site(depth: int = 2) -> str | None:
    """`module.function:line` of the daimon code that constructed the refusal."""
    frame = sys._getframe(depth)  # pyright: ignore[reportPrivateUsage]
    module = str(frame.f_globals.get("__name__", ""))
    if not module.startswith("daimon."):
        return None
    site = f"{module.removeprefix('daimon.core.')}.{frame.f_code.co_name}:{frame.f_lineno}"
    return site if _SITE.fullmatch(site) else None


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


class TurnNotStarted(DaimonError):
    """A turn that never got a turn slot: Stop while it waited, the queue's max
    wait, or a full queue (`daimon.core.turn.slots`). Raised for a continuation,
    so its dispatcher never settles work that did not run as delivered: it is
    offered again after a capacity miss and settled not delivered after Stop."""

    def __init__(self, result: str) -> None:
        super().__init__(f"turn not started: {result}")
        self.result = result

    @property
    def capacity(self) -> bool:
        """The queue was full or the wait ran out: worth offering again."""
        return self.result in ("queue_full", "timed_out")


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
            "This conversation belongs to another agent. Your work is kept. "
            "To use a different agent, start a new conversation."
        )
        self.mapping_id = mapping_id
        self.session_id = session_id
        self.source_agent_id = source_agent_id
        self.destination_agent_id = destination_agent_id
