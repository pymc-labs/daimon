"""Per-channel backend configuration: which provider, profile and model a channel's turns use.

Off by default twice over. With `DAIMON_TURN__CHANNEL_BACKENDS` unset, admission
never reads it. With it set, a channel nobody configured has no revision and
admits exactly as before. A channel's configuration is a chain of immutable
`mux.contracts.config.ConfigRevision`s in `channel_config_revision`; the
newest one applies.

A configured channel is checked at admission, after the config cascade and
before any provider call: its profile must meet what the configuration
requires (`mux.contracts.admission.admit`), and this release must be able to
run it. Anthropic Managed Agents uses the agent's own model, per caller or
shared (`daimon.core.shared_threads`). Explicit OpenAI persistent-workspace
channels use gpt-6-luna per caller and require an injected native host runtime;
anything else is refused visibly as
`backend_unsupported` rather than quietly run on Anthropic.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Literal, Protocol, cast, get_args

import structlog
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.access_policy import OPEN_ACCESS_POLICY
from daimon.core.authz import Action, Subject, authorize
from daimon.core.config import TurnSettings, load_selfhosted_settings
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.model_presets import model_choice_id
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import source_agent_record
from daimon.core.neutral_resources import NeutralResourceHost
from daimon.core.pricing import (
    MODEL_PRICING,
    ProviderPrice,
    list_price_preview,
    provider_price_expired_on,
    provider_prices,
)
from daimon.core.provider_keys import default_enabled_backends
from daimon.core.stores import access_policy as policy_store
from daimon.core.stores import agent_backends, agent_owners, mux_state, security_audit
from daimon.core.stores.domain import (
    AgentBackendRevision,
    BackendAgent,
    CompatibilityItem,
    CompatibilityPreview,
    TenantBackendPolicy,
)
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.thread_sessions import get_thread_session_at
from mux.contracts.admission import Admission as BackendAdmission
from mux.contracts.admission import admit
from mux.contracts.config import (
    DEFAULT_BACKEND,
    BackendConfig,
    CapabilityRequirement,
    ConfigRevision,
    ResolvedBackend,
    ThreadMode,
    resolve_default,
)
from mux.contracts.ids import PROVIDERS, ChannelRef, ModelRef, Provider, ThreadRef
from mux.contracts.profile import SUPPORTED, Capability, Profile
from mux.contracts.resources import AgentSpec
from mux.errors import InvalidConfig, ScopeViolation, UnsupportedCapability
from mux.profiles import (
    MANAGED_AGENTS,
    PROFILES,
    RUNNABLE_PROFILES,
    backend_selection,
    get_profile,
    runnable_selection,
    selectable_models,
    shared_threads_available,
    tariff_gated,
)
from mux.state.lease import Slot
from mux.state.operations import request_digest
from mux.state.store import ConfigRevisionConflict
from pydantic import JsonValue
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)
DEFAULT_BACKEND_LABEL = f"Use default ({DEFAULT_BACKEND.capitalize()})"


class BackendUnsupported(DaimonError):
    """The channel's backend configuration cannot run here.

    Admission turns it into `AdmissionDenied(reason="backend_unsupported")`.
    """


def channel_ref(tenant_id: uuid.UUID, platform: str, channel_id: str) -> ChannelRef:
    return ChannelRef(tenant_id=str(tenant_id), platform=platform, channel_id=channel_id)


async def current_backend(session: AsyncSession, channel: ChannelRef) -> ConfigRevision | None:
    """The channel's newest configuration revision, or None if it was never configured."""
    return await mux_state.latest_config_revision(session, channel, overrides_only=True)


async def is_selfhosted_channel(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, channel_id: str
) -> bool:
    """Whether this channel forbids auxiliary Anthropic calls, even while admission is off."""
    revision = await current_backend(session, channel_ref(tenant_id, platform, channel_id))
    return revision is not None and revision.backend == "selfhosted"


async def effective_thread_backend(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    channel_id: str,
    thread_id: str | None,
    caller_id: str | None,
) -> Provider:
    """Honor the retained shared/caller binding before the current channel selection.

    Slot order and legacy-session fallback match admission. Only local facts
    are read, so deciding whether provider access is allowed cannot itself
    send conversation content to a provider.
    """
    channel = channel_ref(tenant_id, platform, channel_id)
    latest, ever_shared = await current_backend_and_sharing(session, channel)
    if thread_id is not None:
        principal = (
            await find_platform_principal(
                session, tenant_id=tenant_id, platform=platform, external_id=caller_id
            )
            if caller_id is not None
            else None
        )
        from daimon.core.thread_release import release_pending

        thread = ThreadRef(channel=channel, thread_id=thread_id)
        slots = [Slot(thread=thread)] if ever_shared else []
        released = False
        if principal is not None:
            slots.append(Slot(thread=thread, account_id=str(principal.account_id)))
            # A confirmed switch from this thread: its next turn selects afresh.
            released = await release_pending(session, slots[-1])
        for slot in slots:
            binding = await mux_state.get_binding(session, slot)
            if binding is None or (released and slot.account_id is not None):
                continue
            if binding.thread != slot.thread or binding.legacy_account_id != slot.account_id:
                raise ScopeViolation(binding.id, "selected binding differs from authorized thread")
            retained, _, _ = await resolve_turn_backend(
                session, channel, None, enabled=True, retained_revision=binding.config_revision
            )
            if binding.config_revision == 0:
                default = resolve_default(BackendConfig())
                if (binding.provider, binding.profile) != (default.backend, default.profile):
                    raise BackendUnsupported("retained_default_mismatch")
                return default.backend
            if retained is None or (
                retained.channel != channel
                or retained.local != binding.config_revision
                or retained.backend != binding.provider
                or retained.profile != binding.profile
            ):
                raise BackendUnsupported("retained_binding_mismatch")
            return retained.backend
        if principal is not None and not released:
            previous = await get_thread_session_at(
                session,
                tenant_id=tenant_id,
                platform=platform,
                thread_id=thread_id,
                account_id=principal.account_id,
                at=datetime.now(UTC),
            )
            if previous is not None:
                if previous.channel_id not in (None, channel_id):
                    raise ScopeViolation(
                        previous.ma_session_id, "session history differs from authorized channel"
                    )
                return MANAGED_AGENTS.provider
    return latest.backend if latest is not None else DEFAULT_BACKEND


async def current_backend_and_sharing(
    session: AsyncSession, channel: ChannelRef
) -> tuple[ConfigRevision | None, bool]:
    """The newest revision and whether any revision ever shared threads, in one read."""
    return await mux_state.latest_config_revision_and_sharing(session, channel, overrides_only=True)


async def set_channel_backend(
    session: AsyncSession, channel: ChannelRef, config: BackendConfig
) -> ConfigRevision:
    """Record `config` as the channel's configuration (a new revision if it changed).

    Raises `InvalidConfig` for a selection that does not hold together (a
    profile of another backend, a non-default backend without a model), and
    `BackendUnsupported` for a tariff-gated model with no current price. Does
    not otherwise check that the profile meets it: that happens at admission,
    so a configuration can be written before the backend it names is runnable.
    """
    await policy_store.lock_policy_writes_exclusive(session, tenant_id=uuid.UUID(channel.tenant_id))
    resolved = resolve_default(config)
    get_profile(resolved.profile)
    require_current_tariff(ConfigRevision.create(channel, 0, resolved))
    latest = await current_backend(session, channel)
    if (
        latest is not None
        and latest.digest == resolved.content_digest()
        and await mux_state.config_origin(session, channel, latest.local) == "channel"
    ):
        return latest
    revision = ConfigRevision.create(
        channel, await mux_state.next_config_local(session, channel), resolved
    )
    return await mux_state.put_config_revision(session, revision)


