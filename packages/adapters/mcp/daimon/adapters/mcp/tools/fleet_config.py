"""Chat configuration over the fleet-grant and tenant-provider service owners."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial

import structlog
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._authz_facts import mcp_subject
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._session_gate import session_card_gap
from daimon.adapters.mcp.tools.setup_target import get_chat_origin
from daimon.core.channel_backend import (
    AgentBackendRefused,
    BackendActor,
    load_backend_agent,
    make_backend_service,
)
from daimon.core.fleet_configuration import (
    FLEET_LIFECYCLE_PLATFORMS,
    FLEET_UNAVAILABLE_HERE,
    ConfigurationReference,
    FleetConfigInput,
    complete_fleet_config,
    configuration_refusal,
    configured_fleet_grant,
    fleet_config_from_grant,
    fleet_default_line,
    fleet_detail_lines,
    fleet_worker_names,
    provider_preview_digest,
)
from daimon.core.neutral_resources import FLEET_ROOT_OBSTACLE_DEFAULT, FLEET_ROOT_OBSTACLES
from daimon.core.stores.domain import BackendAgent
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.ids import Provider
from pydantic import BaseModel, ValidationError

log = structlog.get_logger(__name__)


class FleetConfigInfo(BaseModel):
    status: str
    agent_name: str
    config: FleetConfigInput | None
    expected_revision: int
    preview_digest: str
    details: tuple[str, ...]
    summary: str


class ChannelPinInfo(BaseModel):
    status: str
    pinned: str | None
    expected_revision: int
    preview_digest: str
    summary: str


class ProviderAllowlistInfo(BaseModel):
    status: str
    providers: tuple[Provider, ...]
    expected_revision: str
    preview_digest: str
    summary: str


async def _require_chat(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None, tool: str
) -> None:
    if tool in ("request_provider_allowlist", "request_channel_pin_clear"):
        _require_admin(auth)
    if auth.agent_id is not None or auth.platform_user_id is None:
        raise ToolError("Request this change from your active chat conversation.")
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    if origin is None:
        raise ToolError("Request this change from your active chat conversation.")
    if await session_card_gap(runtime, auth, origin, tool_name=tool) is not None:
        raise ToolError("This conversation cannot verify the confirmation card. Nothing changed.")


def _actor(auth: AuthIdentity) -> BackendActor:
    return BackendActor(
        tenant_id=auth.tenant_id,
        account_id=auth.account_id,
        subject=mcp_subject(auth, is_admin=auth.is_admin),
    )


async def _capture_fleet_target(
    runtime: McpRuntime, auth: AuthIdentity, source: BackendAgent
) -> BackendAgent:
    """Capture the TARGET agent's own source, by name, not the channel's default agent.

    The explicit-target form of the backend-switch capture: the requester's tenant
    and account, no channel, the named agent and its pinned native agent.
    """
    from daimon.core.ma_resolver import new_resolver_cache
    from daimon.core.turn.admission import capture_agent_source
    from daimon.core.turn.deps import build_turn_deps

    deps = build_turn_deps(
        runtime.settings,
        runtime.client,
        runtime.session_factory,
        deployment_default=runtime.deployment_default,
        resolver_cache=new_resolver_cache(),
        billing_config=None,
    )
    return await capture_agent_source(
        deps, source, account_id=auth.account_id, subject=mcp_subject(auth, is_admin=auth.is_admin)
    )


async def fleet_root_obstacle_for(
    runtime: McpRuntime, auth: AuthIdentity, agent: BackendAgent
) -> str | None:
    """What in this agent's own setup keeps its turns from running a fleet, if anything.

    Asked here, in the MCP process: the turn that refused its root ran in the chat
    adapter's process, whose in-memory reason (`root_refusal`) this one can't see.
    A failed capture is no answer (None), never an error.
    """
    from daimon.core.neutral_resources import fleet_root_obstacle

    try:
        return fleet_root_obstacle(await _capture_fleet_target(runtime, auth, agent))
    except Exception as error:  # an explanation must never fail the tool call
        log.warning("fleet.obstacle_check_failed", cause=type(error).__name__)
        return None


def fleet_status_summary(kind: str, platform: str | None, *, obstacle: str | None = None) -> str:
    """The current-settings line; it says plainly where or why a fleet can't start."""
    if obstacle is not None:
        reason = FLEET_ROOT_OBSTACLES.get(obstacle, FLEET_ROOT_OBSTACLE_DEFAULT)
        return f"Fleet would be on with these limits, but this agent can't run one yet: {reason}."
    if kind != "default":
        return "Current fleet limits (custom)."
    if platform not in FLEET_LIFECYCLE_PLATFORMS:
        return (
            FLEET_UNAVAILABLE_HERE + " These are the default limits this agent would use; "
            "an admin can still set a custom fleet."
        )
    return (
        "Fleet is on with the default limits; nobody has changed them. The agent's owner "
        "or an admin can tighten them or turn fleet off."
    )


