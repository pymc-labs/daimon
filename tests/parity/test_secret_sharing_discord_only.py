"""Executable record: sharing a secret with a person works on Discord only.

The share is a DM card with a Reveal button that answers privately, once. Slack and
Teams have no port of that card yet, so the feature, the command and the tool are
Discord's alone. If another platform gains it, update this record rather than delete it.
"""

from __future__ import annotations

from daimon.core.capability_tools import CAPABILITY_TOOLS
from daimon.core.features.registry import FEATURES
from daimon.core.platform_words import WORDS


def test_the_feature_the_command_and_the_tool_are_discords_alone() -> None:
    assert FEATURES["secret_sharing"].platforms == frozenset({"discord"})
    assert WORDS["discord"].commands["share_secret"].word == "/share-secret"
    assert all("share_secret" not in WORDS[p].commands for p in ("slack", "teams"))
    assert CAPABILITY_TOOLS["secret_sharing"] == frozenset({"share_secret_with"})
