"""Whether a stored loss consent still covers a target (one rule, one helper).

A consent records what the owner accepted losing and a digest of the target it was
confirmed for. Releases change that target without the owner changing anything
(Daimon's tool gating, the catalogue, how a template keeps tools), and each of
those must not send every switched agent a fresh confirmation, while a change the
owner would care about (a model, an MCP server, a gained capability) still must.
`covers` is the only place that compares a consent with a target.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from daimon.core.tool_safety import DAIMON_SERVER_NAME
from mux.contracts.ids import Provider
from mux.contracts.resources import AgentSpec
from mux.profiles import CONSENTED_DEFAULT_MODELS
from mux.state.operations import request_digest

if TYPE_CHECKING:
    from daimon.core.stores.domain import _BackendDropConsent  # pyright: ignore[reportPrivateUsage]


def covers(
    consent: _BackendDropConsent,
    target: AgentSpec | None,
    *,
    source_system: str | None,
    backend: Provider,
    model: str | None,
    picked: str | None,
) -> bool:
    """`consent` still holds for `target`, the copy today's rules build for the source.

    Holds when the target is the one confirmed (digest and drops), when the model
    is the confirmed one and the losses differ only by Daimon's own decisions, when
    it differs only by Daimon tools it offers again behind a card, or when it
    differs only as a release before #1438 or #1483 built it.
    """
    if target is None:
        return False
    return (
        _same_target(target, consent)
        or _regains_carded(target, source_system, consent)
        or (model_consented(backend, model, picked) and same_losses(target, consent))
        or _consented_before(target, source_system, consent)
    )


#: The template drop a provider turn's consent named before Daimon posted the
#: switch card itself (`tool_safety.self_carded_tools`).
_SWITCH_TOOL_DROP: Final[str] = f"mcp_tool:{DAIMON_SERVER_NAME}:request_agent_backend_switch"


def model_consented(backend: Provider, model: str | None, picked: str | None) -> bool:
    """The resolved `model` is the one the consent was confirmed for.

    A consent stores no model (a release before this one must still read the
    row), so a default-model setting counts only while the provider default is
    still the one such consents were recorded under. No model means the
    source's own, which the source pin covers.
    """
    return model is None or model == (picked or CONSENTED_DEFAULT_MODELS.get(backend))


def _same_target(target: AgentSpec, consent: _BackendDropConsent) -> bool:
    """The digest check: a different model, MCP URL or instruction never passes."""
    return (
        target.declared_drops == consent.declared_drops
        and request_digest(target.model_dump(mode="json")) == consent.target_digest
    )


def same_losses(target: AgentSpec, consent: _BackendDropConsent) -> bool:
    """`target` loses what `consent` accepted, apart from what Daimon's own tools decide.

    Daimon's gating and catalogue change with a release, not with the agent: the
    server refuses a gated tool to provider turns whatever the consent says, and a
    tool Daimon no longer serves can't be lost. The source pin covers the owner's
    tool settings. Any other difference, a gain included (a tool Daimon stops
    gating, MCP back on a copy confirmed without it), asks again.
    """
    from daimon.core.tool_safety import (
        backend_switch_tools,
        confirmation_gated_tools,
        self_carded_tools,
    )

    prefix = f"mcp_tool:{DAIMON_SERVER_NAME}:"
    # A self-carded tool runs on provider turns: offering it is a gain.
    gated = confirmation_gated_tools() - self_carded_tools()
    served = {
        drop.removeprefix(prefix) for drop in target.declared_drops if drop.startswith(prefix)
    }
    for server in target.mcp_servers or ():
        allowed = server.tool_policy.get("allowed_tools") if server.tool_policy else None
        if server.name == DAIMON_SERVER_NAME and isinstance(allowed, list):
            served.update(name for name in allowed if isinstance(name, str))
    # Whether a backend-switch tool is offered follows Daimon's switch gate, not
    # the owner: a consent from before it was offered names it a loss, and turning
    # the gate off drops it from an agent confirmed with it. Neither side counts.
    decided = {f"{prefix}{name}" for name in backend_switch_tools(enabled=True)}
    today = {
        drop
        for drop in target.declared_drops
        if drop not in decided
        and (not drop.startswith(prefix) or drop.removeprefix(prefix) not in gated)
    }
    accepted = {
        drop
        for drop in consent.declared_drops
        if drop not in decided
        and (not drop.startswith(prefix) or drop.removeprefix(prefix) in served - gated)
    }
    if "tools" in accepted and "tools" not in today and target.tools:
        # Before #1483 a template lost the source's tools whole; now it keeps
        # them and names only the web tools it can't carry.
        accepted = (accepted - {"tools"}) | {d for d in today if d.startswith("builtin_tool:")}
    return today == accepted


def _with_daimon_tools_dropped(
    target: AgentSpec, source_system: str | None, tools: frozenset[str]
) -> AgentSpec | None:
    """`target` as a consent that named Daimon's `tools` losses builds it.

    Each tool leaves Daimon's MCP allowlist and joins the drops and the drops line
    in the instructions. Only that difference is rebuilt; anything else still
    differs and still needs a fresh confirmation.
    """
    from daimon.core.turn.runtime_manifest import compose_source_instructions
    from mux.contracts.resources import MCPConnection

    named = {f"mcp_tool:{DAIMON_SERVER_NAME}:{tool}" for tool in tools}
    if not tools or named & set(target.declared_drops):
        return None
    servers: list[MCPConnection] = []
    found: set[str] = set()
    for server in target.mcp_servers or ():
        allowed = server.tool_policy.get("allowed_tools") if server.tool_policy else None
        if server.name == DAIMON_SERVER_NAME and isinstance(allowed, list):
            found.update(name for name in allowed if name in tools)
            server = server.model_copy(
                update={
                    "tool_policy": {
                        **server.tool_policy,
                        "allowed_tools": [name for name in allowed if name not in tools],
                    }
                }
            )
        servers.append(server)
    if frozenset(found) != tools:
        return None
    drops = tuple(sorted({*target.declared_drops, *named}))
    return target.model_copy(
        update={
            "mcp_servers": tuple(servers),
            "declared_drops": drops,
            "system": compose_source_instructions(
                source_system, "Unavailable capabilities: " + ", ".join(drops) + "."
            ),
        }
    )


def _with_switch_tool_dropped(target: AgentSpec, source_system: str | None) -> AgentSpec | None:
    """`target` as a consent recorded before the switch tool was offered (#1438) names it."""
    return _with_daimon_tools_dropped(
        target, source_system, frozenset({_SWITCH_TOOL_DROP.rsplit(":", 1)[1]})
    )


def _regained(target: AgentSpec, consent: _BackendDropConsent) -> frozenset[str] | None:
    """Daimon tools `consent` named lost that `target` offers again, or None.

    None when `target` drops anything `consent` did not accept, or regains
    anything but a tool Daimon cards per call: a gain the person never saw asks
    again. A relay connector with a per-person grant would count here too.
    """
    from daimon.core.tool_safety import host_call_carded_tools, self_carded_tools

    if not set(target.declared_drops) <= set(consent.declared_drops):
        return None
    prefix = f"mcp_tool:{DAIMON_SERVER_NAME}:"
    carded = host_call_carded_tools() | self_carded_tools()
    regained = set(consent.declared_drops) - set(target.declared_drops)
    tools = {drop.removeprefix(prefix) for drop in regained}
    if not all(drop.startswith(prefix) for drop in regained) or not tools <= carded:
        return None
    return frozenset(tools)


def _regains_carded(
    target: AgentSpec, source_system: str | None, consent: _BackendDropConsent
) -> bool:
    """`target` is the confirmed one except for Daimon tools it offers again behind a card.

    With the host call gate on a provider turn runs a gated tool after a press,
    so a consent that named it lost still holds: the rest of the target, its
    model and MCP URL included, must still digest to the confirmed one.
    """
    tools = _regained(target, consent)
    if not tools:
        return False
    rebuilt = _with_daimon_tools_dropped(target, source_system, tools)
    return rebuilt is not None and _same_target(rebuilt, consent)


def with_tools_dropped(target: AgentSpec, source_system: str | None) -> AgentSpec | None:
    """`target` as a consent recorded before a template kept the source's tools names it.

    That consent (before #1483) declared `tools` a whole loss: no tools on the
    target, `tools` among its drops, and neither of today's granular web losses
    (an OpenAI consent already named web fetch). Only that difference is rebuilt;
    the copy then runs with its tools from now on.
    """
    from daimon.core.turn.runtime_manifest import compose_source_instructions

    if "tools" in target.declared_drops or not target.tools:
        return None
    from mux.profiles.openai import PERSISTENT_WORKSPACE

    granular: set[str] = {"builtin_tool:web_search"}
    if target.model.provider != PERSISTENT_WORKSPACE.provider:
        granular.add("builtin_tool:web_fetch")
    drops = tuple(sorted({*target.declared_drops, "tools"} - granular))
    return target.model_copy(
        update={
            "tools": (),
            "declared_drops": drops,
            "system": compose_source_instructions(
                source_system, "Unavailable capabilities: " + ", ".join(drops) + "."
            ),
        }
    )


def _consented_before(
    target: AgentSpec, source_system: str | None, consent: _BackendDropConsent
) -> bool:
    """A consent recorded before a template kept the source's tools (#1483) or
    offered the switch tool (#1438).

    Either difference alone, or both, is rebuilt; anything else still needs a
    fresh confirmation.
    """
    without_tools = with_tools_dropped(target, source_system)
    candidates = (
        _with_switch_tool_dropped(target, source_system),
        without_tools,
        _with_switch_tool_dropped(without_tools, source_system) if without_tools else None,
    )
    return any(
        _same_target(candidate, consent) for candidate in candidates if candidate is not None
    )
