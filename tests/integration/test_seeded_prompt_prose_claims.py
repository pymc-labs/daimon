"""Prose drift gate: the seeded prompt asserts no routing mechanism.

The existing gate, `test_seeded_prompt_tool_claims.py`, checks that every
tool name the seeded prompt cites is reachable from a chat turn — but it
cannot see a prose claim about mechanism, since a sentence like "invite the
agent to your channel" contains no tool name to check against the live
registry. Both observed routing defects this phase fixes (#23) were exactly
that shape: prose asserting a routing mechanism the tool surface actually
contradicts. This file is the narrower, separate gate for that failure
shape: the seeded system prompt must carry no behavior-asserting routing
prose. The routing truth is delivered instead by the
`set_agent_default`/`clear_agent_default` tool result and by the
`workspace-setup` skill.
"""

from __future__ import annotations

import re
from pathlib import Path

from daimon.core.defaults.loader import load_agent_specs, load_skill_paths, load_skill_spec

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS = REPO_ROOT / "defaults"

# Each fragment covers one of the two observed false claims (an invite flow;
# mentionless routing) or a close paraphrase, plus the one-bot-per-agent
# claim named alongside them. Matched as a lowercase substring against a
# whitespace-collapsed, lowercased prompt string. The reason is printed on
# failure so a future author sees WHY the phrase is forbidden and where the
# truth actually lives (the tool result / the workspace-setup skill).
_FORBIDDEN_ROUTING_PHRASES: tuple[tuple[str, str], ...] = (
    (
        "invite the agent",
        "false invite-flow claim (#23) — there is one bot for the whole "
        "workspace; nothing needs inviting per agent. See routing_facts.py "
        "and the workspace-setup skill.",
    ),
    (
        "invite the bot",
        "false invite-flow claim (#23), paraphrased",
    ),
    (
        "invite daimon",
        "false invite-flow claim (#23), naming the seeded agent directly",
    ),
    (
        "add the bot to your channel",
        "false invite-flow paraphrase — no per-agent bot is ever added",
    ),
    (
        "adding the agent to",
        "false invite-flow paraphrase — no per-agent bot is ever added",
    ),
    (
        "added as a separate bot",
        "false one-bot-per-agent claim — there is one bot for the whole workspace",
    ),
    (
        "a separate bot for",
        "false one-bot-per-agent claim",
    ),
    (
        "a bot for each agent",
        "false one-bot-per-agent claim",
    ),
    (
        "one bot per agent",
        "states the routing mechanism directly — even stated correctly, this "
        "belongs in the tool result / workspace-setup skill, not the prompt, "
        "per the delete-don't-correct rule (prose that asserts behavior rots)",
    ),
    (
        "without mentioning",
        "false mentionless-routing claim (#23) — a mention is always required",
    ),
    (
        "without being mentioned",
        "false mentionless-routing claim (#23)",
    ),
    (
        "respond without a mention",
        "false mentionless-routing claim (#23)",
    ),
    (
        "no need to mention",
        "false mentionless-routing claim (#23)",
    ),
)


def _normalize(text: str) -> str:
    """Whitespace-collapsed, lowercased — so a wrapped sentence still matches."""
    return re.sub(r"\s+", " ", text).strip().lower()


def _load_seeded_prompt() -> str:
    specs = load_agent_specs(DEFAULTS / "agents")
    daimon = next(s for s in specs if s.name == "daimon")
    return daimon.system or ""


def _load_workspace_setup_body() -> str:
    for skill_dir in load_skill_paths(DEFAULTS / "skills"):
        spec, body = load_skill_spec(skill_dir)
        if spec.name == "workspace-setup":
            return body
    raise AssertionError("workspace-setup skill not found under defaults/skills")


def test_seeded_prompt_carries_no_behavior_asserting_routing_prose() -> None:
    """Regression coverage note (verified manually, not asserted here — the
    check needs a live edit to prove it isn't vacuous): temporarily adding
    one of the forbidden phrases above to `defaults/agents/daimon.yaml`'s
    system block makes this test fail and names the offending phrase and its
    reason in the failure message. Confirmed during development, then
    reverted before this file was committed."""
    prompt = _normalize(_load_seeded_prompt())
    offending = [
        f"{phrase!r}: {reason}" for phrase, reason in _FORBIDDEN_ROUTING_PHRASES if phrase in prompt
    ]
    assert not offending, (
        "the seeded prompt asserts a routing mechanism the tool surface can "
        "contradict — delete it, don't correct it:\n" + "\n".join(offending)
    )


def test_workspace_setup_skill_still_carries_the_routing_facts() -> None:
    """Relocating the routing facts out of the prompt must not silently lose
    them — they must land in the `workspace-setup` skill instead."""
    body = _normalize(_load_workspace_setup_body())
    assert "mention" in body, "workspace-setup skill must state the mention requirement"
    assert "one bot" in body, (
        "workspace-setup skill must state the single-bot (not one per agent) fact"
    )


def test_routing_facts_module_is_imported_by_both_delivery_surfaces() -> None:
    """The payload delivery cannot be silently removed without this gate
    noticing: both surfaces that render the routing truth must still import
    the shared core source. Reads source text directly rather than
    importing the modules, so this stays a plain static check."""
    mcp_source = (
        REPO_ROOT / "packages/adapters/mcp/daimon/adapters/mcp/tools/propagation.py"
    ).read_text()
    discord_routing_view_source = (
        REPO_ROOT / "packages/adapters/discord/daimon/adapters/discord/agent_setup/routing_view.py"
    ).read_text()
    slack_source = (
        REPO_ROOT / "packages/adapters/slack/daimon/adapters/slack/agent_setup/panel_views.py"
    ).read_text()
    assert "daimon.core.routing_facts" in mcp_source, (
        "mcp/tools/propagation.py must import daimon.core.routing_facts"
    )
    assert "daimon.core.routing_facts" in discord_routing_view_source, (
        "discord/agent_setup/routing_view.py must import daimon.core.routing_facts"
    )
    assert "daimon.core.routing_facts" in slack_source, (
        "slack/agent_setup/panel_views.py must import daimon.core.routing_facts"
    )
