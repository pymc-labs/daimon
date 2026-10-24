"""``list_models``: the models an agent here can be set to, by name, with list prices.

Read-only. It answers "what models can I use?" and "what does luna cost?" from
the same priced, runnable catalog the model-name resolver uses, filtered to the
providers the caller's tenant has turned on, so every name it lists resolves
and can be chosen here.
"""

from __future__ import annotations

from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from fastmcp import Context, FastMCP
from mux.contracts.ids import Provider
from pydantic import JsonValue


def _list_models_impl(enabled: tuple[Provider, ...]) -> list[JsonValue]:
    from daimon.core.agent_model_names import agent_catalog, model_name
    from daimon.core.channel_backend import backend_is_enabled

    return [
        {
            "name": model_name(entry.model),
            "id": entry.model.id,
            "provider": entry.model.provider,
            "price": entry.price,
        }
        for entry in agent_catalog()
        if backend_is_enabled(entry.model.provider, enabled)
    ]


def register_model_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_models(ctx: Context) -> list[JsonValue]:  # pyright: ignore[reportUnusedFunction]
        """List the models an agent here can run on: plain name, id, provider and list price.

        Use it to answer "what models can I use?" or "what does luna cost?", and
        to pick a name for an agent's model. Only providers this server has turned
        on are listed, and every listed name (e.g. "Opus 5.5", "Astra", "Argon")
        is accepted wherever a model is named. Prices are list prices per million
        tokens, before this server's markup. A fleet's own allowed models are in
        fleet_agents.
        """
        from daimon.core.channel_backend import make_backend_service

        auth = await _auth(ctx)
        service = make_backend_service(runtime.session_factory, runtime.settings.turn)
        policy = await service.tenant_policy(auth.tenant_id)
        return _list_models_impl(tuple(policy.enabled_backends))
