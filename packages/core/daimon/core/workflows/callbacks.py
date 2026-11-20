"""`run.callbacks.<wait node>`: a URL bound to one run and one Wait node, signed with the
deployment's keys. It only delivers data to that Wait (source `callback`), once, while the
run lasts; it never decides an Approval."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Final

from daimon.core.config import Settings
from daimon.core.workflows.spec import Spec, WaitNode

SOURCE: Final = "callback"


def _sign(key: str, run_id: uuid.UUID, node_id: str) -> str:
    message = f"workflow-callback:{run_id}:{node_id}".encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()[:40]


def source_of(run_id: uuid.UUID, node_id: str) -> str:
    """The event source a callback delivers to, as the wait stores it."""
    return f"{SOURCE}:{run_id}:{node_id}"


def urls(settings: Settings, spec: Spec, run_id: uuid.UUID) -> dict[str, str]:
    """Each callback Wait node's URL; none without keys or a public web address."""
    keys = settings.crypto.keys
    if not keys or settings.web is None:
        return {}
    base = str(settings.web.public_url).rstrip("/")
    key = keys[0].get_secret_value()
    return {
        node_id: f"{base}/hooks/callback/{run_id}/{node_id}/{_sign(key, run_id, node_id)}"
        for node_id, node in spec.nodes.items()
        if isinstance(node, WaitNode) and node.event and node.event.source == SOURCE
    }


def verify(settings: Settings, run_id: uuid.UUID, node_id: str, signature: str) -> bool:
    """Any current deployment key signed it, so key rotation keeps open callbacks working."""
    return any(
        hmac.compare_digest(_sign(k.get_secret_value(), run_id, node_id), signature)
        for k in settings.crypto.keys
    )
