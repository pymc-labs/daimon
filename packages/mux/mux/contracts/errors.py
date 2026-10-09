"""The error taxonomy.

`MuxError` is the root. A driver catches every provider SDK exception at its
boundary and raises one of these instead, so no SDK type ever reaches the
host. A message that might be ambiguous on delivery is not an exception: it
comes back as a receipt whose status is `outcome_unknown`.
"""

from __future__ import annotations

from typing import Literal

ProviderErrorCategory = Literal[
    "auth",
    "permission",
    "not_found",
    "conflict",
    "invalid_request",
    "rate_limited",
    "overloaded",
    "upstream",
    "transient_network",
]

PROVIDER_ERROR_CATEGORIES: tuple[ProviderErrorCategory, ...] = (
    "auth",
    "permission",
    "not_found",
    "conflict",
    "invalid_request",
    "rate_limited",
    "overloaded",
    "upstream",
    "transient_network",
)


class MuxError(Exception):
    """Base for everything mux raises."""


class InvalidConfig(MuxError):
    """A channel's backend configuration cannot be resolved.

    For example a non-default backend without a model, or a profile that
    belongs to another provider.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnsupportedCapability(MuxError):
    """Admission refused: the profile cannot meet what the config requires.

    `missing` lists every required capability the profile does not support,
    not just the first, so the host can show the whole gap at once.
    """

    def __init__(self, missing: tuple[str, ...], profile: str) -> None:
        super().__init__(f"profile {profile!r} does not support {', '.join(missing)}")
        self.missing = missing
        self.profile = profile


class ExtensionVersionError(MuxError):
    """The profile offers the extension namespace, but not this version."""

    def __init__(self, namespace: str, requested: int, offered: tuple[int, ...]) -> None:
        super().__init__(
            f"extension {namespace!r} v{requested} is not offered (offered: {offered})"
        )
        self.namespace = namespace
        self.requested = requested
        self.offered = offered


class ScopeViolation(MuxError):
    """A reference belongs to another tenant, or to no binding the scope may use."""

    def __init__(self, resource_id: str, reason: str) -> None:
        super().__init__(f"{resource_id}: {reason}")
        self.resource_id = resource_id
        self.reason = reason


class ContinuityLost(MuxError):
    """The thread's conversation or workspace is gone and cannot be continued.

    Raised instead of starting fresh: a silent fresh start is a bug.
    """

    def __init__(self, binding_id: str, evidence: tuple[str, ...]) -> None:
        super().__init__(f"continuity lost for binding {binding_id}: {'; '.join(evidence)}")
        self.binding_id = binding_id
        self.evidence = evidence


class BindingConflict(MuxError):
    """A compare-and-swap on a thread binding lost to another writer."""

    def __init__(self, expected_generation: int, actual_generation: int) -> None:
        super().__init__(
            f"binding generation is {actual_generation}, expected {expected_generation}"
        )
        self.expected_generation = expected_generation
        self.actual_generation = actual_generation


class OperationConflict(MuxError):
    """An operation key was reused with a different request."""

    def __init__(self, key: str) -> None:
        super().__init__(f"operation key {key!r} was already used for a different request")
        self.key = key


class MigrationUnsupported(MuxError):
    """Moving an existing thread to another backend is not supported yet."""

    def __init__(self, thread_id: str) -> None:
        super().__init__(f"thread {thread_id} cannot move to another backend")
        self.thread_id = thread_id


class ProviderError(MuxError):
    """A provider call failed, normalized. `native_code` is the provider's own code."""

    def __init__(
        self,
        category: ProviderErrorCategory,
        *,
        retryable: bool,
        native_code: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        super().__init__(
            f"provider error: {category}" + (f" ({native_code})" if native_code else "")
        )
        self.category: ProviderErrorCategory = category
        self.retryable = retryable
        self.native_code = native_code
        self.operation_id = operation_id
