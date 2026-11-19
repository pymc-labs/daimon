"""Seeded GitHub guidance keeps filesystem and token access distinct."""

from pathlib import Path

from daimon.core.agent_guidance import CREDENTIAL_GUIDANCE_BLOCK

ROOT = Path(__file__).resolve().parents[2]


def test_seeded_guidance_describes_one_working_repo_and_token_repos() -> None:
    prompt = (ROOT / "defaults/agents/default.yaml").read_text()
    setup = (ROOT / "defaults/skills/workspace-setup/SKILL.md").read_text()
    cli_auth = (ROOT / "defaults/skills/cli-auth/SKILL.md").read_text()
    # The seeded prompt points at the guidance block reconcile prepends to it,
    # so the GitHub rules live in one place for every agent.
    for body in (setup, CREDENTIAL_GUIDANCE_BLOCK):
        assert "one working repo" in body
        assert "/workspace/<owner>/<repo>" in body
        assert "GitHub App" in body
    assert 'GitHub rules are under "Your credentials & capabilities" above' in prompt
    assert "Your credentials & capabilities" in CREDENTIAL_GUIDANCE_BLOCK
    assert "set_working_repo" in setup
    assert "remove_repo" in setup
    assert "A clear ask to add, remove or set the working repo is enough" in prompt
    assert "The Connect GitHub page shows" in CREDENTIAL_GUIDANCE_BLOCK
    assert "The Connect GitHub page shows all of this agent's repos:" in setup
    assert "then select Save" in CREDENTIAL_GUIDANCE_BLOCK
    assert "In chat, make one change on a clear ask" in CREDENTIAL_GUIDANCE_BLOCK
    flat = " ".join(CREDENTIAL_GUIDANCE_BLOCK.split())
    assert "the page shows whether it is on this agent's list" in flat
    assert "the GitHub account they signed in with can't open it" in flat
    assert "none" in setup
    assert "GH_TOKEN" in cli_auth


def test_tool_catalogue_describes_working_repo_separately() -> None:
    catalogue = (ROOT / "docs/mcp-tools.md").read_text()
    assert "| `set_working_repo` |" in catalogue
    assert "| `remove_repo` |" in catalogue
    assert "Choose the one repo in this agent's filesystem" in catalogue
    assert "Give this agent token access to more repos" in catalogue
    assert "Ask for token access to a repo" in catalogue
