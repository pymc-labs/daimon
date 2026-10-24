"""Fleet and provider configuration references for the existing admin cards."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from uuid import UUID

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic
    from daimon.core.channel_backend import AgentBackendService
    from daimon.core.stores.domain import BackendAgent
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from daimon.core.neutral_resources import (
    NATIVE_DISPATCH_MAX_SECONDS,
    AgentFleetGrant,
    WorkerLimits,
)
from daimon.core.stores.domain import FLEET_OFF, CompatibilityPreview, TenantBackendPolicy
from mux.contracts.config import ConfigRevision, ResolvedBackend
from mux.contracts.ids import ModelRef, Provider
from mux.profiles import MANAGED_AGENTS, backend_selection, worker_selection
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

#: 10 min per worker, the most native spend-capped dispatch (e.g. Astra) allows; a run
#: gets 20 min. The run's budget caps still bound the money.
DEFAULT_FLEET_WORKER_SECONDS = NATIVE_DISPATCH_MAX_SECONDS
DEFAULT_FLEET_RUN_SECONDS = 1200


def _plain_seconds(seconds: int) -> str:
    """`10 minutes` for whole minutes, else `90 seconds`."""
    minutes, rest = divmod(seconds, 60)
    if rest or not minutes:
        return f"{seconds} seconds"
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def _replies(count: int) -> str:
    return "1 reply" if count == 1 else f"{count} replies"


class FleetConfigInput(BaseModel):
    """Chat-facing ceilings; the grant expiry and worker duration are distinct."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    worker_agents: Literal["any"] | tuple[str, ...] | None = "any"
    max_workers: int | None = Field(default=None, gt=0, strict=True)
    budget_usd: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_worker_seconds: int | None = Field(default=None, gt=0, strict=True)
    max_run_seconds: int = Field(default=DEFAULT_FLEET_RUN_SECONDS, gt=0, strict=True)
    max_turns: int = Field(default=1, gt=0, strict=True)
    max_root_turns: int = Field(default=3, gt=0, strict=True)
    expires_at: datetime | None = None
    provider_limits_usd: dict[Provider, Decimal] | None = None
    max_model_requests: int | None = Field(default=None, gt=0, strict=True)
    max_input_tokens: int | None = Field(default=None, gt=0, strict=True)
    max_output_tokens: int | None = Field(default=None, gt=0, strict=True)
    native_budget_usd: Decimal | None = None
    native_root_budget_usd: Decimal | None = None
    root_cash_budget_usd: Decimal | None = None

    @field_validator("worker_agents")
    @classmethod
    def _names(
        cls, value: Literal["any"] | tuple[str, ...] | None
    ) -> Literal["any"] | tuple[str, ...] | None:
        if isinstance(value, tuple) and (
            not value
            or any(not name.strip() for name in value)
            or len({name.casefold() for name in value}) != len(value)
        ):
            raise ValueError("Choose a nonempty list of distinct worker agent names.")
        return value

    def grant(
        self,
        now: datetime,
        *,
        models: tuple[ModelRef, ...],
        worker_ids: Literal["any"] | tuple[UUID, ...] | None,
    ) -> AgentFleetGrant:
        if self.max_workers is None or self.budget_usd is None or self.max_worker_seconds is None:
            raise ValueError("A new fleet needs worker count, run budget and a worker deadline.")
        expires = self.expires_at or now + timedelta(days=90)
        choices = tuple(
            (
                worker_selection(model.provider).model_copy(
                    update={"model": model.id} if model.provider != MANAGED_AGENTS.provider else {}
                ),
                model,
            )
            for model in models
        )
        return AgentFleetGrant.model_validate_json(
            json.dumps(
                {
                    "worker_agents": worker_ids,
                    "max_workers": self.max_workers,
                    "choices": [
                        (backend.model_dump(mode="json"), model.model_dump(mode="json"))
                        for backend, model in choices
                    ],
                    "max_run_seconds": self.max_run_seconds,
                    "max_worker_seconds": self.max_worker_seconds,
                    "max_root_turns": self.max_root_turns,
                    "native_root_budget_usd": self.native_root_budget_usd,
                    "root_cash_budget_usd": self.root_cash_budget_usd,
                    "limits": WorkerLimits.model_validate(
                        {
                            "budget_usd": self.budget_usd,
                            "deadline": expires,
                            "max_turns": self.max_turns,
                            "provider_limits_usd": self.provider_limits_usd,
                            "max_model_requests": self.max_model_requests,
                            "max_input_tokens": self.max_input_tokens,
                            "max_output_tokens": self.max_output_tokens,
                            "native_budget_usd": self.native_budget_usd,
                        }
                    ).model_dump(mode="json"),
                },
                default=str,
            )
        )