async def _request_fleet_config_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    agent_name: str,
    config: FleetConfigInput | None = None,
    revoke: bool = False,
    expected_revision: int | None = None,
    preview_digest: str | None = None,
    origin_context_id: str | None = None,
) -> FleetConfigInfo:
    await _require_chat(runtime, auth, origin_context_id, "request_fleet_config")
    # The source capture lets a new grant's preview refuse an agent that can't run a fleet.
    service = make_backend_service(
        runtime.session_factory,
        runtime.settings.turn,
        capture_source=partial(_capture_fleet_target, runtime, auth),
    )
    now = datetime.now(UTC)
    try:
        agent = await load_backend_agent(
            runtime.client,
            runtime.session_factory,
            tenant_id=auth.tenant_id,
            agent_name=agent_name,
        )
        from daimon.adapters.mcp.tools.agents import (
            _require_agent_editor,  # pyright: ignore[reportPrivateUsage]
        )

        await _require_agent_editor(service, agent, _actor(auth), auth)
        setting = await service.fleet_setting(auth.tenant_id, agent.agent_id, now=now)
        # With no custom grant, a change starts from the default limits.
        existing = setting.base
        if config is None and not revoke:
            if preview_digest is not None:
                raise ToolError("A change needs the exact configuration from its preview.")
            if setting.grant is None:
                return FleetConfigInfo(
                    status="not_configured",
                    agent_name=agent_name,
                    config=None,
                    expected_revision=(
                        current.local if (current := await service.current(agent)) else 0
                    ),
                    preview_digest="",
                    details=(),
                    summary="Fleet is off for this agent. Ask the agent's owner or an admin: "
                    '"Turn on fleet for ' + agent_name + ', with the default limits."',
                )
            existing = setting.grant
            saved = fleet_config_from_grant(
                existing,
                worker_names=await fleet_worker_names(
                    existing, runtime.client, tenant_id=auth.tenant_id
                ),
            )
            preview = await service.preview_fleet_grant(agent, existing, now=now)
            return FleetConfigInfo(
                status="current",
                agent_name=agent_name,
                config=saved,
                expected_revision=preview.setting_revision,
                preview_digest=preview.digest,
                details=fleet_detail_lines(preview, worker_names=saved.worker_agents),
                # Say "on" only when this agent's turns can actually start one.
                summary=fleet_status_summary(
                    setting.kind,
                    auth.platform,
                    obstacle=await fleet_root_obstacle_for(runtime, auth, agent),
                ),
            )
        if config is not None:
            config = await complete_fleet_config(
                config,
                existing,
                runtime.client,
                tenant_id=auth.tenant_id,
                default=setting.kind != "custom",
            )
        if revoke and config is not None:
            raise ToolError("Choose either fleet limits or turning fleet off. Nothing changed.")
        value = (
            await configured_fleet_grant(
                config, service, runtime.client, runtime.session_factory, agent, now=now
            )
            if config is not None
            else None
        )
        # Freeze the default expiry in the returned config; confirmation cannot move it.
        frozen = (
            config.model_copy(update={"expires_at": value.expires_at}) if config and value else None
        )
        if value is not None:
            # A non-admin owner may only tighten; say so before showing a card.
            await service.check_owner_fleet_ceiling(agent, value, _actor(auth), now=now)
        preview = await service.preview_fleet_grant(agent, value, now=now, check_root=True)
        status = "preview"
        summary = (
            "Show these limits, then call request_fleet_config with this exact config, "
            "revoke, expected_revision and preview_digest. The requesting owner or "
            "admin must Confirm "
            "on the card. Nothing changed yet."
        )
        if preview_digest is not None:
            reference = ConfigurationReference.model_validate(
                {
                    "operation": "fleet_revoke" if revoke else "fleet_set",
                    "agent_name": agent_name,
                    "config": config,
                    "expected_revision": expected_revision,
                    "preview_digest": preview_digest,
                }
            )
            agent = await load_backend_agent(
                runtime.client,
                runtime.session_factory,
                tenant_id=auth.tenant_id,
                agent_name=agent_name,
            )
            assert isinstance(reference.expected_revision, int)
            if value is None:
                await service.revoke_fleet_grant(
                    agent,
                    reference.expected_revision,
                    _actor(auth),
                    expected_preview_digest=reference.preview_digest,
                    now=now,
                )
            else:
                await service.set_fleet_grant(
                    agent,
                    value,
                    reference.expected_revision,
                    _actor(auth),
                    expected_preview_digest=reference.preview_digest,
                    now=now,
                )
            status = "changed"
            summary = "Fleet access turned off." if revoke else "Fleet limits saved."
        return FleetConfigInfo(
            status=status,
            agent_name=agent_name,
            config=frozen,
            expected_revision=preview.setting_revision,
            preview_digest=preview.digest,
            details=(
                *(fleet_default_line(setting.kind, revoke=revoke) if status == "preview" else ()),
                *fleet_detail_lines(
                    preview,
                    worker_names=frozen.worker_agents if frozen else None,
                    previous=existing if setting.kind == "custom" else None,
                ),
            ),
            summary=summary,
        )
    except AgentBackendRefused as exc:
        raise ToolError(configuration_refusal(exc.reason) + " Nothing changed.") from exc
    except ValueError as exc:
        if not isinstance(exc, ValidationError):
            raise ToolError(str(exc) + " Nothing changed.") from exc
        raise ToolError(
            "Check the fleet limits and use the exact expiry from the preview. Nothing changed."
        ) from exc


