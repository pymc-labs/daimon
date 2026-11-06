"""Seeded agent prompts and skills make no GitHub access claim the guidance contradicts.

A contract test, written after `dev_agent` kept telling its agent that "the
repo's git remote is already authenticated" and to push and open PRs through the
GitHub MCP, long after the per-installation token change and the GitHub router
made both wrong. `test_github_access_surfaces` checks the claims in the core
guidance; nothing checked the prompts and skills Daimon seeds.

Every sentence in a seeded prompt or skill that makes a GitHub access claim must
be backed: either by a claim the core guidance already maps to a test
(`GITHUB_MCP_CLAIMS`), or by an entry in `SEEDED_GITHUB_CLAIMS` below.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import yaml
from daimon.core.agent_guidance import CREDENTIAL_GUIDANCE_BLOCK

from .test_github_access_surfaces import (
    GITHUB_MCP_CLAIMS,
    _is_github_mcp_claim,  # pyright: ignore[reportPrivateUsage]
    _sentences,  # pyright: ignore[reportPrivateUsage]
    _test_exists,  # pyright: ignore[reportPrivateUsage]
)

_DEFAULTS = Path(__file__).resolve().parents[3] / "defaults"
_THIS_TEST = "packages/core/tests/test_seeded_github_claims.py"

#: Claims seeded text may make beyond the core guidance's, each mapped to the
#: test that backs it.
SEEDED_GITHUB_CLAIMS: Mapping[str, str] = {
    "exists, push any other repo with": (
        "packages/core/tests/test_github_access_surfaces.py"
        "::test_every_surface_uses_the_repo_installation_token"
    ),
    # Which token each push or read uses, as the surfaces contract checks.
    "Push the working repo with git and GH_TOKEN": (
        "packages/core/tests/test_github_access_surfaces.py"
        "::test_every_surface_uses_the_repo_installation_token"
    ),
    "For any repo other than the working repo, when that helper exists": (
        "packages/core/tests/test_github_access_surfaces.py"
        "::test_every_surface_uses_the_repo_installation_token"
    ),
    # Which surface reaches which repo: the helper when it exists, else GH_TOKEN.
    "The repo's git remote is not pre-authenticated.": (
        f"{_THIS_TEST}::test_dev_agent_github_wording_matches_the_core_guidance"
    ),
    "never through the GitHub MCP": (
        f"{_THIS_TEST}::test_dev_agent_github_wording_matches_the_core_guidance"
    ),
    "Never push or open pull requests through the GitHub MCP.": (
        f"{_THIS_TEST}::test_dev_agent_github_wording_matches_the_core_guidance"
    ),
}


#: The only tests a seeded claim may cite. A claim needs a test that checks the
#: behaviour it describes; adding a test here is a reviewed change, so mapping a
#: claim to an unrelated test cannot slip through.
BACKING_TESTS = frozenset(
    {
        "packages/core/tests/test_github_access_surfaces.py"
        "::test_every_surface_uses_the_repo_installation_token",
        f"{_THIS_TEST}::test_dev_agent_github_wording_matches_the_core_guidance",
    }
)


def _is_seeded_github_claim(sentence: str) -> bool:
    """A claim about how an agent authenticates to GitHub, or what it pushes through."""
    lowered = sentence.lower()
    if not re.search(r"github|git remote|gh_token|\bgh\b", lowered):
        return False  # a 404 in a data skill is not a GitHub claim
    github_tools = any(tool in lowered for tool in ("github tools", "github mcp", "copilot mcp"))
    tokens = any(word in lowered for word in ("gh_token", "github-repo.sh", "token"))
    access = any(word in lowered for word in ("push", "read", "clone", "cover", "reach"))
    return (
        _is_github_mcp_claim(sentence)
        or (tokens and access)
        or "authenticated" in lowered
        or (github_tools and any(word in lowered for word in ("push", "pull request", " pr ")))
        or ("copilot mcp" in lowered and "repository operations" in lowered)
    )


def _seeded_texts() -> Iterator[tuple[str, str]]:
    """(path relative to the repo, text) for every seeded prompt and skill."""
    for pattern in ("agents/*.yaml", "agents-optional/*.yaml"):
        for path in sorted(_DEFAULTS.glob(pattern)):
            system = yaml.safe_load(path.read_text()).get("system", "")
            yield str(path.relative_to(_DEFAULTS.parent)), system
    for path in sorted(_DEFAULTS.glob("skills/**/SKILL.md")):
        yield str(path.relative_to(_DEFAULTS.parent)), path.read_text()


def _is_backed(claim: str) -> bool:
    return any(key in claim for key in (*GITHUB_MCP_CLAIMS, *SEEDED_GITHUB_CLAIMS))


def test_every_seeded_github_claim_names_a_backing_test() -> None:
    claims = [
        (source, sentence)
        for source, text in _seeded_texts()
        for sentence in _sentences(text)
        if _is_seeded_github_claim(sentence)
    ]
    assert claims, "no seeded text makes a GitHub claim; drop this test with the last one"
    unmapped = [(source, claim) for source, claim in claims if not _is_backed(claim)]
    assert not unmapped, f"map these seeded claims to the test that backs them: {unmapped}"
    for key, node_id in SEEDED_GITHUB_CLAIMS.items():
        assert any(key in claim for _, claim in claims), f"stale seeded claim key: {key!r}"
        assert _test_exists(node_id), f"{node_id} backs {key!r} but does not exist"
        assert node_id in BACKING_TESTS, f"{key!r} cites {node_id}, which is not a backing test"


def test_dev_agent_github_wording_matches_the_core_guidance() -> None:
    """The dev agent's GitHub rules are the core guidance's, not a second version."""
    spec = yaml.safe_load((_DEFAULTS / "agents-optional" / "dev_agent.yaml").read_text())
    prompt = " ".join(spec["system"].split())
    guidance = " ".join(CREDENTIAL_GUIDANCE_BLOCK.split())
    shared = (
        "never through the GitHub MCP",
        "`gh pr create`",
        "x-access-token:${GH_TOKEN}@github.com/owner/repo.git <branch>",
        "/mnt/session/uploads/github-repo.sh owner/repo push <branch>",
    )
    for phrase in shared:
        assert phrase in guidance, f"the core guidance no longer says {phrase!r}"
        assert phrase in prompt, f"dev_agent does not say {phrase!r}"
    assert "not pre-authenticated" in prompt
    assert "The GitHub MCP is still fine for reading." in prompt
    for stale in ("already authenticated", "through the github tools", "Copilot MCP tools"):
        assert stale not in prompt, f"dev_agent still says {stale!r}"
