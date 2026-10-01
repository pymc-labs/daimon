"""Mux errors. Failures propagate as exceptions, never sentinel returns."""

from __future__ import annotations


class MuxError(Exception):
    """Base for everything mux raises."""


class CapabilityUnavailableError(MuxError):
    """A caller asked for a capability the backend does not declare."""

    def __init__(self, backend: str, capability: str) -> None:
        super().__init__(f"backend {backend!r} does not provide {capability!r}")
        self.backend = backend
        self.capability = capability