#: Platforms whose adapter owns a fleet's lifecycle: it settles or defers each worker's
#: completion (`worker_finished_decision`) and delivers the results to the root's
#: thread. Only there does the computed default fleet apply; elsewhere late workers
#: would be paid for and never delivered. Slack and Teams join once their adapters do.
FLEET_LIFECYCLE_PLATFORMS: frozenset[str] = frozenset({"discord"})

#: What a person asking for a fleet is told where the default doesn't run. It never
#: names another chat app (`platform_words.one_platform_rule`).
FLEET_UNAVAILABLE_HERE = "Fleets aren't available here yet."

#: The fleet every agent has until its owner or an admin sets one or turns it off.
DEFAULT_FLEET_MAX_WORKERS = 4
DEFAULT_FLEET_BUDGET_USD = Decimal("2")
#: Fixed, so the default reads the same on every call and a frozen root never goes stale
#: just because time passed; a policy or catalog change still changes its digest.
DEFAULT_FLEET_EXPIRES_AT = datetime(2100, 1, 1, tzinfo=UTC)


def default_fleet_grant(policy: TenantBackendPolicy) -> AgentFleetGrant | None:
    """The computed default: the tenant's enabled providers' selectable models, small caps.

    Nothing is stored. Models come from the priced, runnable, selectable catalog
    (`agent_catalog`), so worker-only and unpriced models are out, and Pro is never
    offered. A provider the tenant hasn't enabled (an unkeyed one under the deployment
    default) contributes nothing. The root's own model is added by the runtime.
    """
    from daimon.core.agent_model_names import agent_catalog
    from daimon.core.channel_backend import backend_is_enabled
    from mux.profiles import RUNNABLE_PROFILES, get_profile, runnable_selection

    limits = WorkerLimits(
        budget_usd=DEFAULT_FLEET_BUDGET_USD,
        deadline=DEFAULT_FLEET_EXPIRES_AT,
        max_turns=1,
    )
    choices: list[tuple[ResolvedBackend, ModelRef]] = []
    for entry in agent_catalog():
        model = ModelRef(provider=entry.model.provider, id=entry.model.id)
        tokens = model.id.casefold().replace("_", "-").split("-")
        if "pro" in tokens or "fast" in tokens:
            # Never Pro; never a "-fast" speed preset (a local preset key, at a higher price).
            continue
        if not backend_is_enabled(model.provider, policy.enabled_backends):
            continue
        backend = worker_selection(model.provider).model_copy(
            update={"model": model.id} if model.provider != MANAGED_AGENTS.provider else {}
        )
        if backend.profile not in RUNNABLE_PROFILES or not runnable_selection(backend):
            continue
        try:
            limits.require_profile(get_profile(backend.profile))
        except ValueError:
            continue
        if (model.provider, model.id) not in {(m.provider, m.id) for _b, m in choices}:
            choices.append((backend, model))
    if not choices:
        return None
    return AgentFleetGrant(
        worker_agents="any",
        max_workers=DEFAULT_FLEET_MAX_WORKERS,
        choices=tuple(choices),
        limits=limits,
        max_run_seconds=DEFAULT_FLEET_RUN_SECONDS,
        max_worker_seconds=DEFAULT_FLEET_WORKER_SECONDS,
        max_root_turns=3,
    )


