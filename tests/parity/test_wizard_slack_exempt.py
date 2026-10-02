"""Executable record of the wizard's deliberate Slack exemption.

An absent capability file in this suite is mechanically indistinguishable
from an oversight: nothing else in the test suite would fail if a Slack
wizard renderer were built without anyone updating the platform-neutral
core's documented scope. Asserting the exemption here, rather than merely
documenting it in a comment, makes that omission fail loudly instead of
silently.

No platform parametrization, no database -- this is a scope check, not a
scenario test.
"""

from __future__ import annotations

import importlib.util

import daimon.core.wizard


def test_no_slack_wizard_module_exists() -> None:
    assert importlib.util.find_spec("daimon.adapters.slack.wizard") is None, (
        "a Slack wizard renderer has appeared -- if a Slack wizard is ever built, "
        "this exemption record must be replaced rather than deleted. The "
        "platform-neutral screen description daimon.core.wizard produces is the "
        "seam a Slack renderer would attach to."
    )


def test_teams_wizard_module_exists() -> None:
    assert importlib.util.find_spec("daimon.adapters.teams.wizard") is not None


def test_wizard_core_documents_the_slack_exemption() -> None:
    doc = daimon.core.wizard.__doc__

    assert doc is not None, "daimon.core.wizard must carry a package docstring"
    assert "Slack is deliberately left out" in doc, (
        "the package docstring must state the wizard's Slack exemption in its "
        "own terms, not just in a planning document"
    )
    assert "test_wizard_slack_exempt" in doc, (
        "the package docstring must name this test file as the executable record "
        "of the exemption, so prose and assertion don't drift apart"
    )
