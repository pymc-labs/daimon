"""Which provider profiles settle their usage at $0 infrastructure on top of their tokens.

provider_cost_of refuses to settle usage whose infrastructure cost is unknown, so a
profile's usage only reaches the ledger once its infrastructure figure is known. Gemini
inline reuse and OpenAI conversation-only sessions run no sandbox or container, so
theirs is exactly $0. An OpenAI persistent workspace runs a hosted container whose
fee no meter reports: it counts as $0 too, so the tokens settle instead of staying
pending forever, and the container fee is the operator's. Self-hosted deployments keep
an unknown figure until a real one exists, and their usage stays pending.
"""

from decimal import Decimal
from typing import Final

from mux.profiles.gemini import INLINE_REUSE
from mux.profiles.openai import CONVERSATION_ONLY, PERSISTENT_WORKSPACE

ZERO_INFRASTRUCTURE_PROFILES: Final[frozenset[str]] = frozenset(
    {INLINE_REUSE.profile_id, CONVERSATION_ONLY.profile_id, PERSISTENT_WORKSPACE.profile_id}
)


def known_infrastructure_usd(profile: str) -> Decimal | None:
    """$0 for a profile that settles at $0 infrastructure; None (unknown) for every other."""
    return Decimal(0) if profile in ZERO_INFRASTRUCTURE_PROFILES else None
