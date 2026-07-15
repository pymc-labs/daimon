"""daimon-core stores — free async functions over the core tables.

Each submodule takes `session: AsyncSession` as the first arg and returns
Pydantic domain types from `daimon.core.stores.domain`. No module-level state;
callers own the transaction boundary.
"""

from __future__ import annotations

from daimon.core.stores import (
    domain,
    identity,
    scoped_config_read,
    scoped_config_write,
    tenants,
    thread_sessions,
)

__all__ = [
    "domain",
    "identity",
    "scoped_config_read",
    "scoped_config_write",
    "tenants",
    "thread_sessions",
]
