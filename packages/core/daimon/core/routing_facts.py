"""The routing truth: mention gating and one-bot-not-one-per-agent.

These sentences exist here, once, because both the chat tool that sets a
default (`set_agent_default` / `clear_agent_default`) and the setup panel that
sets one must say the same thing about how routing actually works — and a
claim about routing that lives in prose somewhere else drifts from the code
that makes it true. The deliberate design is for the product to answer the
routing question at the moment the belief forms (right after a default is
set or cleared), rather than for a system prompt to assert it up front.

Everything here is pure: no I/O, no clock, same inputs always produce the
same output.
"""

from __future__ import annotations

from typing import Final


def build_set_default_note(*, agent_name: str, scope_label: str) -> str:
    """Routing truth shown after a default is set at ``scope_label``.

    States both facts: members reach ``agent_name`` only by @mentioning the
    bot in the channel, and there is one bot user for the whole workspace —
    not one per agent — so setting a default did not add a new bot member.
    """
    return (
        f"{agent_name} is now the default for {scope_label}. Members still "
        f"reach it only by @mentioning the bot in that channel — there is one "
        f"bot for the whole workspace, not one bot per agent, so this did not "
        f"add a new member to the server."
    )


def build_clear_default_note(*, scope_label: str, cleared: bool) -> str:
    """Routing truth shown after a default is cleared at ``scope_label``.

    When ``cleared`` is False, ``scope_label`` had no default and nothing
    changed. Otherwise states that ``scope_label`` no longer has a default,
    that resolution now falls through to the next tier, and repeats the
    mention requirement.
    """
    if not cleared:
        return f"{scope_label} had no default agent to clear — nothing changed."
    return (
        f"{scope_label} no longer has a default agent; resolution now falls "
        f"through to the next tier. Whichever agent answers is still reached "
        f"only by @mentioning the bot in that channel — there is one bot for "
        f"the whole workspace, not one bot per agent."
    )


def build_resolution_note(*, agent_name: str | None, tier: str | None, channel_id: str) -> str:
    """Explain which agent answers in ``channel_id`` and which tier decided it.

    The tier is the whole point: "daimon answers here" is not actionable, but
    "daimon answers here because the workspace default says so, and this channel
    has no setting of its own" tells the caller exactly where to change it.
    """
    if agent_name is None:
        return (
            f"No agent resolves for channel {channel_id} — the channel, the "
            f"workspace, and the deployment default are all unset, so a mention "
            f"there has nothing to answer it."
        )
    where = {
        "channel": f"this channel's own default (channel {channel_id})",
        "tenant": "the workspace default, since this channel has none of its own",
        "deployment": (
            "the deployment default, since neither this channel nor the workspace sets one"
        ),
    }.get(tier or "", "an unrecognised tier")
    return (
        f"{agent_name} answers in channel {channel_id}, from {where}. Members "
        f"reach it only by @mentioning the bot there — there is one bot for the "
        f"whole workspace, not one bot per agent."
    )


# The one-line statement of "nothing routes to this agent", rendered by the
# Discord and Slack setup panels and by the MCP create/fork outcome. Splitting
# the fact from the next step lets a caller that knows the channel name append a
# concrete request instead of the generic one.
UNROUTED_LINE: Final = "Not answering in any channel yet."
UNROUTED_NEXT_STEP: Final = "An admin can ask Daimon to make it answer in a channel."
PRECEDENCE_LINE: Final = "A channel's own choice beats the workspace default."


def build_routing_request(*, agent_name: str, channel_label: str) -> str:
    """The sentence a person says to Daimon to route ``agent_name`` somewhere.

    Rendered on its own under the routing map, where it is a standalone
    instruction rather than the tail of a longer sentence.
    """
    return f"Make {agent_name} answer in {channel_label}."


def build_unrouted_note(*, agent_name: str, channel_label: str | None, is_admin: bool) -> str:
    """State that ``agent_name`` is routed nowhere, plus the next step.

    With ``channel_label`` known the next step is the exact request to make,
    in the caller's own voice: an admin can say it themselves, a member has to
    ask one. With no channel in hand there is nothing concrete to name, so the
    generic next step stands in.
    """
    if channel_label is None:
        return f"{UNROUTED_LINE}\n{UNROUTED_NEXT_STEP}"
    lead = "Tell Daimon" if is_admin else "An admin can tell Daimon"
    return f"{UNROUTED_LINE}\n{lead}: make {agent_name} answer in {channel_label}."
