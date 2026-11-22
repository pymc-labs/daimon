"""The artifacts skill and docs page state the limits, hosts and tools the code enforces."""

from __future__ import annotations

from pathlib import Path

from daimon.core.artifact_pages.content import ASSET_HOSTS
from daimon.core.artifact_pages.model import Limits
from daimon.core.capability_tools import CAPABILITY_TOOLS
from daimon.core.config import ArtifactPagesSettings
from daimon.core.continuity.page_events import MAX_EVENT_BYTES

ROOT = Path(__file__).resolve().parents[2]
SKILL = (ROOT / "defaults/skills/artifacts/SKILL.md").read_text()
DOCS = (ROOT / "docs/artifacts.md").read_text()
MIB, KIB = 1024 * 1024, 1024
LIMITS = ArtifactPagesSettings()  # the defaults the docs state

SKILL_CLAIMS = (
    f"{LIMITS.html_bytes // MIB} MiB of HTML",
    f"{LIMITS.document_bytes // KIB} KiB per document",
    f"{LIMITS.documents:,} documents and {LIMITS.data_bytes // MIB} MiB",
    f"{LIMITS.list_bytes // MIB} MiB per list reply",
    f"{LIMITS.events:,} events ({LIMITS.events_bytes // MIB} MiB)",
    f"{LIMITS.requests_per_minute} calls a minute",
    f"past {LIMITS.asks_per_minute} a minute",
    f"({LIMITS.versions_kept} kept)",
    f"up to {LIMITS.event_bytes // KIB} KiB",
    f"event over {MAX_EVENT_BYTES // KIB} KiB",
)
DOCS_CLAIMS = (
    f"| Page HTML | {LIMITS.html_bytes // MIB} MiB |",
    f"| Versions kept | {LIMITS.versions_kept} |",
    f"| One document | {LIMITS.document_bytes // KIB} KiB |",
    f"| Documents per page | {LIMITS.documents:,}, times",
    f"| Data per page | {LIMITS.data_bytes // MIB} MiB, times",
    f"| One event | {LIMITS.event_bytes // KIB} KiB |",
    f"| Page API calls | {LIMITS.requests_per_minute} a minute",
    f"| Questions to the agent | {LIMITS.asks_per_minute} a minute |",
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