def _choice_keys(grant: AgentFleetGrant) -> set[str]:
    return {
        json.dumps([backend.model_dump(mode="json"), model.model_dump(mode="json")], sort_keys=True)
        for backend, model in grant.choices
    }


def fleet_grant_within(new: AgentFleetGrant, ceiling: AgentFleetGrant) -> bool:
    """Whether `new` only tightens `ceiling`, in every dimension (an owner's edit)."""

    def capped(value: Decimal | int | None, limit: Decimal | int | None) -> bool:
        # An absent limit on the ceiling allows any finite value; a present one bounds it.
        return limit is None or (value is not None and value <= limit)

    a, b = new.limits, ceiling.limits
    if b.provider_limits_usd is not None and (
        a.provider_limits_usd is None
        or any(
            value > b.provider_limits_usd.get(provider, Decimal(0))
            for provider, value in a.provider_limits_usd.items()
        )
    ):
        return False
    if isinstance(ceiling.worker_agents, tuple):
        workers_ok = new.worker_agents is None or (
            isinstance(new.worker_agents, tuple)
            and set(new.worker_agents) <= set(ceiling.worker_agents)
        )
    elif ceiling.worker_agents is None:
        workers_ok = new.worker_agents is None
    else:
        workers_ok = True
    return (
        workers_ok
        and new.max_workers <= ceiling.max_workers
        and _choice_keys(new) <= _choice_keys(ceiling)
        and a.budget_usd <= b.budget_usd
        and a.deadline <= b.deadline
        and a.max_turns <= b.max_turns
        and capped(a.max_model_requests, b.max_model_requests)
        and capped(a.max_input_tokens, b.max_input_tokens)
        and capped(a.max_output_tokens, b.max_output_tokens)
        and capped(a.native_budget_usd, b.native_budget_usd)
        and new.max_run_seconds <= ceiling.max_run_seconds
        and new.max_worker_seconds <= ceiling.max_worker_seconds
        and new.max_root_turns <= ceiling.max_root_turns
        # Root spending controls are an admin's to grant: only within one already granted.
        and _within_granted(new.native_root_budget_usd, ceiling.native_root_budget_usd)
        and _within_granted(new.root_cash_budget_usd, ceiling.root_cash_budget_usd)
    )


def _within_granted(value: Decimal | None, limit: Decimal | None) -> bool:
    return value is None or (limit is not None and value <= limit)


def is_default_fleet(grant: AgentFleetGrant) -> bool:
    """A stored grant always has epoch >= 1; only the computed default has 0."""
    return grant.epoch == 0


def fleet_config_from_grant(
    grant: AgentFleetGrant,
    *,
    worker_names: Literal["any"] | tuple[str, ...] | None,
) -> FleetConfigInput:
    """Return every stored ceiling, not defaults that could widen an edit."""
    limits = grant.limits
    return FleetConfigInput(
        worker_agents=worker_names,
        max_workers=grant.max_workers,
        budget_usd=limits.budget_usd,
        max_worker_seconds=grant.max_worker_seconds,
        max_run_seconds=grant.max_run_seconds,
        max_turns=limits.max_turns,
        max_root_turns=grant.max_root_turns,
        expires_at=grant.expires_at,
        provider_limits_usd=dict(limits.provider_limits_usd)
        if limits.provider_limits_usd is not None
        else None,
        max_model_requests=limits.max_model_requests,
        max_input_tokens=limits.max_input_tokens,
        max_output_tokens=limits.max_output_tokens,
        native_budget_usd=limits.native_budget_usd,
        native_root_budget_usd=grant.native_root_budget_usd,
        root_cash_budget_usd=grant.root_cash_budget_usd,
    )


async def fleet_worker_names(
    grant: AgentFleetGrant,
    client: AsyncAnthropic,
    *,
    tenant_id: UUID,
) -> Literal["any"] | tuple[str, ...] | None:
    from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
    from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME

    if not isinstance(grant.worker_agents, tuple):
        return grant.worker_agents
    names: list[str] = []
    for agent_id in grant.worker_agents:
        native = await find_agent_by_derived_uuid(client, tenant_id=tenant_id, agent_id=agent_id)
        if native is None:
            raise ValueError(
                "A previously selected worker agent is unavailable. "
                "Choose worker agents again; nothing changed."
            )
        names.append(native.metadata.get(MA_METADATA_KEY_NAME) or native.name)
    return tuple(names)