async def clear_channel_backend(
    session: AsyncSession,
    channel: ChannelRef,
    *,
    actor: BackendActor | None = None,
    expected: ConfigRevision | None = None,
) -> ConfigRevision:
    """Clear the override while retaining an immutable default revision for thread pins.

    `expected` is the pin a confirmed preview showed: under the policy write lock
    the channel must still hold exactly that pin, else `AgentBackendRefused`.
    `actor` records who cleared which channel in the security audit.
    """
    await policy_store.lock_policy_writes_exclusive(session, tenant_id=uuid.UUID(channel.tenant_id))
    latest = await current_backend(session, channel)
    origin = await mux_state.config_origin(session, channel, latest.local) if latest else None
    if expected is not None and (
        latest is None
        or origin != "channel"
        or latest.local != expected.local
        or latest.digest != expected.digest
    ):
        raise AgentBackendRefused("stale_revision")
    if latest is not None and origin == "channel_clear":
        return latest
    revision = ConfigRevision.create(
        channel, await mux_state.next_config_local(session, channel), resolve_default(None)
    )
    cleared = await mux_state.put_config_revision(session, revision, origin="channel_clear")
    if actor is not None:
        await security_audit.append_event(
            session,
            tenant_id=uuid.UUID(channel.tenant_id),
            account_id=actor.account_id,
            agent_id=None,
            platform=channel.platform,
            platform_user_id=actor.subject.platform_user_id,
            tool_name="channel_backend",
            operation="clear_pin",
            outcome="allowed",
            reason="completed",
            target_channel_id=channel.channel_id,
            scope=(
                f"cleared {latest.backend} pin local {latest.local}"
                if latest is not None
                else "no pin"
            ),
        )
    return cleared


def require_current_tariff(revision: ConfigRevision) -> None:
    """Refuse a tariff-gated model unless the host holds a current price for it."""
    if not tariff_gated(revision) or revision.model is None:
        return
    if (revision.backend, revision.model) in provider_prices(revision.profile):
        return
    log_backend_unsupported("backend_runnable", reason="tariff_unavailable", revision=revision)
    ended = provider_price_expired_on(revision.profile, revision.backend, revision.model)
    if ended is not None:
        raise BackendUnsupported(
            f"{revision.model}'s price window ended on {ended.isoformat()}, so it can't run "
            "until a new price is published. Pick another model."
        )
    raise BackendUnsupported(f"{revision.model} has no current price here. Pick another model.")


def check_backend(
    revision: ConfigRevision,
    *,
    enabled_backends: tuple[Provider, ...] | None = None,
) -> BackendAdmission:
    """Admit `revision`, or raise `BackendUnsupported`."""
    if revision.backend == "selfhosted" and not load_selfhosted_settings().admission_enabled:
        log_backend_unsupported(
            "backend_runnable", reason="selfhosted_admission_disabled", revision=revision
        )
        raise BackendUnsupported("selfhosted_admission_disabled")
    if enabled_backends is not None and not backend_is_enabled(revision.backend, enabled_backends):
        log_backend_unsupported("workspace_policy", reason="backend_not_enabled", revision=revision)
        raise BackendUnsupported("That model provider isn't turned on here. Pick another.")
    try:
        admission = admit(revision, get_profile(revision.profile))
    except (UnsupportedCapability, InvalidConfig) as err:
        stage = (
            "backend_capabilities"
            if isinstance(err, UnsupportedCapability)
            else "backend_configuration"
        )
        log_backend_unsupported(stage, err, revision=revision)
        raise BackendUnsupported(str(err)) from err
    runnable = revision.profile in RUNNABLE_PROFILES and runnable_selection(revision)
    if not runnable:
        log_backend_unsupported(
            "backend_runnable", reason="profile_not_runnable", revision=revision
        )
        raise BackendUnsupported(f"{revision.profile} is not runnable in this release")
    require_current_tariff(revision)
    return admission


@dataclass(frozen=True)
class BackendActor:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    subject: Subject


@dataclass(frozen=True)
class AgentSettings:
    """What an agent states about how it runs; unset is its backend's default.

    `profile` must belong to the agent's backend, `requires` names the
    capabilities the agent needs (a channel pin may refuse a turn over them but
    never drops one), and `thread_mode="shared"` is the agent's intent to share
    threads, which a channel pin that keeps them per caller overrides.
    """

    profile: str | None = None
    requires: Mapping[Capability, CapabilityRequirement] | None = None
    thread_mode: ThreadMode | None = None

    @classmethod
    def of(cls, revision: AgentBackendRevision | None) -> AgentSettings:
        if revision is None:
            return cls()
        return cls(revision.profile, revision.requires or None, revision.thread_mode)

    def is_default(self) -> bool:
        return self.profile is None and not self.requires and self.thread_mode is None

    def facts(self) -> dict[str, object]:
        """The set fields, as stored and hashed."""
        facts: dict[str, object] = {}
        if self.profile is not None:
            facts["profile"] = self.profile
        if self.requires:
            facts["requires"] = {
                cap: need.model_dump(mode="json") for cap, need in sorted(self.requires.items())
            }
        if self.thread_mode is not None:
            facts["thread_mode"] = self.thread_mode
        return facts

    def for_backend(self, backend: Provider | None) -> AgentSettings:
        """Keep what still applies on `backend`: a profile belongs to one backend only."""
        if self.profile is None or self.profile.startswith(f"{backend or DEFAULT_BACKEND}."):
            return self
        return AgentSettings(None, self.requires, self.thread_mode)


@dataclass(frozen=True)
class BackendSelectionState:
    agent: BackendAgent
    revision: AgentBackendRevision | None
    policy: TenantBackendPolicy
    preview: CompatibilityPreview


@dataclass(frozen=True)
class BackendResolution:
    source: Literal["channel", "agent", "default"]
    selection: ResolvedBackend
    revision: ConfigRevision | None = None
    agent_revision: int | None = None


class AgentBackendRefused(DaimonError):
    def __init__(
        self,
        reason: str,
        *,
        provider: Provider | None = None,
        missing: tuple[str, ...] = (),
        detail: str | None = None,
    ) -> None:
        self.reason = reason
        # The provider a `backend_not_allowed` refusal names, for its copy.
        self.provider = provider
        # `source_unsupported`: what the target can't carry (closed obligation labels).
        self.missing = missing
        # `source_unavailable`: which permanent cause, if not a transient read failure.
        self.detail = detail
        super().__init__(reason.replace("_", " "))


type BackendRefusalStage = Literal[
    "workspace_policy",
    "backend_capabilities",
    "backend_configuration",
    "backend_runnable",
    "admission_origin",
    "backend_origin",
    "retained_revision_lookup",
    "retained_default_mismatch",
    "retained_binding_mismatch",
    "retained_default_policy",
    "channel_policy",
    "preparation_counterpart",
    "source_agent_lookup",
    "agent_preference",
    "reauthorize_policy",
]


