from __future__ import annotations

import hashlib
import json
from typing import Literal

from daimon.core.stores.domain import TenantBackendPolicy
from mux.contracts.config import ConfigRevision
from mux.contracts.ids import ChannelRef, Provider
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator


class ConfigurationReference(BaseModel):
    """Bind an admin's provider or channel-pin confirmation to its preview."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["provider_allowlist", "channel_pin", "channel_pin_clear"]
    providers: tuple[Provider, ...] | None = None
    model: StrictStr | None = None
    """A pin's catalog model (`provider:id`); only a pin names one."""
    expected_revision: StrictInt | StrictStr
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _bound(self) -> ConfigurationReference:
        if (self.operation == "channel_pin") != bool(self.model):
            raise ValueError("only pinning a channel names a model")
        if self.operation == "channel_pin":
            if (
                type(self.expected_revision) is not int
                or self.expected_revision < 0
                or self.providers is not None
            ):
                raise ValueError("pinning a channel requires the pin's revision, 0 for none")
        elif self.operation == "channel_pin_clear":
            if (
                type(self.expected_revision) is not int
                or self.expected_revision < 1
                or self.providers is not None
            ):
                raise ValueError("clearing a channel pin requires the pin's revision")
        elif not isinstance(self.expected_revision, str) or self.providers is None:
            raise ValueError("provider changes require the current policy revision")
        return self


def channel_pin_digest(pin: ConfigRevision) -> str:
    """Binds a pin-clear preview to this exact channel pin revision."""
    return hashlib.sha256(
        json.dumps(
            {
                "channel": pin.channel.model_dump(mode="json"),
                "local": pin.local,
                "digest": pin.digest,
                "operation": "channel_pin_clear",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def channel_pin_set_digest(channel: ChannelRef, local: int, model: str) -> str:
    """Binds a pin preview to the channel's pin revision (0 for none) and the model."""
    return hashlib.sha256(
        json.dumps(
            {
                "channel": channel.model_dump(mode="json"),
                "local": local,
                "model": model,
                "operation": "channel_pin",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def provider_preview_digest(policy: TenantBackendPolicy, providers: tuple[Provider, ...]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "tenant_id": str(policy.tenant_id),
                "revision": policy.revision,
                "providers": providers,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def configuration_refusal(reason: str) -> str:
    return {
        "backend_not_allowed": (
            "That provider is not allowed here. "
            "Ask an admin to enable it in the provider allowlist."
        ),
        "stale_preview": "This preview changed. Preview and confirm again.",
        "stale_revision": "This setting changed. Preview and confirm again.",
        "feature_disabled": "Provider settings are switched off for this Daimon installation.",
        "admin_required": "A current admin must confirm this change.",
    }.get(
        reason,
        "This change could not be saved. Preview again or ask an admin "
        "to check the agent's settings.",
    )