async def _request_provider_allowlist_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    providers: tuple[Provider, ...],
    expected_revision: str | None = None,
    preview_digest: str | None = None,
    origin_context_id: str | None = None,
) -> ProviderAllowlistInfo:
    await _require_chat(runtime, auth, origin_context_id, "request_provider_allowlist")
    service = make_backend_service(runtime.session_factory, runtime.settings.turn)
    try:
        policy = await service.tenant_policy(auth.tenant_id)
        # The existing service validates actor, policy and values without saving.
        await service.set_tenant_allowlist(
            auth.tenant_id, providers, policy.revision, _actor(auth), dry_run=True
        )
        digest = provider_preview_digest(policy, providers)
        status = "preview"
        summary = (
            "Allowed providers will be: " + (", ".join(providers) or "none") + ". "
            "This applies across this server. Keys do not enable providers automatically. "
            "Call request_provider_allowlist again with the same providers, expected_revision "
            "and preview_digest; the admin must Confirm on the card. Nothing changed yet."
        )
        if preview_digest is not None:
            reference = ConfigurationReference.model_validate(
                {
                    "operation": "provider_allowlist",
                    "providers": providers,
                    "expected_revision": expected_revision,
                    "preview_digest": preview_digest,
                }
            )
            if reference.expected_revision != policy.revision or reference.preview_digest != digest:
                raise ToolError("This preview changed. Preview and confirm again. Nothing changed.")
            await service.set_tenant_allowlist(
                auth.tenant_id, providers, policy.revision, _actor(auth)
            )
            status = "changed"
            summary = "Allowed providers saved: " + (", ".join(providers) or "none") + "."
        return ProviderAllowlistInfo(
            status=status,
            providers=providers,
            expected_revision=policy.revision,
            preview_digest=digest,
            summary=summary,
        )
    except AgentBackendRefused as exc:
        raise ToolError(configuration_refusal(exc.reason) + " Nothing changed.") from exc
    except ValidationError as exc:
        raise ToolError(
            "This preview is incomplete. Preview and confirm again. Nothing changed."
        ) from exc


async def _request_channel_pin_clear_impl(
    runtime: McpRuntime,
    auth: AuthIdentity,
    *,
    expected_revision: int | None = None,
    preview_digest: str | None = None,
    origin_context_id: str | None = None,
) -> ChannelPinInfo:
    """This chat's channel pin: show it, then clear it once the admin confirms."""
    from daimon.core.channel_backend import channel_ref, clear_channel_backend, current_backend
    from daimon.core.fleet_configuration import channel_pin_digest
    from daimon.core.stores import mux_state
    from daimon.core.turn.notices import provider_name

    await _require_chat(runtime, auth, origin_context_id, "request_channel_pin_clear")
    origin = await get_chat_origin(runtime, auth, origin_context_id)
    assert origin is not None
    channel = channel_ref(origin.tenant_id, origin.platform, origin.parent_channel_id)
    async with runtime.session_factory() as session:
        pin = await current_backend(session, channel)
        pinned = (
            pin is not None
            and await mux_state.config_origin(session, channel, pin.local) == "channel"
        )
    if pin is None or not pinned:
        if preview_digest is not None:
            raise ToolError("This channel has no provider pin any more. Nothing changed.")
        return ChannelPinInfo(
            status="not_pinned",
            pinned=None,
            expected_revision=0,
            preview_digest="",
            summary="This channel has no provider pin: each agent answers here on its own "
            "provider.",
        )
    named = provider_name(pin.backend) + (f" ({pin.model})" if pin.model else "")
    digest = channel_pin_digest(pin)
    if preview_digest is None:
        return ChannelPinInfo(
            status="preview",
            pinned=named,
            expected_revision=pin.local,
            preview_digest=digest,
            summary=f"This channel pins {named}, so agents answer here on it instead of their "
            "own provider. Call request_channel_pin_clear again with this expected_revision and "
            "preview_digest; the requesting admin must Confirm on the card. Nothing changed yet.",
        )
    if expected_revision != pin.local or preview_digest != digest:
        raise ToolError("This channel's pin changed. Preview and confirm again. Nothing changed.")
    try:
        async with runtime.session_factory.begin() as session:
            # Re-checked under the clear's own lock; who and which channel are audited.
            await clear_channel_backend(session, channel, actor=_actor(auth), expected=pin)
    except AgentBackendRefused as exc:
        raise ToolError(
            "This channel's pin changed. Preview and confirm again. Nothing changed."
        ) from exc
    return ChannelPinInfo(
        status="cleared",
        pinned=None,
        expected_revision=pin.local,
        preview_digest=digest,
        summary=f"Cleared this channel's {named} pin. New conversations here use each agent's "
        "own provider; existing threads keep their model.",
    )