#: The capability names our own code raises in UnsupportedCapability (never request
#: data), so a refusal log can name them. An explicit list: any other name, even a
#: plain lowercase one (a future path passing a skill or agent name without its
#: "kind:" prefix), stays redacted. An Opus turn (f95639c4) failed with its
#: only clue redacted.
_RAISED_CAPABILITIES: frozenset[str] = frozenset(
    {
        "agent_delete",
        "agent_hard_delete",
        "agent_list_order",
        "agent_revision_pin",
        "artifact_input",
        "artifacts",
        "binary_inline_source",
        "cancel",
        "conflicting_runtime_price",
        "conversation_only_workspace",
        "custom_tools_or_tool_permissions",
        "daimon_tool_rules",
        "environment_list_order",
        "environment_sources",
        "event_history",
        "expected_turn",
        "explicit_send_decision",
        "file_turn_or_order_filter",
        "function_error_content",
        "function_result_action",
        "host_approval_root_identity",
        "host_cancel_root_identity",
        "host_confirmation_call_identity",
        "host_deployment_authorization",
        "host_initial_input_batch",
        "host_input_content",
        "host_input_image",
        "host_neutral_error_reducer",
        "host_one_root_per_turn",
        "host_origin_confirmation_identity",
        "host_output_content",
        "host_required_action_executor",
        "host_turn_backend",
        "host_turn_codec",
        "host_turn_input",
        "host_turn_input_batch",
        "input_content",
        "input_mode_precondition",
        "legacy_event_codec",
        "mcp_allowed_tools",
        "mcp_tool_policy",
        "mcp_tool_policy_scope",
        "memory_list_order",
        "memory_store_list_order",
        "memory_update_by_path",
        "model_controls",
        "model_discovery",
        "model_interaction_resources",
        "models",
        "mux_turn_path",
        "session_add_non_file_resource",
        "neutral_retained_skill_ownership",
        "neutral_retained_skill_bundle",
        "neutral_resource_host_provider",
        "neutral_resource_host_profile",
        "neutral_resource_host_after_g2",
        "neutral_concrete_skill_pin",
        "mcp_tool_policy_widens_allowlist",
        "native_authentication_action",
        "native_content_input",
        "origin_approval_action",
        "pinned_inline_skill",
        "session_archive",
        "session_create_initial_message",
        "session_delete",
        "session_export",
        "session_reads",
        "session_resource",
        "session_restore",
        "session_update_plan",
        "single_inline_skill",
        "skill_bundle_gcs_deployment",
        "skill_list_order",
        "skill_versions_order",
        "skills_bundle",
        "snapshot_artifact_mount",
        "source_representation",
        "steer",
        "stream_cursor",
        "turn_input",
        "typed_provider_action_surface",
        "unknown_cancel_root",
        "unknown_function_action",
        "unseeded_skill",
        "unseeded_skill_upload",
        "unseeded_usage",
        "usage",
        "vault_conditional_or_native_credential",
        "vault_list_order",
        "workspace_source",
    }
)
#: Provider-specific names our code raises: "<provider>_<suffix>" for these suffixes
#: only (kept free of provider literals; the provider comes from the contract).
_RAISED_PROVIDER_SUFFIXES: frozenset[str] = frozenset(
    {
        "changed_admission",
        "durable_accounting",
        "durable_runtime",
        "explicit_runtime",
        "host_controls_and_durable_state",
        "host_deployment_policy",
        "host_flash_primary",
        "host_input",
        "host_model",
        "host_runtime",
        "native_message_display",
        "native_required_action",
        "native_tool_result_display",
        "pinned_deployment",
        "private_transport",
        "resource_catalog_transport",
        "turn_codec",
    }
)


def _raised_capability(item: str) -> bool:
    from mux.contracts.ids import Provider
    from mux.profiles import PROFILES

    if item in _RAISED_CAPABILITIES:
        return True
    provider, _, suffix = item.partition("_")
    if provider in get_args(Provider) and suffix in _RAISED_PROVIDER_SUFFIXES:
        return True
    # A native extension namespace a profile declares.
    return any(
        extension.namespace == item
        for profile in PROFILES.values()
        for extension in profile.extensions
    )


_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")
#: SQLSTATE classes and codes a retry can clear: connection loss (08), a
#: serialization failure or deadlock (40001, 40P01), a lock not granted (55P03),
#: a statement cancelled by its timeout (57014), too many connections (53300), the
#: server shutting down or not yet up (57P01-57P03), an idle transaction timed out
#: (25P03).
_TRANSIENT_SQLSTATES = frozenset(
    {"40001", "40P01", "55P03", "57014", "53300", "57P01", "57P02", "57P03", "25P03"}
)


def exception_labels(error: BaseException) -> dict[str, object]:
    """Fixed, code-defined labels for any exception: never its message or args.

    `class` is the exception's module and qualified name (only for a class defined
    in code by that name); `frame` the innermost
    traceback frame in Daimon's or mux's own code (`module:function:line`);
    `sqlstate` a database error's five-character code, when it carries one.
    """
    kind = type(error)
    labels: dict[str, object] = {}
    # A class made at run time (or by a test) could carry any text in its name.
    if _importable_class(kind):
        labels["class"] = f"{kind.__module__}.{kind.__qualname__}"
    frame: str | None = None
    tb = error.__traceback__
    while tb is not None:
        module = tb.tb_frame.f_globals.get("__name__")
        if isinstance(module, str) and module.split(".", 1)[0] in ("daimon", "mux"):
            frame = f"{module}:{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}"
        tb = tb.tb_next
    if frame is not None:
        labels["frame"] = frame
    sqlstate = _sqlstate(error)
    if sqlstate is not None:
        labels["sqlstate"] = sqlstate
    return labels


def _importable_class(kind: type[BaseException]) -> bool:
    """Whether a class is defined in code by that name: its module, read by its
    qualified name, gives this very class. A class made at run time (whose name
    could be any text) or one from a test module is not."""
    import sys

    module = sys.modules.get(kind.__module__)
    if module is None or "tests" in kind.__module__.split(".") or "<" in kind.__qualname__:
        return False
    found: object = module
    for part in kind.__qualname__.split("."):
        found = getattr(found, part, None)
    return found is kind


def _sqlstate(error: BaseException) -> str | None:
    """A database error's SQLSTATE, read off the driver error SQLAlchemy wraps."""
    from sqlalchemy.exc import DBAPIError

    if not isinstance(error, DBAPIError):
        return None
    orig = error.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        for attribute in ("sqlstate", "pgcode"):
            code = getattr(candidate, attribute, None)
            if isinstance(code, str) and _SQLSTATE.match(code):
                return code
    return None


def _upstream_transient(error: BaseException) -> bool:
    """Whether a provider failure is one a retry can clear: a timeout or a lost
    connection, a 5xx or a 429, or a normalized provider error marked retryable.
    A 401/403/404 (a bad key, a missing resource) is not: retrying never fixes it,
    so its turn gives the reference instead of "busy, try again" forever."""
    import httpx
    from mux.errors import ProviderError

    pending: list[object] = [getattr(error, "cause", None), error.__cause__, error.__context__]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (TimeoutError, ConnectionError, httpx.TransportError)):
            return True
        if isinstance(current, ProviderError) and current.retryable:
            return True
        status = getattr(current, "status_code", None)
        if isinstance(status, int) and (status >= 500 or status == 429):
            return True
        if isinstance(current, BaseException):
            pending.extend((current.__cause__, current.__context__))
    return False


def transient_failure(error: BaseException | None) -> bool:
    """Whether a failure, or any exception in its chain, is one a retry can clear:
    a timeout, a database pool timeout, a lost connection or server restart, a
    lock/serialization conflict, a busy session or policy write, or a provider
    connection lost mid-way. Its turn says "busy, try again", never "not set up".

    The chain is the one a traceback shows: an explicit `__cause__`, else the
    implicit `__context__` unless `raise ... from None` suppressed it.
    """
    from daimon.core.errors import TurnError
    from daimon.core.session_fence_retry import FenceUnavailable
    from daimon.core.stores.access_policy import PolicyBusyError
    from daimon.core.turn.errors import SessionBusyError
    from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
    from sqlalchemy.exc import TimeoutError as PoolTimeoutError

    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(
            error,
            (
                TimeoutError,
                PoolTimeoutError,
                InterfaceError,
                SessionBusyError,
                PolicyBusyError,
                FenceUnavailable,
            ),
        ):
            return True
        if isinstance(error, TurnError) and (
            error.kind == "connection_lost"
            or (error.kind == "upstream" and _upstream_transient(error))
        ):
            return True
        if isinstance(error, DBAPIError):
            sqlstate = _sqlstate(error)
            if sqlstate is not None and (
                sqlstate in _TRANSIENT_SQLSTATES or sqlstate.startswith("08")
            ):
                return True
            if sqlstate is None and isinstance(error, OperationalError):
                return True
        error = (
            error.__cause__
            if error.__cause__ is not None
            else None
            if error.__suppress_context__
            else error.__context__
        )
    return False


