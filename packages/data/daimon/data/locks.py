"""Advisory-lock keys, namespaced so two stores (tests, deployments) never collide."""

from __future__ import annotations

import hashlib

CLUSTER = "cluster"
"""Namespace for what every store on the server shares, such as the roles."""


def advisory_key(namespace: str, name: str) -> int:
    """A stable signed 64-bit key for the lock `name` in `namespace` (a schema or `CLUSTER`)."""
    digest = hashlib.blake2b(f"{namespace}:{name}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)
