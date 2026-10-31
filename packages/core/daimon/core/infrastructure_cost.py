"""Which provider profiles have an infrastructure charge on top of their tokens.

provider_cost_of refuses to settle usage whose infrastructure cost is unknown, so a
profile's usage only reaches the ledger once its infrastructure figure is known. These
profiles have none to know: their sessions run no sandbox, container or hosted
environment, only model requests, so their infrastructure is exactly $0. Every other
profile (OpenAI persistent workspaces with hosted containers, self-hosted deployments)
keeps an unknown figure until a real one exists, and its usage stays pending.
"""

from decimal import Decimal
from typing import Final

from mux.profiles.gemini import INLINE_REUSE
from mux.profiles.openai import CONVERSATION_ONLY

#: Gemini inline reuse: an inline model request per turn, no sandbox or container.
#: OpenAI conversation-only: a conversation with no sandbox, container or tools.
NO_INFRASTRUCTURE_PROFILES: Final[frozenset[str]] = frozenset(
    {INLINE_REUSE.profile_id, CONVERSATION_ONLY.profile_id}
)


def known_infrastructure_usd(profile: str) -> Decimal | None:
    """$0 for a profile with no infrastructure; None (unknown) for every other."""
    return Decimal(0) if profile in NO_INFRASTRUCTURE_PROFILES else None