def backend_refusal_causes(error: BaseException | None) -> list[dict[str, object]]:
    """One closed-label sanitizer for turn and background refusals."""
    from daimon.core.turn.errors import AdmissionDenialReason, AdmissionDenied

    safe_types: dict[type[BaseException], str] = {
        AgentBackendRefused: "AgentBackendRefused",
        AdmissionDenied: "AdmissionDenied",
        BackendUnsupported: "BackendUnsupported",
        InvalidConfig: "InvalidConfig",
        UnsupportedCapability: "UnsupportedCapability",
        ScopeViolation: "ScopeViolation",
        RuntimeError: "RuntimeError",
        ValueError: "ValueError",
        LookupError: "LookupError",
    }
    safe_reasons = {
        "actor_not_found",
        "admin_required",
        "agent_identity_required",
        "agent_not_found",
        "backend_not_allowed",
        "drops_mismatch",
        "drops_required",
        "feature_disabled",
        "no_previous_revision",
        "owner_or_admin_required",
        "admin_required_for_shared_threads",
        "profile_not_selectable",
        "channel_pin_lacks_capability",
        "re_confirm",
        "revision_busy",
        "source_context_unavailable",
        "source_unavailable",
        "source_unsupported",
        "stale_preview",
        "stale_revision",
        "tenant_mismatch",
        "backend_unsupported",
    }
    safe_reasons.update(get_args(AdmissionDenialReason))
    safe_missing = set(get_args(Capability)) | {
        "source_definition",
        "source_context",
        "source_drift",
        "native_counterpart",
        "instructions",
        "tools",
        "skills",
        "mcp",
        "memory",
        "environment",
        "sandbox",
        "credentials",
        "repository",
        "files",
    }
    causes: list[dict[str, object]] = []
    seen: set[int] = set()
    while error is not None and id(error) not in seen and len(causes) < 8:
        seen.add(id(error))
        cause: dict[str, object] = {"type": safe_types.get(type(error), "unlisted")}
        if type(error) not in safe_types:
            # An unlisted type is still named by code-defined labels, never text.
            cause.update(exception_labels(error))
        elif isinstance(error, (RuntimeError, ValueError, LookupError)):
            # A generic type says little on its own: add where it was raised.
            cause.update({k: v for k, v in exception_labels(error).items() if k != "class"})
        if isinstance(error, (AgentBackendRefused, AdmissionDenied)):
            cause["reason"] = error.reason if error.reason in safe_reasons else "redacted"
        if isinstance(error, AdmissionDenied) and error.site is not None:
            cause["site"] = error.site  # Our own module.function:line, never request data.
        if isinstance(error, UnsupportedCapability):
            cause["missing"] = sorted(
                {
                    item if item in safe_missing or _raised_capability(item) else "redacted"
                    for item in error.missing
                }
            )
        causes.append(cause)
        error = error.__cause__ or error.__context__
    return causes


def log_backend_unsupported(
    stage: BackendRefusalStage,
    error: BaseException | None = None,
    *,
    reason: Literal[
        "backend_not_enabled",
        "profile_not_runnable",
        "selfhosted_admission_disabled",
        "channel_only_model",
        "tariff_unavailable",
    ]
    | None = None,
    revision: ConfigRevision | None = None,
) -> None:
    """Log closed diagnostic labels, never provider messages or source payloads."""
    causes = backend_refusal_causes(error)
    fields: dict[str, object] = {}
    if reason is not None:
        fields["reason"] = (
            reason
            if reason
            in {
                "backend_not_enabled",
                "profile_not_runnable",
                "selfhosted_admission_disabled",
                "channel_only_model",
                "tariff_unavailable",
            }
            else "redacted"
        )
    if revision is not None:
        fields.update(
            channel_id=revision.channel.channel_id,
            profile=revision.profile if revision.profile in PROFILES else "redacted",
        )
    log.info(
        "turn.backend_unsupported",
        stage=stage if stage in get_args(BackendRefusalStage.__value__) else "redacted",
        causes=causes,
        **fields,
    )