async def complete_fleet_config(
    config: FleetConfigInput,
    existing: AgentFleetGrant | None,
    client: AsyncAnthropic,
    *,
    tenant_id: UUID,
    default: bool = False,
) -> FleetConfigInput:
    """Fill omitted fields from `existing`; `default` says it is the computed default."""
    if existing is None:
        return config
    names = (
        config.worker_agents
        if "worker_agents" in config.model_fields_set
        else await fleet_worker_names(existing, client, tenant_id=tenant_id)
    )
    base = fleet_config_from_grant(existing, worker_names=names).model_dump()
    if default:
        # Custom limits get their own expiry (90 days unless chosen), never the default's.
        base.pop("expires_at")
    return FleetConfigInput.model_validate({**base, **config.model_dump(exclude_unset=True)})


async def configured_fleet_grant(
    config: FleetConfigInput,
    service: AgentBackendService,
    client: AsyncAnthropic,
    sessions: async_sessionmaker[AsyncSession],
    agent: BackendAgent,
    *,
    now: datetime,
) -> AgentFleetGrant:
    """Resolve tenant-scoped worker names on each preview/click, never trust caller ids."""
    from daimon.core.channel_backend import AgentBackendRefused, load_backend_agent
    from daimon.core.constants import DEFAULT_AGENT_MODEL

    current = await service.current(agent)
    provider = (current.backend if current else None) or MANAGED_AGENTS.provider
    selection = backend_selection(provider)
    root = ModelRef(
        provider=provider,
        id=(current.model if current else None)
        or (agent.model if provider == MANAGED_AGENTS.provider else selection.model)
        or DEFAULT_AGENT_MODEL,
    )
    # No custom grant: the edit starts from the default, so "2 workers" tightens it.
    setting = await service.fleet_setting(agent.tenant_id, agent.agent_id, now=now)
    existing, default = setting.base, setting.kind != "custom"
    config = await complete_fleet_config(
        config, existing, client, tenant_id=agent.tenant_id, default=default
    )
    models = existing.models if existing else (root,)
    worker_ids: Literal["any"] | tuple[UUID, ...] | None = None
    if config.worker_agents == "any":
        worker_ids = "any"
    elif config.worker_agents is not None:
        workers: list[BackendAgent] = []
        for name in config.worker_agents:
            try:
                worker = await load_backend_agent(
                    client, sessions, tenant_id=agent.tenant_id, agent_name=name
                )
            except AgentBackendRefused as exc:
                raise ValueError(
                    f'Worker agent "{name}" is not available on this server. '
                    "Choose an existing agent by name."
                ) from exc
            workers.append(worker)
        worker_ids = tuple(worker.agent_id for worker in workers)
    value = config.grant(now, models=models, worker_ids=worker_ids)
    if existing is not None:
        # Editing agent eligibility or ceilings must not silently rewrite legacy profiles.
        caps = value.limits.provider_limits_usd
        if default and caps is not None:
            # Provider caps chosen over the default: the root's own model, as for any
            # new fleet, plus the default's models on the providers given a cap.
            models = (
                root,
                *(
                    model
                    for model in existing.models
                    if caps.get(model.provider, Decimal(0)) > 0 and model != root
                ),
            )
            return config.grant(now, models=models, worker_ids=worker_ids)
        data = value.model_dump(mode="json")
        data["choices"] = existing.model_dump(mode="json")["choices"]
        value = AgentFleetGrant.model_validate_json(json.dumps(data))
    return value


