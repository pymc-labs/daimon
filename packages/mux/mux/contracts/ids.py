"""Identity and addressing: who is asking, and which thing they mean."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from mux.contracts._base import Contract, FrozenMap

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

    platform_reason: str | None = None
    legacy_call_site: str | None = None

    @model_validator(mode="after")
    def _explicit_authorization(self) -> Scope:
        if self.platform_reason is not None and not self.platform_reason.strip():
            raise ValueError("platform authorization requires a nonblank reason")
        if self.legacy_call_site is not None and not self.legacy_call_site.strip():
            raise ValueError("legacy authorization requires a nonblank call site")
        if self.platform_reason is not None and self.legacy_call_site is not None:
            raise ValueError("platform and legacy authorization are distinct")
        return self

    @property
    def is_platform(self) -> bool:
        return self.platform_reason is not None

    @property
    def is_legacy_host_authorized(self) -> bool:
        return self.legacy_call_site is not None

    @classmethod
    def platform(cls, *, reason: str, authorization_id: str = "platform-resource") -> Scope:
        """Host-only capability for an explicitly authorized workspace operation."""
        return cls(
            tenant_id="platform",
            account_id="service",
            principal_id="daimon",
            authorization_id=authorization_id,
            platform_reason=reason,
        )

    @classmethod
    def legacy_host_authorized(cls, *, call_site: str) -> Scope:
        """Temporary M0 capability: the existing host caller authorizes access.

        This is distinct from operator privilege and must be removed when the
        named caller supplies its tenant authorization decision directly.
        """
        return cls(
            tenant_id="legacy",
            account_id="legacy",
            principal_id="daimon",
            authorization_id="legacy-host-authorized",
            legacy_call_site=call_site,
        )


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
    `account_scope_id` is the provider account or workspace the resource
    lives in, the routing boundary for API keys. It is not a thread binding:
    that is `ProviderBinding.id`.
    """

    id: str
    kind: str
    provider: Provider
    account_scope_id: str
    tenant_id: str | None = None
    account_id: str | None = None


class Revision(Contract):
    """A resource version. `native` is opaque: not every provider counts."""

    local: int = Field(ge=0)
    native: str | None = None


class PageRequest(Contract):
    """What page to fetch. `None` sends nothing, so the provider's default applies."""

    cursor: str | None = None
    limit: int | None = Field(default=None, ge=1, le=1000)
    order: Literal["asc", "desc"] | None = None


class Page[T](Contract):
    """One page.

    `has_more` is the provider's own end-of-list signal and `next_cursor` is
    None exactly when it is false. A last page can be full: a caller that
    guards against truncation compares `len(data)` with the limit it asked
    for and `has_more`, and needs no extra request to do it.
    """

    data: tuple[T, ...]
    has_more: bool = False
    next_cursor: str | None = None

    @model_validator(mode="after")
    def _cursor_matches(self) -> Page[T]:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("next_cursor is set exactly when has_more is true")
        return self


class ModelRef(Contract):
    provider: Provider
    id: str
    options: FrozenMap[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class SkillRef(Contract):
    """A skill, and which version of it.

    `version` pins one version; `None` lets the provider resolve its latest,
    which is how many existing agent configurations reference a skill.
    `source` is the provider's own catalogue name (Anthropic: `anthropic` or
    `custom`). `digest` is the content digest when the library knows it.
    """

    id: str
    version: str | None = None
    source: str | None = None
    digest: str | None = None