def _digest(facts: object) -> str:
    return hashlib.sha256(
        json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _revision_digest(
    agent: BackendAgent,
    backend: Provider | None,
    drop_consent: JsonValue | None = None,
    model: str | None = None,
    settings: AgentSettings | None = None,
) -> str:
    facts: dict[str, object] = {
        "tenant_id": agent.tenant_id,
        "agent_id": agent.agent_id,
        "backend": backend,
    }
    if model is not None:
        facts["model"] = model
    if settings is not None:
        # Only set fields are hashed, so a revision without them keeps its digest.
        facts.update(settings.facts())
    if drop_consent is not None:
        facts["drop_consent"] = drop_consent
    return _digest(facts)


def spec_compatibility_items(
    spec: AgentSpec | BetaManagedAgentsAgent, *, skill_providers: tuple[Provider, ...] = PROVIDERS
) -> tuple[Iterator[CompatibilityItem], Iterator[CompatibilityItem]]:
    """Map source skills and MCP requirements once for preview and target preparation."""
    if isinstance(spec, AgentSpec):
        skill_names = (pin.id for pin in spec.skills or ())
        servers = ((server.name, bool(server.credential_ref)) for server in spec.mcp_servers or ())
    else:
        skill_names = (pin.skill_id for pin in spec.skills or ())
        # The SDK does not expose vault credential intent; retain its conservative requirement.
        servers = ((server.name, True) for server in spec.mcp_servers or ())
    return (
        (
            CompatibilityItem(name=name, requires=("skills_bundle",), providers=skill_providers)
            for name in skill_names
        ),
        (
            CompatibilityItem(
                name=name,
                requires=("tool_loop", "required_actions", *(("vaults",) if credential else ())),
            )
            for name, credential in servers
        ),
    )


def compatibility_missing(
    item: CompatibilityItem, target_backend: Provider, profile: Profile
) -> bool:
    """One provider/capability predicate for preview and target preparation."""
    return target_backend not in item.providers or any(
        profile.support_for(cap) not in SUPPORTED for cap in item.requires
    )


def compatibility_preview(
    agent: BackendAgent,
    target_backend: Provider,
    tenant: TenantBackendPolicy,
    *,
    setting_revision: int = 0,
    cost_line: str = "Cost preview pending",
    model: str | None = None,
    settings: AgentSettings | None = None,
) -> CompatibilityPreview:
    """Pure preview from host-resolved agent facts and declared profile metadata."""
    if agent.tenant_id != tenant.tenant_id:
        raise AgentBackendRefused("tenant_mismatch")
    if target_backend == "selfhosted":
        raise AgentBackendRefused("selfhosted_requires_channel_configuration")
    selection = agent_selection(target_backend, model, settings)
    profile = get_profile(selection.profile)

    preview = CompatibilityPreview(
        digest="",
        tenant_id=agent.tenant_id,
        agent_id=agent.agent_id,
        target_backend=target_backend,
        setting_revision=setting_revision,
        policy_revision=tenant.revision,
        unsupported_skills=tuple(
            sorted(
                i.name for i in agent.skills if compatibility_missing(i, target_backend, profile)
            )
        ),
        unsupported_mcps=tuple(
            sorted(i.name for i in agent.mcps if compatibility_missing(i, target_backend, profile))
        ),
        unsupported_capabilities=tuple(
            sorted(
                c
                for c in set(agent.required_capabilities) | set(_required(settings))
                if compatibility_missing(
                    CompatibilityItem(name=c, requires=(c,)), target_backend, profile
                )
            )
        ),
        shared_thread_limits=("Shared threads are unavailable on this selection.",)
        if not shared_threads_available(target_backend)
        else ("Shared threads stay per caller wherever a channel pins per-caller threads.",)
        if selection.thread_mode == "shared"
        else (),
        cost_line=cost_line,
        allowed=backend_is_enabled(target_backend, tenant.enabled_backends),
        target_model=model if model != backend_selection(target_backend).model else None,
        target_settings=cast(dict[str, JsonValue], settings.facts())
        if settings is not None and not settings.is_default()
        else None,
    )
    digest = _digest(
        {
            **preview.model_dump(mode="json"),
            "configuration_digest": agent.configuration_digest,
            "profile": profile.model_dump(mode="json"),
        }
    )
    return preview.model_copy(update={"digest": digest})


def resolve_agent_backend(
    channel: ConfigRevision | None, agent: AgentBackendRevision | None, *, enabled: bool = True
) -> ResolvedBackend:
    if not enabled:
        return resolve_default(None)
    if channel is not None:
        return ResolvedBackend.model_validate(
            channel.model_dump(include=set(ResolvedBackend.model_fields))
        )
    return revision_selection(agent) or resolve_default(None)


def backend_selection_enabled(settings: TurnSettings) -> bool:
    return settings.path == "mux" and settings.channel_backends


class AgentBackendService:
    """Agent preferences build through channel admission, policy and audit owners."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        enabled: bool = False,
        cost_line: Callable[[BackendAgent, Provider], str] | None = None,
        capture_source: Callable[[BackendAgent], Awaitable[BackendAgent]] | None = None,
    ) -> None:
        self._sessions = sessionmaker
        self._capture_source = capture_source
        self.enabled = enabled
        self._cost_line: Callable[[BackendAgent, Provider], str] = cost_line or _default_cost

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise AgentBackendRefused("feature_disabled")

    def _check_actor(self, tenant_id: uuid.UUID, actor: BackendActor) -> None:
        self._require_enabled()
        if actor.tenant_id != tenant_id:
            raise AgentBackendRefused("tenant_mismatch")
        if not authorize(OPEN_ACCESS_POLICY, subject=actor.subject, action=Action.SET_AGENT_RULE):
            raise AgentBackendRefused("admin_required")

    async def _actor(
        self, session: AsyncSession, tenant_id: uuid.UUID, actor: BackendActor
    ) -> None:
        self._check_actor(tenant_id, actor)
        policy = await policy_store.load_access_policy(session, tenant_id=tenant_id)
        if not authorize(policy, subject=actor.subject, action=Action.SET_AGENT_RULE):
            raise AgentBackendRefused("admin_required")
        if not await agent_backends.actor_exists(session, tenant_id, actor.account_id):
            raise AgentBackendRefused("actor_not_found")

    def _check_editor(self, tenant_id: uuid.UUID, actor: BackendActor) -> None:
        """The cheap half of `_authorize_edit`, before any transaction opens."""
        self._require_enabled()
        if actor.tenant_id != tenant_id:
            raise AgentBackendRefused("tenant_mismatch")

    async def _authorize_edit(
        self, session: AsyncSession, agent: BackendAgent, actor: BackendActor
    ) -> bool:
        """Owner or current server admin, decided inside the write's transaction.

        The owner row is read locked, so an owner change cannot interleave with
        a write it no longer allows. An agent key or a client is never an owner.
        Tenant-wide settings (the provider allowlist) stay admin-only: they use
        `_actor`, never this. Returns whether the actor is a current admin.
        """
        self._check_editor(agent.tenant_id, actor)
        if not await agent_backends.actor_exists(session, agent.tenant_id, actor.account_id):
            raise AgentBackendRefused("actor_not_found")
        policy = await policy_store.load_access_policy(session, tenant_id=agent.tenant_id)
        if authorize(policy, subject=actor.subject, action=Action.SET_AGENT_RULE):
            return True
        if not await may_edit_as_owner(session, agent.tenant_id, agent.agent_id, actor):
            raise AgentBackendRefused("owner_or_admin_required")
        return False

    async def may_edit(self, agent: BackendAgent, actor: BackendActor) -> bool:
        """Whether `actor` may change this agent now: a read for previews and cards."""
        try:
            async with self._sessions() as session:
                await self._authorize_edit(session, agent, actor)
        except AgentBackendRefused:
            return False
        return True

    async def _policy(self, session: AsyncSession, tenant_id: uuid.UUID) -> TenantBackendPolicy:
        policy = await policy_store.load_access_policy(session, tenant_id=tenant_id)
        return TenantBackendPolicy(
            tenant_id=tenant_id,
            revision=_digest(policy.model_dump(mode="json")),
            enabled_backends=policy.enabled_backends,
            explicit=policy.backends_explicit,
        )

    async def tenant_policy(self, tenant_id: uuid.UUID) -> TenantBackendPolicy:
        async with self._sessions() as session:
            return await self._policy(session, tenant_id)

    async def current(self, agent: BackendAgent) -> AgentBackendRevision | None:
        if not self.enabled:
            return None
        async with self._sessions() as session:
            return await agent_backends.current(session, agent.tenant_id, agent.agent_id)

    async def revision(self, agent: BackendAgent, local: int) -> AgentBackendRevision | None:
        self._require_enabled()
        async with self._sessions() as session:
            return await agent_backends.current(
                session, agent.tenant_id, agent.agent_id, local=local
            )

    async def _preview(
        self,
        session: AsyncSession,
        agent: BackendAgent,
        target: Provider | None,
        model: str | None = None,
        settings: AgentSettings | None = None,
    ) -> CompatibilityPreview:
        capture = self._capture_source if target is not None else None
        current = await agent_backends.current(session, agent.tenant_id, agent.agent_id)
        if settings is None:
            # A provider change keeps the agent's settings that still apply.
            settings = AgentSettings.of(current).for_backend(target)
        target = target or DEFAULT_BACKEND
        if target == "selfhosted":
            raise AgentBackendRefused("selfhosted_requires_channel_configuration")
        original = agent
        if capture is not None:
            agent = await capture(agent)
            if (agent.tenant_id, agent.agent_id) != (original.tenant_id, original.agent_id):
                raise AgentBackendRefused("tenant_mismatch")
        preview = compatibility_preview(
            agent,
            target,
            await self._policy(session, agent.tenant_id),
            setting_revision=current.local if current else 0,
            cost_line=self._cost_line(agent, target)
            if model is None
            else _default_cost(agent, target, model=model),
            model=model,
            settings=settings,
        )

        if capture is None:
            return preview
        from daimon.core.turn.runtime_manifest import preview_source_drops, source_preservation_pin

        selection = agent_selection(target, model, settings)
        try:
            proposed = preview_source_drops(
                agent, ModelRef(provider=selection.backend, id=selection.model or agent.model or "")
            )
        except UnsupportedCapability as error:
            raise AgentBackendRefused(
                "source_unsupported", provider=target, missing=tuple(error.missing)
            ) from error
        consent = {
            "declared_drops": proposed.declared_drops,
            "source_pin": source_preservation_pin(agent),
            "target_digest": request_digest(proposed.model_dump(mode="json")),
        }
        preview = CompatibilityPreview.model_validate(
            {**preview.model_dump(), "drop_consent": consent}
        )
        return preview.model_copy(
            update={
                "digest": _digest(
                    {
                        "preview": preview.model_dump(mode="json"),
                        "configuration_digest": agent.configuration_digest,
                    }
                )
            }
        )

    async def preview(
        self,
        agent: BackendAgent,
        target_backend: Provider | None = None,
        model: str | None = None,
        settings: AgentSettings | None = None,
    ) -> CompatibilityPreview:
        """Preview a change. `settings` None keeps the agent's current ones that still apply."""
        self._require_enabled()
        model = picked_model(target_backend, model)
        async with self._sessions() as session:
            return await self._preview(session, agent, target_backend, model, settings)

    async def preview_settings(
        self, agent: BackendAgent, settings: AgentSettings
    ) -> CompatibilityPreview:
        """Preview new settings on the agent's current provider and model."""
        self._require_enabled()
        async with self._sessions() as session:
            current = await agent_backends.current(session, agent.tenant_id, agent.agent_id)
            return await self._preview(
                session,
                agent,
                current.backend if current else None,
                current.model if current else None,
                settings,
            )

    async def read_selection(
        self,
        client: AsyncAnthropic,
        *,
        tenant_id: uuid.UUID,
        agent_name: str,
        target_backend: Provider | None = None,
        resource_host: NeutralResourceHost | None = None,
    ) -> BackendSelectionState | None:
        """Read an optional panel selection, preserving strict mutation refusals."""
        if not self.enabled:
            return None
        if target_backend == "selfhosted":
            return None
        try:
            agent = await load_backend_agent(
                client,
                self._sessions,
                tenant_id=tenant_id,
                agent_name=agent_name,
                resource_host=resource_host,
            )
            return BackendSelectionState(
                agent=agent,
                revision=await self.current(agent),
                policy=await self.tenant_policy(tenant_id),
                preview=await self.preview(agent, target_backend),
            )
        except AgentBackendRefused:
            return None

    async def _change(
        self,
        agent: BackendAgent,
        backend: Provider | None,
        expected_revision: int,
        actor: BackendActor,
        operation: Literal[
            "set",
            "clear",
            "revert",
            "settings",
        ],
        expected_preview_digest: str | None,
        *,
        accepted_drops: tuple[str, ...] | None = None,
        now: datetime | None = None,
        model: str | None = None,
        settings: AgentSettings | None = None,
    ) -> AgentBackendRevision:
        self._check_editor(agent.tenant_id, actor)
        async with policy_store.policy_write_transaction(
            self._sessions, tenant_id=agent.tenant_id
        ) as session:
            admin = await self._authorize_edit(session, agent, actor)
            current = await agent_backends.current(session, agent.tenant_id, agent.agent_id)
            kept = AgentSettings.of(current)
            if operation == "settings":
                # A settings change keeps the provider and model and states the rest anew.
                backend = current.backend if current else None
                model = current.model if current else None
                settings = settings or AgentSettings()
            elif operation in ("set", "clear"):
                # Stated settings replace the agent's; otherwise those that still apply stay.
                settings = settings if settings is not None else kept.for_backend(backend)
            else:
                settings = kept
            local = current.local if current else 0
            if local != expected_revision:
                raise AgentBackendRefused("stale_revision")
            previous = None
            if operation == "revert":
                if current is None:
                    raise AgentBackendRefused("no_previous_revision")
                previous = (
                    await agent_backends.current(
                        session, agent.tenant_id, agent.agent_id, local=current.previous_local
                    )
                    if current.previous_local
                    else None
                )
                if current.previous_local is not None and previous is None:
                    raise AgentBackendRefused("no_previous_revision")
                backend = previous.backend if previous else None
                model = previous.model if previous else None
                settings = AgentSettings.of(previous)
            if settings.thread_mode == "shared" and kept.thread_mode != "shared" and not admin:
                # Sharing applies in every channel the agent answers in unpinned, so
                # it stays an admin's call until a destination can consent to it.
                # TODO: a per-channel setting allowing shared threads, then owners too.
                raise AgentBackendRefused("admin_required_for_shared_threads")
            preview = await self._preview(session, agent, backend, model, settings)
            if not preview.allowed:
                raise AgentBackendRefused("backend_not_allowed")
            previous_local = local or None
            consent = preview.drop_consent
            if (
                operation == "revert"
                and (previous.drop_consent if previous is not None else None) != consent
            ):
                raise AgentBackendRefused("re_confirm")
            if consent is not None:
                if operation != "revert":
                    if accepted_drops is None and consent.declared_drops:
                        raise AgentBackendRefused("drops_required")
                    try:
                        accepted = type(consent).model_validate(
                            {
                                **consent.model_dump(),
                                "declared_drops": accepted_drops
                                if accepted_drops is not None
                                else (),
                            }
                        )
                    except ValueError:
                        raise AgentBackendRefused("drops_mismatch") from None
                    if accepted != consent:
                        raise AgentBackendRefused("drops_mismatch")
                if expected_preview_digest is None:
                    raise AgentBackendRefused("stale_preview")
            elif accepted_drops not in (None, ()):
                raise AgentBackendRefused("source_unavailable")
            consent_json = consent.model_dump(mode="json") if consent is not None else None
            if expected_preview_digest is not None and (preview.digest != expected_preview_digest):
                raise AgentBackendRefused("stale_preview")
            # Validation is the same owner used for explicit channel selections.
            check_backend(
                ConfigRevision.create(
                    ChannelRef(
                        tenant_id=str(agent.tenant_id), platform="cli", channel_id=agent.name
                    ),
                    1,
                    agent_selection(preview.target_backend, model, settings),
                )
            )
            try:
                revision = await agent_backends.put_revision(
                    session,
                    tenant_id=agent.tenant_id,
                    agent_id=agent.agent_id,
                    agent_name=agent.name,
                    local=local + 1,
                    backend=backend,
                    previous_local=previous_local,
                    digest=_revision_digest(agent, backend, consent_json, model, settings),
                    preview_digest=preview.digest,
                    drop_consent=consent_json,
                    model=model,
                    profile=settings.profile,
                    requires=cast(JsonValue, settings.facts().get("requires")),
                    thread_mode=settings.thread_mode,
                )
            except IntegrityError:
                raise AgentBackendRefused("stale_revision") from None
            await security_audit.append_event(
                session,
                tenant_id=agent.tenant_id,
                account_id=actor.account_id,
                agent_id=agent.agent_id,
                platform=None,
                platform_user_id=None,
                tool_name="agent_backend",
                operation=operation,
                outcome="allowed",
                reason="completed"
                + (
                    "; declared_drops=" + ",".join(revision.declared_drops)
                    if revision.declared_drops
                    else ""
                ),
                scope=f"agent_backend:{agent.agent_id}:{revision.local}",
            )
            return revision

    async def set_backend(
        self,
        agent: BackendAgent,
        target_backend: Provider,
        expected_revision: int,
        actor: BackendActor,
        *,
        expected_preview_digest: str | None = None,
        accepted_drops: tuple[str, ...] | None = None,
        model: str | None = None,
        settings: AgentSettings | None = None,
    ) -> AgentBackendRevision:
        """Switch the provider. `settings` None keeps the agent's settings that still apply."""
        model = picked_model(target_backend, model)
        return await self._change(
            agent,
            target_backend,
            expected_revision,
            actor,
            "set",
            expected_preview_digest,
            accepted_drops=accepted_drops,
            model=model,
            settings=settings,
        )

    async def clear_backend(
        self,
        agent: BackendAgent,
        expected_revision: int,
        actor: BackendActor,
        *,
        expected_preview_digest: str | None = None,
    ) -> AgentBackendRevision:
        return await self._change(
            agent, None, expected_revision, actor, "clear", expected_preview_digest
        )

    async def set_settings(
        self,
        agent: BackendAgent,
        settings: AgentSettings,
        expected_revision: int,
        actor: BackendActor,
        *,
        expected_preview_digest: str | None = None,
        accepted_drops: tuple[str, ...] | None = None,
    ) -> AgentBackendRevision:
        """State the agent's profile, requirements and thread mode; provider and model stay.

        On a provider that drops source features, `accepted_drops` must name them
        again, as for a provider switch.
        """
        return await self._change(
            agent,
            None,
            expected_revision,
            actor,
            "settings",
            expected_preview_digest,
            settings=settings,
            accepted_drops=accepted_drops,
        )

    async def revert(
        self,
        agent: BackendAgent,
        actor: BackendActor,
        *,
        expected_revision: int,
        expected_preview_digest: str | None = None,
    ) -> AgentBackendRevision:
        return await self._change(
            agent, None, expected_revision, actor, "revert", expected_preview_digest
        )

    async def audit(
        self,
        tenant_id: uuid.UUID,
        *,
        agent_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[security_audit.SecurityAuditRow, ...]:
        self._require_enabled()
        async with self._sessions() as session:
            rows = await security_audit.list_events(
                session,
                tenant_id=tenant_id,
                agent_id=agent_id,
                tool_name="agent_backend",
                limit=limit,
                offset=offset,
            )
        return tuple(rows)

    async def set_tenant_allowlist(
        self,
        tenant_id: uuid.UUID,
        backends: tuple[Provider, ...] | None,
        expected_revision: str,
        actor: BackendActor,
        *,
        dry_run: bool = False,
    ) -> TenantBackendPolicy:
        """Save the admin's provider list; None goes back to the deployment default."""
        self._check_actor(tenant_id, actor)
        async with policy_store.policy_write_transaction(
            self._sessions, tenant_id=tenant_id
        ) as session:
            await self._actor(session, tenant_id, actor)
            current = await self._policy(session, tenant_id)
            if current.revision != expected_revision:
                raise AgentBackendRefused("stale_revision")
            policy = await policy_store.load_access_policy(session, tenant_id=tenant_id)
            updated = type(policy).model_validate(
                {
                    **policy.model_dump(),
                    "enabled_backends": backends
                    if backends is not None
                    else default_enabled_backends(),
                    "backends_explicit": backends is not None,
                }
            )
            if dry_run:
                return current.model_copy(update={"enabled_backends": updated.enabled_backends})
            await policy_store.set_access_policy(session, tenant_id=tenant_id, policy=updated)
            await security_audit.append_event(
                session,
                tenant_id=tenant_id,
                account_id=actor.account_id,
                agent_id=None,
                platform=None,
                platform_user_id=None,
                tool_name="agent_backend",
                operation="allowlist",
                outcome="allowed",
                reason="completed",
            )
            return await self._policy(session, tenant_id)

    async def resolve(
        self, channel: ChannelRef, agent: BackendAgent, *, retained_revision: int | None = None
    ) -> BackendResolution:
        if not self.enabled:
            return BackendResolution("default", resolve_default(None))
        from daimon.core.session_fence_retry import retry_fences

        # resolve_turn_backend's shared policy fence is one try-lock: a concurrent
        # policy writer is waited out (each miss rolls back and re-reads), never a failure.
        async def attempt() -> tuple[
            ConfigRevision | None, bool, Literal["channel", "agent", "default"]
        ]:
            async with self._sessions.begin() as session:
                return await resolve_turn_backend(
                    session,
                    channel,
                    agent,
                    enabled=self.enabled,
                    retained_revision=retained_revision,
                )

        revision, _shared, source = await retry_fences(
            attempt, busy_error=policy_store.PolicyBusyError
        )
        return BackendResolution(source, resolve_agent_backend(revision, None), revision)


def make_backend_service(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: TurnSettings,
    *,
    cost_line: Callable[[BackendAgent, Provider], str] | None = None,
    prices: Mapping[str, ProviderPrice] | None = None,
    capture_source: Callable[[BackendAgent], Awaitable[BackendAgent]] | None = None,
) -> AgentBackendService:
    def configured_cost(agent: BackendAgent, target: Provider) -> str:
        return _default_cost(agent, target, prices=prices)

    return AgentBackendService(
        sessionmaker,
        enabled=backend_selection_enabled(settings),
        cost_line=cost_line or configured_cost,
        capture_source=capture_source,
    )


class _ConsentOriginValidator(Protocol):
    def __call__(
        self,
        selection: ResolvedBackend,
        origin: Literal["channel", "agent"],
        revision: ConfigRevision | None,
        /,
        *,
        agent_revision: AgentBackendRevision,
    ) -> Awaitable[None] | None: ...


async def resolve_turn_backend(
    session: AsyncSession,
    channel: ChannelRef,
    agent: BackendAgent | None,
    *,
    enabled: bool,
    retained_revision: int | None = None,
    validate_origin: Callable[
        [ResolvedBackend, Literal["channel", "agent"], ConfigRevision | None],
        Awaitable[None] | None,
    ]
    | None = None,
) -> tuple[ConfigRevision | None, bool, Literal["channel", "agent", "default"]]:
    """Resolve and retain through the existing immutable channel revision namespace."""
    if not enabled or retained_revision == 0:
        return None, False, "default"
    if retained_revision is not None:
        retained = await mux_state.get_config_revision(session, channel, retained_revision)
        origin = await mux_state.config_origin(session, channel, retained_revision)
        if retained is None or origin not in ("channel", "channel_clear", "agent"):
            raise AgentBackendRefused("stale_revision")
        source: Literal["channel", "agent"] = "agent" if origin == "agent" else "channel"
        if source == "agent" and retained.backend == "selfhosted":
            raise AgentBackendRefused("selfhosted_requires_channel_configuration")
        return retained, retained.thread_mode == "shared", source
    if not isinstance(agent, BackendAgent):
        raise AgentBackendRefused("agent_identity_required")
    tenant_id = uuid.UUID(channel.tenant_id)
    if agent.tenant_id != tenant_id:
        raise AgentBackendRefused("tenant_mismatch")
    await policy_store.lock_policy_writes_shared(session, tenant_id=tenant_id)
    override, ever_shared = await current_backend_and_sharing(session, channel)
    setting: AgentBackendRevision | None = None
    setting = await agent_backends.current(session, tenant_id, agent.agent_id)
    if (
        override is not None
        and await mux_state.config_origin(session, channel, override.local) != "channel_clear"
    ):
        require_pin_meets_agent(override, setting)
        return override, ever_shared, "channel"
    agent_choice = revision_selection(setting)
    if agent_choice is None:
        return None, ever_shared, "default"
    selection = agent_choice
    if selection.backend == "selfhosted":
        raise AgentBackendRefused("selfhosted_requires_channel_configuration")
    consent_revision = setting if setting is not None and setting.drop_consent is not None else None
    if consent_revision is not None:
        if validate_origin is None:
            raise AgentBackendRefused("source_unavailable")
        validate_origin = partial(
            cast(_ConsentOriginValidator, validate_origin), agent_revision=consent_revision
        )
    policy = await policy_store.load_access_policy(session, tenant_id=tenant_id)
    if not backend_is_enabled(selection.backend, policy.enabled_backends):
        raise AgentBackendRefused("backend_not_allowed", provider=selection.backend)
    if validate_origin is not None:
        checking = validate_origin(selection, "agent", None)
        if inspect.isawaitable(checking):
            await checking
    check_backend(
        ConfigRevision.create(channel, 1, selection),
    )
    if retained_revision is not None:
        retained = await mux_state.get_config_revision(session, channel, retained_revision)
        if (
            retained is None
            or await mux_state.config_origin(session, channel, retained_revision) != "agent"
            or retained.content_digest() != selection.content_digest()
        ):
            raise AgentBackendRefused("stale_revision")
        if validate_origin is not None:
            checking = validate_origin(selection, "agent", retained)
            if inspect.isawaitable(checking):
                await checking
        return retained, ever_shared, "agent"
    for _ in range(3):
        effective = await mux_state.effective_revision(
            session, channel, selection.content_digest(), "agent", agent_revision=consent_revision
        )
        if effective is not None:
            if validate_origin is not None:
                checking = validate_origin(selection, "agent", effective)
                if inspect.isawaitable(checking):
                    await checking
            return effective, ever_shared, "agent"
        effective = ConfigRevision.create(
            channel, await mux_state.next_config_local(session, channel), selection
        )
        if validate_origin is not None:
            checking = validate_origin(selection, "agent", effective)
            if inspect.isawaitable(checking):
                await checking
        try:
            await mux_state.put_config_revision(
                session, effective, origin="agent", agent_revision=consent_revision
            )
        except ConfigRevisionConflict:
            continue
        return effective, ever_shared, "agent"
    raise AgentBackendRefused("revision_busy")


async def answers_for_everyone(
    sessionmaker: async_sessionmaker[AsyncSession],
    agent: BackendAgent,
    *,
    default_agent_name: str | None,
) -> bool:
    """Whether switching `agent` changes it for everyone on the server.

    The built-in agent (defaults-managed, or the deployment's default) and the
    server's own default agent answer every channel that picks no other.
    """
    from daimon.core.agent_identity import is_builtin_agent
    from daimon.core.scope import TenantScopeRef
    from daimon.core.stores.scoped_config_read import get_scope

    metadata = agent.source.spec.metadata if agent.source is not None else None
    if is_builtin_agent(name=agent.name, metadata=metadata, default_agent_name=default_agent_name):
        return True
    async with sessionmaker() as session:
        tenant = await get_scope(session, scope=TenantScopeRef(tenant_id=agent.tenant_id))
    return tenant is not None and tenant.agent_name == agent.name


async def load_backend_agent(
    anthropic: AsyncAnthropic,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: uuid.UUID,
    agent_name: str,
    resource_host: NeutralResourceHost | None = None,
    actual_agent: BetaManagedAgentsAgent | None = None,
) -> BackendAgent:
    """Read actual tenant-scoped resources at the same edge used by skill edits."""
    if resource_host is not None:
        if resource_host.scope.tenant_id != str(tenant_id):
            raise AgentBackendRefused("tenant_mismatch")
        view = await resource_host.find(agent_name)
        if view is None:
            raise AgentBackendRefused("agent_not_found")
        row = await resource_host.read(view.revision.catalog_id)
        actual = row.agent
        skills, mcps = (tuple(items) for items in spec_compatibility_items(actual.spec))
        facts = actual.spec.model_dump(mode="json")
        return BackendAgent(
            tenant_id=tenant_id,
            agent_id=derive_agent_uuid(
                tenant_id=tenant_id,
                ma_agent_id="catalog:"
                + json.dumps(
                    [
                        resource_host.scope.account_id,
                        resource_host.backend.capabilities().provider,
                        resource_host.workspace,
                        row.catalog_id,
                    ],
                    separators=(",", ":"),
                ),
            ),
            name=agent_name,
            model=actual.spec.model.id,
            configuration_digest=_digest(
                {
                    "ref": actual.ref.model_dump(mode="json"),
                    "revision": actual.revision.model_dump(mode="json"),
                    "spec": facts,
                }
            ),
            skills=skills,
            mcps=mcps,
            required_capabilities=_native_agent_requirements(actual.spec),
            source=actual,
        )
    actual = actual_agent
    if actual is not None:
        metadata = actual.metadata or {}
        if metadata.get(MA_METADATA_KEY_TENANT) != str(tenant_id):
            raise AgentBackendRefused("tenant_mismatch")
        if metadata.get(MA_METADATA_KEY_NAME) != agent_name or actual.archived_at is not None:
            raise AgentBackendRefused("agent_not_found")
    else:
        actual = await find_agent_by_daimon_tag(anthropic, tenant_id=tenant_id, name=agent_name)
    if actual is None:
        raise AgentBackendRefused("agent_not_found")
    # Native SDK projection is hashed only; neither credentials nor URLs leave this edge.
    facts = actual.model_dump(mode="json")
    skills, mcps = (tuple(items) for items in spec_compatibility_items(actual))
    return BackendAgent(
        tenant_id=tenant_id,
        agent_id=derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=actual.id),
        name=agent_name,
        model=actual.model.id if not isinstance(actual.model, str) else actual.model,
        configuration_digest=_digest(facts),
        skills=skills,
        mcps=mcps,
        required_capabilities=("multiagent",) if actual.multiagent is not None else (),
        source=source_agent_record(actual, resource_scope(tenant_id=str(tenant_id))),
    )


def backend_is_enabled(backend: Provider, enabled_backends: tuple[Provider, ...]) -> bool:
    """One tenant-allowlist rule used by preview, writes and admission."""
    return backend in enabled_backends


def agent_selection(
    backend: Provider, model: str | None = None, settings: AgentSettings | None = None
) -> ResolvedBackend:
    """An agent's provider selection: a picked model or the provider default, and its settings."""
    selection = backend_selection(backend)
    if model is not None and model != selection.model:
        if backend == DEFAULT_BACKEND or model not in selectable_models(backend):
            raise AgentBackendRefused("model_not_selectable")
        selection = selection.model_copy(update={"model": model})
    if settings is None or settings.is_default():
        return selection
    update: dict[str, object] = {}
    if settings.profile is not None and settings.profile != selection.profile:
        if not settings.profile.startswith(f"{backend}.") or settings.profile not in (
            RUNNABLE_PROFILES
        ):
            raise AgentBackendRefused("profile_not_selectable")
        update["profile"] = settings.profile
    if settings.requires:
        update["requires"] = dict(settings.requires)
    if settings.thread_mode is not None:
        update["thread_mode"] = settings.thread_mode
    try:
        return ResolvedBackend.model_validate({**dict(selection), **update})
    except (ValueError, InvalidConfig):
        raise AgentBackendRefused("profile_not_selectable") from None


def revision_selection(revision: AgentBackendRevision | None) -> ResolvedBackend | None:
    """The agent's own selection, or None when it states nothing (the legacy default path)."""
    if revision is None:
        return None
    settings = AgentSettings.of(revision)
    if revision.backend is None and settings.is_default():
        return None
    return agent_selection(revision.backend or DEFAULT_BACKEND, revision.model, settings)


def require_pin_meets_agent(pin: ConfigRevision, setting: AgentBackendRevision | None) -> None:
    """A channel pin may tighten an agent's settings, never drop a capability it requires.

    The turn is refused visibly instead of running without it.
    """
    if setting is None or not setting.requires:
        return
    profile = get_profile(pin.profile)
    if any(
        need.level == "required" and profile.support_for(cap) not in SUPPORTED
        for cap, need in setting.requires.items()
    ):
        log_backend_unsupported("channel_policy", revision=pin)
        raise AgentBackendRefused("channel_pin_lacks_capability")


async def may_edit_as_owner(
    session: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID, actor: BackendActor
) -> bool:
    """Whether `actor` is a person who owns the agent. Agent keys and clients never are."""
    if actor.subject.via_agent_key or actor.subject.client:
        return False
    owner = await agent_owners.owner_of(session, tenant_id, agent_id, lock=True)
    return owner is not None and owner == actor.account_id


def _required(settings: AgentSettings | None) -> tuple[Capability, ...]:
    if settings is None or not settings.requires:
        return ()
    return tuple(cap for cap, need in settings.requires.items() if need.level == "required")


def picked_model(backend: Provider | None, model: str | None) -> str | None:
    """`model` if it is a selectable non-default; None for the provider default.

    The default is stored and previewed as no model, as before models could be
    picked, so naming it changes no digest.
    """
    if model is None or backend is None:
        return None
    return (
        model if agent_selection(backend, model).model != backend_selection(backend).model else None
    )


def _default_cost(
    agent: BackendAgent,
    target: Provider,
    *,
    prices: Mapping[str, ProviderPrice] | None = None,
    model: str | None = None,
) -> str:
    if model is not None:
        # An admin-picked model is priced from the provider tariff it runs on.
        tariff = provider_prices(agent_selection(target, model).profile).get((target, model))
        return list_price_preview(model, tariff or MODEL_PRICING.get(model))
    model = backend_selection(target).model or agent.model
    if model is None:
        return "List price unavailable: agent model unknown. Worst case per reply: unknown."
    if target == DEFAULT_BACKEND and agent.source is not None:
        native_model = agent.source.spec.model
        if native_model.provider == DEFAULT_BACKEND and native_model.id == model:
            model = model_choice_id({"id": model, **dict(native_model.options)})
    rates = (prices or {}).get(model) or MODEL_PRICING.get(model)
    return list_price_preview(model, rates)


def _native_agent_requirements(spec: AgentSpec) -> tuple[Capability, ...]:
    required: list[Capability] = []
    if any(tool.permission == "ask" for tool in spec.tools or ()):
        required.append("tool_confirmation")
    if "anthropic.multiagent" in (spec.extensions or {}):
        required.append("multiagent")
    return tuple(required)


async def current_agent_backend(
    session: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID
) -> AgentBackendRevision | None:
    """Read the current preference for a host-resolved scoped resource identity."""
    if not isinstance(cast(object, agent_id), uuid.UUID):
        raise AgentBackendRefused("agent_identity_required")
    return await agent_backends.current(session, tenant_id, agent_id)
