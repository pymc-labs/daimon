"""Page-event signing keys, derived from the deployment's crypto keys.

The web process signs a page's events for each of its watches and the watch stores
only the key's digest (`watches.rotate_signing_key`), so no key is ever stored or shown.
Every process that has `DAIMON_CRYPTO__KEYS` derives the same key from the watch and
key id. The key id starts with a fingerprint of the crypto key it was derived from, so
a key enrolled before a crypto key rotation stays derivable while the old crypto key
stays listed.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Sequence

from daimon.core.artifact_pages.model import ArtifactError


def _fingerprint(crypto_key: str) -> str:
    return hmac.new(crypto_key.encode(), b"daimon artifacts key id", hashlib.sha256).hexdigest()[:8]


def new_key_id(crypto_keys: Sequence[str]) -> str:
    if not crypto_keys:
        raise ArtifactError("unavailable", "This Daimon has no encryption key set up.")
    return _fingerprint(crypto_keys[0]) + secrets.token_hex(8)


def page_event_key(crypto_keys: Sequence[str], watch_id: uuid.UUID, key_id: str) -> str | None:
    """The watch's signing key for `key_id`; None once its crypto key is no longer listed."""
    for crypto_key in crypto_keys:
        if hmac.compare_digest(_fingerprint(crypto_key), key_id[:8]):
            message = b"daimon artifacts page-event\0%s\0%s" % (
                str(watch_id).encode(),
                key_id.encode(),
            )
            return hmac.new(crypto_key.encode(), message, hashlib.sha256).hexdigest()
    return None