class ConfigurationReference(BaseModel):
    """Untrusted reference; every host reloads and validates its preview."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["fleet_set", "fleet_revoke", "provider_allowlist", "channel_pin_clear"]
    agent_name: str | None = Field(default=None, min_length=1)
    config: FleetConfigInput | None = None
    providers: tuple[Provider, ...] | None = None
    expected_revision: StrictInt | StrictStr
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _bound(self) -> ConfigurationReference:
        if self.operation == "channel_pin_clear":
            # The channel is never in the reference: the card binds the chat it is posted in.
            if (
                type(self.expected_revision) is not int
                or self.expected_revision < 1
                or self.agent_name is not None
                or self.config is not None
                or self.providers is not None
            ):
                raise ValueError("clearing a channel pin requires the pin's revision")
            return self
        if self.operation == "provider_allowlist":
            if (
                not isinstance(self.expected_revision, str)
                or self.providers is None
                or self.agent_name is not None
                or self.config is not None
            ):
                raise ValueError("provider changes require the current policy revision")
        elif (
            type(self.expected_revision) is not int
            or self.expected_revision < 0
            or self.agent_name is None
            or self.providers is not None
            or ((self.config is not None) != (self.operation == "fleet_set"))
        ):
            raise ValueError("fleet changes require an agent and current setting revision")
        if self.config is not None and self.config.expires_at is None:
            raise ValueError("confirm the exact expiry returned by the preview")
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


def fleet_default_line(kind: str, *, revoke: bool) -> tuple[str, ...]:
    """Say plainly that the agent runs on the default fleet before a change replaces it."""
    if kind != "default":
        return ()
    if revoke:
        return ("Now: fleet is on with the default limits.",)
    return ("Now: fleet is on with the default limits. Confirming saves these as custom limits.",)


def fleet_detail_lines(
    preview: CompatibilityPreview,
    *,
    worker_names: tuple[str, ...] | Literal["any"] | None = None,
    previous: AgentFleetGrant | None = None,
) -> tuple[str, ...]:
    if preview.fleet_grant is None or preview.fleet_grant == FLEET_OFF:
        return (
            "Removed: fleet access for this agent, including all worker permissions.",
            "Fleet stays off for this agent, default limits included, until its owner "
            "or an admin turns it on again.",
        )
    grant = AgentFleetGrant.model_validate_json(json.dumps(preview.fleet_grant))
    providers = grant.limits.provider_limits_usd
    budget = (
        "Shared across providers; no separate provider budgets."
        if providers is None
        else "Provider caps: "
        + (", ".join(f"{key}: ${value}" for key, value in providers.items()) or "none")
        + ". Providers omitted from this list cannot spend."
    )
    denied = (
        ()
        if providers is None
        else tuple(sorted({m.provider for m in grant.models if providers.get(m.provider, 0) <= 0}))
    )
    return (
        f"Up to {grant.max_workers} workers; ${grant.limits.budget_usd} per run.",
        "Each worker runs as its selected agent, using that agent's own model and settings.",
        f"Each worker: up to {_plain_seconds(grant.max_worker_seconds)}; "
        f"{_replies(grant.limits.max_turns)}.",
        f"Each run: up to {_plain_seconds(grant.max_run_seconds)} and "
        f"{_replies(grant.max_root_turns)} from the coordinating agent.",
        f"Fleet access expires: {grant.expires_at.isoformat()}.",
        budget,
        *(
            ("Cannot spend on: " + ", ".join(denied) + "; add a provider cap to allow it.",)
            if denied
            else ()
        ),
        (
            "Worker agents: any on this server."
            if grant.worker_agents == "any"
            else "Worker agents: " + ", ".join(worker_names) + "; this agent is always included."
            if isinstance(worker_names, tuple)
            else f"Worker agents: this agent and {len(grant.worker_agents)} selected agents."
            if grant.worker_agents
            else "Worker agents: this agent only."
        ),
        "Funding and server limits are checked again when workers start.",
        *fleet_removal_lines(previous, grant),
        *(
            (f"Worker request cap: {grant.limits.max_model_requests}.",)
            if grant.limits.max_model_requests is not None
            else ()
        ),
        *(
            (f"Worker input token cap: {grant.limits.max_input_tokens}.",)
            if grant.limits.max_input_tokens
            else ()
        ),
        *(
            (f"Worker output token cap: {grant.limits.max_output_tokens}.",)
            if grant.limits.max_output_tokens
            else ()
        ),
        *(
            (f"Native worker budget: ${grant.limits.native_budget_usd}.",)
            if grant.limits.native_budget_usd is not None
            else ()
        ),
        *(
            (
                f"Native root budget: ${grant.native_root_budget_usd}; "
                f"root cash cap: ${grant.root_cash_budget_usd}.",
            )
            if grant.native_root_budget_usd is not None
            else ()
        ),
    )


def fleet_removal_lines(before: AgentFleetGrant | None, after: AgentFleetGrant) -> tuple[str, ...]:
    if before is None:
        return ()
    lines: list[str] = []
    for field, label in (
        ("max_model_requests", "worker request cap"),
        ("max_input_tokens", "worker input token cap"),
        ("max_output_tokens", "worker output token cap"),
        ("native_budget_usd", "provider spending cap per worker"),
    ):
        if getattr(before.limits, field) is not None and getattr(after.limits, field) is None:
            lines.append("Removed: " + label + ".")
    for field, label in (
        ("native_root_budget_usd", "provider spending cap for the coordinating agent"),
        ("root_cash_budget_usd", "cash spending cap for the coordinating agent"),
    ):
        if getattr(before, field) is not None and getattr(after, field) is None:
            lines.append("Removed: " + label + ".")
    old = before.limits.provider_limits_usd
    new = after.limits.provider_limits_usd
    if old is not None and new is None:
        lines.append("Removed: provider caps. The run budget is now shared across providers.")
    elif old is not None and new is not None:
        for provider in sorted(set(old) - set(new)):
            lines.append(
                f"Removed: {provider} spending allocation; this provider now cannot spend."
            )
    if before.worker_agents != "any" and after.worker_agents == "any":
        lines.append("Removed: the restriction to previously selected worker agents.")
    if before.worker_agents == "any" and after.worker_agents != "any":
        lines.append("Removed: eligibility for worker agents outside the selection shown above.")
    elif isinstance(before.worker_agents, tuple) and after.worker_agents != "any":
        remaining = set(after.worker_agents or ())
        if set(before.worker_agents) - remaining:
            lines.append(
                "Removed: previously selected worker entries not shown above. "
                "This agent remains eligible."
            )
    if before.expires_at != after.expires_at:
        lines.append(
            f"Access expiry changed from {before.expires_at.isoformat()} "
            f"to {after.expires_at.isoformat()}."
        )
    return tuple(lines)


def configuration_refusal(reason: str, detail: str | None = None) -> str:
    if reason == "fleet_raise_admin_required" and detail is not None:
        # The limit an owner may go up to, and who can raise it.
        return f"Only an admin can raise this agent's fleet above {detail}; ask an admin."
    if reason.startswith("fleet_root_"):
        from daimon.core.neutral_resources import fleet_root_obstacle_text

        return fleet_root_obstacle_text(reason.removeprefix("fleet_root_"))
    return {
        "backend_not_allowed": (
            "That provider is not allowed here. Ask an admin to enable it "
            "in the provider allowlist."
        ),
        "fleet_provider_limits_exceed_budget": (
            "The provider caps add up to more than the run budget. "
            "Lower them or raise the run budget before saving."
        ),
        "fleet_raise_admin_required": (
            "Only an admin can raise fleet limits above the default (4 workers, $2 a run) "
            "or above what an admin set. You can lower them, or turn fleet off."
        ),
        "invalid_fleet_grant": (
            "These fleet limits cannot be used. "
            "Check the worker agents, deadline, expiry and token caps before saving."
        ),
        "stale_preview": "This preview changed. Preview and confirm again.",
        "stale_revision": "This setting changed. Preview and confirm again.",
        "feature_disabled": (
            "Fleet and provider settings are switched off for this whole Daimon "
            "installation (an emergency stop), so nobody can change them from chat right "
            "now. Try again later, or tell whoever runs this Daimon installation."
        ),
        "admin_required": "A current admin must confirm this change.",
    }.get(
        reason,
        "This change could not be saved. "
        "Preview again or ask an admin to check the agent's settings.",
    )
