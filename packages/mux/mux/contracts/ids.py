"""Identity and addressing: who is asking, and which thing they mean."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import Field, JsonValue

from mux.contracts._base import Contract

Provider = Literal["anthropic", "openai", "gemini"]
"""A backend family. One provider can offer several profiles."""

PROVIDERS: tuple[Provider, ...] = ("anthropic", "openai", "gemini")


class Scope(Contract):
    """The authorized caller of one operation.

    The host builds it from its own authorization decision, never from a
    request field, and every port call takes one.
    """

    tenant_id: str
    account_id: str
    principal_id: str
    authorization_id: str


class ChannelRef(Contract):
    """A channel: the key the agent configuration hangs off."""

    tenant_id: str
    platform: str
    channel_id: str


class ThreadRef(Contract):
    """A thread within a channel. The thread owns the workspace."""

    channel: ChannelRef
    thread_id: str


class ResourceRef(Contract):
    """A provider resource the library tracks: agent, session, file, vault.

    `id` is the library identity (existing native IDs may be adopted as is);
    `binding_id` is the routing boundary the resource lives behind.
    """

    id: str
    kind: str
    provider: Provider
    binding_id: str


class Revision(Contract):
    """A resource version. `native` is opaque: not every provider counts."""

    local: int = Field(ge=0)
    native: str | None = None


class PageRequest(Contract):
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    order: Literal["asc", "desc"] = "asc"


class Page[T](Contract):
    data: tuple[T, ...]
    next_cursor: str | None = None


class ModelRef(Contract):
    provider: Provider
    id: str
    options: Mapping[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class SkillRef(Contract):
    """One immutable skill version. A digest, never "latest"."""

    id: str
    digest: str
