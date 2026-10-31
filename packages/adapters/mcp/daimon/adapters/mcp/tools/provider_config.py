"""Provider and channel-pin configuration over the service owners."""

from __future__ import annotations

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
from daimon.core.agent_configuration import (
    ConfigurationReference,
    configuration_refusal,
    provider_preview_digest,
)
from daimon.core.channel_backend import AgentBackendRefused, BackendActor, make_backend_service
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mux.contracts.ids import Provider
from pydantic import BaseModel, ValidationError

log = structlog.get_logger(__name__)


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
        _require_admin(auth)  # pyright: ignore[reportPrivateUsage]
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
            "This applies across this server. Saving makes this list the server's own choice; "
            "until an admin saves one, the server uses every provider this Daimon install has "
            "a key for. "
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
    from daimon.core.agent_configuration import channel_pin_digest
    from daimon.core.channel_backend import channel_ref, clear_channel_backend, current_backend
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


def register_provider_config_tools(mcp: FastMCP, runtime: McpRuntime) -> None:

    @mcp.tool(tags={"admin"})
    async def request_provider_allowlist(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        providers: tuple[Provider, ...],
        expected_revision: str | None = None,
        preview_digest: str | None = None,
        origin_context_id: str | None = None,
    ) -> ProviderAllowlistInfo:
        """Preview and confirm the server's allowed model providers. Admin-only.

        providers is the complete replacement list, not an addition. A server no
        admin configured uses every provider this Daimon install holds a key for;
        saving a list makes it the server's own choice, which always wins.
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
