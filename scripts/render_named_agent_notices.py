"""Write sample Discord and Slack named-agent notice payloads as JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import discord
from daimon.adapters.discord.named_agent_notices import build_named_agent_notice
from daimon.adapters.slack.mrkdwn import escape_mrkdwn
from daimon.adapters.slack.named_agent_notices import build_named_agent_blocks
from daimon.core.turn.errors import NamedAgentRefused


def render(output_dir: Path) -> None:
    samples = {
        "two_agents": NamedAgentRefused(kind="two"),
        "unavailable": NamedAgentRefused(kind="unavailable"),
        "setup_thread": NamedAgentRefused(
            kind="setup", current_name="Daimon", named_name="Planner"
        ),
        "existing_thread": NamedAgentRefused(
            kind="thread",
            current_name="Daimon",
            named_name="Planner",
            hand_over_agent_id="ag_planner",
            hand_over_agent_name="Planner",
        ),
        "own_channel": NamedAgentRefused(kind="own", current_name="Daimon"),
    }
    for platform in ("discord", "slack"):
        (output_dir / platform).mkdir(parents=True, exist_ok=True)
    for name, err in samples.items():
        discord_payload = {
            "components": build_named_agent_notice(err).to_components(),
            "allowed_mentions": discord.AllowedMentions.none().to_dict(),
        }
        slack_payload = {"text": escape_mrkdwn(str(err)), "blocks": build_named_agent_blocks(err)}
        for platform, payload in (("discord", discord_payload), ("slack", slack_payload)):
            (output_dir / platform / f"{name}.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    render(parser.parse_args().output_dir)
