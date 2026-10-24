"""Provider drivers, one subpackage per provider.

Only `mux.drivers.<provider>` may import that provider's SDK; the
import-linter contracts in the root `pyproject.toml` hold this.
"""

from __future__ import annotations

import os
import ssl

import httpx


def isolated_ssl_context() -> ssl.SSLContext:
    """Build explicit TLS roots only after refusing ambient key logging."""
    if any(name.upper() == "SSLKEYLOGFILE" for name in os.environ):
        raise ValueError("TLS key logging is unsupported")
    return httpx.create_ssl_context(trust_env=False)


__all__ = ["isolated_ssl_context"]
