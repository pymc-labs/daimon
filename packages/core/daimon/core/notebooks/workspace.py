"""Per-conversation workspace slug derivation.

Pure function — no state, no I/O. The output is fed into ``_resolve_slug``
in ``publish.py`` to add the principal prefix; passing it directly to
``publish_notebook`` / ``attach_notebook_data`` as the ``slug`` kwarg is
the canonical use.
"""

from __future__ import annotations

import hashlib

_DIGEST_SIZE_BYTES = 16  # 16 bytes -> 32 hex chars, fits _AGENT_SLUG_PATTERN.


def conversation_workspace_slug(*, principal_key: str, conversation_id: str) -> str:
    """Deterministic 32-char hex slug for a (principal, conversation) pair.

    Same inputs always produce the same output (no clock, no RNG). Two
    different ``conversation_id`` values for the same principal produce
    two different slugs — that is the whole point: an adapter computes
    this per turn and the agent gets a stable workspace for the duration
    of the conversation without storage.
    """
    h = hashlib.blake2b(
        f"{principal_key}:{conversation_id}".encode(),
        digest_size=_DIGEST_SIZE_BYTES,
    )
    return h.hexdigest()
