"""Per-channel backend configuration: which provider, profile and model a channel's turns use.

Off by default twice over. With `DAIMON_TURN__CHANNEL_BACKENDS` unset, admission
never reads it. With it set, a channel nobody configured has no revision and
admits exactly as before. A channel's configuration is a chain of immutable
`mux.contracts.config.ConfigRevision`s in `channel_config_revision`; the
newest one applies.

A configured channel is checked at admission, after the config cascade and
before any provider call: its profile must meet what the configuration
requires (`mux.contracts.admission.admit`), and this release must be able to
run it. Today that is Anthropic Managed Agents with the agent's own model,
per caller or shared (`daimon.core.shared_threads`); anything else is refused visibly as
`backend_unsupported` rather than quietly run on Anthropic.
"""

from __future__ import annotations

import uuid

import structlog
from daimon.core.errors import DaimonError
from daimon.core.stores import mux_state
from mux.contracts.admission import Admission as BackendAdmission
from mux.contracts.admission import admit
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef
from mux.errors import InvalidConfig, UnsupportedCapability
from mux.profiles import get_profile
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


class BackendUnsupported(DaimonError):
    """The channel's backend configuration cannot run here.

    Admission turns it into `AdmissionDenied(reason="backend_unsupported")`.
    """


RUNNABLE_PROFILES: frozenset[str] = frozenset({"anthropic.managed_agents"})
"""Profiles this release's turn path can run. Widened as drivers are wired in."""


def channel_ref(tenant_id: uuid.UUID, platform: str, channel_id: str) -> ChannelRef:
    return ChannelRef(tenant_id=str(tenant_id), platform=platform, channel_id=channel_id)


async def current_backend(session: AsyncSession, channel: ChannelRef) -> ConfigRevision | None:
    """The channel's newest configuration revision, or None if it was never configured."""
    return await mux_state.latest_config_revision(session, channel)


async def current_backend_and_sharing(
    session: AsyncSession, channel: ChannelRef
) -> tuple[ConfigRevision | None, bool]:
    """The newest revision and whether any revision ever shared threads, in one read."""
    return await mux_state.latest_config_revision_and_sharing(session, channel)


async def set_channel_backend(
    session: AsyncSession, channel: ChannelRef, config: BackendConfig
) -> ConfigRevision:
    """Record `config` as the channel's configuration (a new revision if it changed).

    Raises `InvalidConfig` for a selection that does not hold together (a
    profile of another backend, a non-default backend without a model). Does
    not check that the profile meets it: that happens at admission, so a
    configuration can be written before the backend it names is runnable.
    """
    resolved = resolve_default(config)
    get_profile(resolved.profile)
    latest = await current_backend(session, channel)
    if latest is not None and latest.digest == resolved.content_digest():
        return latest
    revision = ConfigRevision.create(channel, latest.local + 1 if latest else 1, resolved)
    return await mux_state.put_config_revision(session, revision)


async def clear_channel_backend(session: AsyncSession, channel: ChannelRef) -> ConfigRevision:
    """Return the channel to the default backend, as a new revision."""
    return await set_channel_backend(session, channel, BackendConfig())


def check_backend(revision: ConfigRevision) -> BackendAdmission:
    """Admit `revision`, or raise `BackendUnsupported`."""
    try:
        admission = admit(revision, get_profile(revision.profile))
    except (UnsupportedCapability, InvalidConfig) as err:
        log.info(
            "turn.backend_unsupported",
            channel_id=revision.channel.channel_id,
            profile=revision.profile,
            error=str(err),
        )
        raise BackendUnsupported(str(err)) from err
    runnable = (
        revision.profile in RUNNABLE_PROFILES
        and revision.model is None
        and revision.thread_mode in ("per_caller", "shared")
    )
    if not runnable:
        log.info(
            "turn.backend_unsupported",
            channel_id=revision.channel.channel_id,
            profile=revision.profile,
            error="not runnable in this release",
        )
        raise BackendUnsupported(f"{revision.profile} is not runnable in this release")
    return admission
