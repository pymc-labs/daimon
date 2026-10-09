"""Native extensions: typed, namespaced and versioned.

A feature only one provider has (Anthropic memory stores, OpenAI vaults) is
reached through a typed port addressed by `(port type, namespace, version)`
— `ManagedAgents.extension(...)` in `mux.contracts.ports`. There is no raw
client attribute anywhere: an extension call goes through the same scope
check, operation store, journal and usage path as a core call.
"""

from __future__ import annotations

from pydantic import Field, JsonValue, field_validator

from mux.contracts._base import Contract, FrozenMap
from mux.contracts.ids import PROVIDERS


def _check_namespace(namespace: str) -> str:
    provider, _, name = namespace.partition(".")
    if provider not in PROVIDERS or not name:
        raise ValueError(f"namespace {namespace!r} must be '<provider>.<name>'")
    return namespace


class ExtensionRef(Contract):
    """An extension's address, e.g. `anthropic.memory_stores` version 1."""

    namespace: str
    version: int = Field(ge=1)

    _namespace = field_validator("namespace")(_check_namespace)

    def __str__(self) -> str:
        return f"{self.namespace}@{self.version}"


class ExtensionConfig(Contract):
    """Configuration for a native feature, validated against its own schema.

    Never arbitrary SDK keyword arguments: the extension's driver package
    owns and versions the schema `value` must satisfy.
    """

    namespace: str
    version: int = Field(ge=1)
    value: FrozenMap[str, JsonValue] = Field(default_factory=dict[str, JsonValue])

    _namespace = field_validator("namespace")(_check_namespace)


def _refs(*names: str) -> tuple[ExtensionRef, ...]:
    return tuple(ExtensionRef(namespace=n, version=1) for n in names)


ANTHROPIC_EXTENSIONS = _refs(
    "anthropic.agent_tools",
    "anthropic.memory_stores",
    "anthropic.vaults",
    "anthropic.session_resources",
    "anthropic.skills_versions",
    "anthropic.multiagent",
    "anthropic.environments_fork",
    "anthropic.platform_export",
)
OPENAI_EXTENSIONS = _refs("openai.vaults", "openai.steer")
