"""The artifacts skill and docs page state the limits, hosts and tools the code enforces."""

from __future__ import annotations

from pathlib import Path

from daimon.core.artifact_pages.content import ASSET_HOSTS
from daimon.core.artifact_pages.model import Limits
from daimon.core.capability_tools import CAPABILITY_TOOLS
from daimon.core.continuity.page_events import MAX_EVENT_BYTES

ROOT = Path(__file__).resolve().parents[2]
SKILL = (ROOT / "defaults/skills/artifacts/SKILL.md").read_text()
DOCS = (ROOT / "docs/artifacts.md").read_text()
MIB, KIB = 1024 * 1024, 1024

SKILL_CLAIMS = (
    f"{Limits.HTML_BYTES // MIB} MiB of HTML",
    f"{Limits.DOCUMENT_BYTES // KIB} KiB per document",
    f"{Limits.DOCUMENTS:,} documents and {Limits.DATA_BYTES // MIB} MiB",
    f"{Limits.LIST_BYTES // MIB} MiB per list reply",
    f"{Limits.EVENTS:,} events ({Limits.EVENTS_BYTES // MIB} MiB)",
    f"{Limits.API_PER_MINUTE} calls a minute",
    f"past {Limits.ASK_PER_MINUTE} a minute",
    f"({Limits.VERSIONS_KEPT} kept)",
    f"up to {MAX_EVENT_BYTES // KIB} KiB",
)
DOCS_CLAIMS = (
    f"| Page HTML | {Limits.HTML_BYTES // MIB} MiB |",
    f"| Versions kept | {Limits.VERSIONS_KEPT} |",
    f"| One document | {Limits.DOCUMENT_BYTES // KIB} KiB |",
    f"| Documents per page | {Limits.DOCUMENTS:,}, times",
    f"| Data per page | {Limits.DATA_BYTES // MIB} MiB, times",
    f"| One event | {MAX_EVENT_BYTES // KIB} KiB |",
    f"| Page API calls | {Limits.API_PER_MINUTE} a minute",
    f"| Questions to the agent | {Limits.ASK_PER_MINUTE} a minute |",
    f"| People and editors | {Limits.PEOPLE} and {Limits.EDITORS} |",
)


def test_the_skill_and_docs_state_what_the_code_enforces() -> None:
    for claim in SKILL_CLAIMS:
        assert claim in SKILL, f"skill: {claim!r}"
    for claim in DOCS_CLAIMS:
        assert claim in DOCS, f"docs: {claim!r}"
    for host in ASSET_HOSTS:
        name = host.removeprefix("https://")
        assert name in SKILL and name in DOCS, name
    for tool in CAPABILITY_TOOLS["artifacts"]:
        assert tool in SKILL, f"skill: {tool}"