def register_fleet_config_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def request_fleet_config(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        agent_name: str,
        config: FleetConfigInput | None = None,
        revoke: bool = False,
        expected_revision: int | None = None,
        preview_digest: str | None = None,
        origin_context_id: str | None = None,
    ) -> FleetConfigInfo:
        """Configure this agent's fleet of worker agents, with owner/admin confirmation.

        Every agent already has a fleet with default limits (4 workers, $2 a run);
        call this only when the agent's owner or an admin asks to change or turn off
        those limits. Never use it to get around a fleet refusal or a limit someone
        else hit: tell them the limit and who can change it instead.

        Use agent names, never model lists: e.g. "let this agent work with the
        research and writing agents, up to 4 workers and $2 per run". config sets
        worker_agents ("any", a list of names, or null for this agent alone),
        max_workers, budget_usd and max_worker_seconds (required for a new fleet).
        On edits, every omitted field keeps its saved value, including expiry,
        provider caps, token limits and worker selection. Explicit null removes an
        optional cap; the card marks removals. A current-settings read returns the
        complete saved config, so it can be edited without reconstructing defaults.
        Use fleet_agents to list eligible workers. Each worker runs AS that
        agent, using its own prompt, model, tools and skills; model choices
        belong in agent settings through request_agent_backend_switch.
        Worker access defaults to any agent on this server for a NEW config;
        existing grants stay unchanged until confirmed. Show every detail,
        especially worker agents, budget, expiry and provider caps.
        max_run_seconds defaults to 600; max_turns to 1; access expires after
        90 days unless specified. No provider map means a shared budget; a
        partial map prevents omitted providers from spending.
        Omit config to show the current settings; revoke=true turns fleet off.
        Preview first. Then pass the exact returned config, expected_revision
        and preview_digest to show the requesting owner's/admin's Confirm card.
        Pass this turn's origin_context_id. A stale preview changes nothing.
        """
        return await _request_fleet_config_impl(
            runtime,
            await _auth(ctx),
            agent_name=agent_name,
            config=config,
            revoke=revoke,
            expected_revision=expected_revision,
            preview_digest=preview_digest,
            origin_context_id=origin_context_id,
        )

    @mcp.tool(tags={"admin"})
    async def request_provider_allowlist(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        providers: tuple[Provider, ...],
        expected_revision: str | None = None,
        preview_digest: str | None = None,
        origin_context_id: str | None = None,
    ) -> ProviderAllowlistInfo:
        """Preview and confirm the server's allowed model providers. Admin-only.

        providers is the complete replacement list, not an addition. Having an API
        key does not consent to provider use; nothing is enabled automatically.
        Preview first, explain the server-wide scope and full list, then call with
        the same providers and returned expected_revision and preview_digest for
        the admin's Confirm card. Pass this active turn's origin_context_id.
        """
        return await _request_provider_allowlist_impl(
            runtime,
            await _auth(ctx),
            providers=providers,
            expected_revision=expected_revision,
            preview_digest=preview_digest,
            origin_context_id=origin_context_id,
        )

    @mcp.tool(tags={"admin"})
    async def request_channel_pin_clear(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        expected_revision: int | None = None,
        preview_digest: str | None = None,
        origin_context_id: str | None = None,
    ) -> ChannelPinInfo:
        """Show or clear THIS channel's model-provider pin, with admin confirmation. Admin-only.

        Use when an admin says "clear this channel's pin", or when a switch or refusal says
        this channel pins a provider. A pin makes every agent answer here on that provider;
        clearing it lets each agent use its own. First call without preview_digest and show
        what the channel pins. Then call again with that preview's expected_revision and
        preview_digest: the requesting admin must Confirm on the card. Only this chat's
        channel can be cleared. Existing threads keep their model. Pass this turn's
        origin_context_id. A stale preview changes nothing.
        """
        return await _request_channel_pin_clear_impl(
            runtime,
            await _auth(ctx),
            expected_revision=expected_revision,
            preview_digest=preview_digest,
            origin_context_id=origin_context_id,
        )
