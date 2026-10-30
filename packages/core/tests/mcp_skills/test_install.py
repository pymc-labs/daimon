"""Reading one MCP-served skill for `add_skill` (`daimon.core.mcp_skills.install`)."""

from __future__ import annotations

import pytest
from daimon.core.agent_mcp_credentials import ResolvedMcpCredential
from daimon.core.config import SignedIdentityServer
from daimon.core.mcp_skills.install import fetch_server_skill, server_access
from daimon.core.mcp_skills.manifest import SkillRejected
from daimon.core.mcp_skills.protocol import (
    MAX_LISTING_BYTES,
    PROTOCOL_VERSION,
    SKILLS_EXTENSION,
    Endpoint,
)
from daimon.core.skills.ingest import SkillIngestError
from daimon.testing.mcp_skills import SkillsServer, skill_md

URL = "https://skills.example.com/mcp"
ENDPOINT = Endpoint(url=URL, bearer="tok-1")
RELAY_BASE = "https://daimon.example.com/mcp-identity/"
TICKETS = SignedIdentityServer(
    name="tickets", url="https://tickets.example.com/mcp", issuer="daimon", audience="tickets"
)


def _server() -> SkillsServer:
    return SkillsServer(
        skills={
            "release-notes": {"SKILL.md": skill_md("release-notes"), "logo.bin": b"\x00\xff"},
            "triage": {"SKILL.md": skill_md("triage"), "scripts/run.sh": b"echo hi\n"},
        },
        dynamic=("live-report",),
    )


@pytest.mark.parametrize("skill", ["triage", SkillsServer.uri("triage")])
async def test_a_skill_is_found_by_name_or_uri_and_every_file_verified(skill: str) -> None:
    """The listing is walked, then each manifest file read once and checked."""
    server = _server()

    async with server.client() as http:
        fetched = await fetch_server_skill(http, ENDPOINT, server="docs", skill=skill)

    assert fetched.files == {
        "SKILL.md": skill_md("triage"),
        "scripts/run.sh": b"echo hi\n",
    }, "every listed file should be read"
    assert fetched.origin("docs").startswith(f"mcp docs: {SkillsServer.uri('triage')} sha256:"), (
        "the origin names the server, the URI and the manifest digest"
    )
    assert server.methods().count("skills/list") == 3, "every listing page should be walked"
    assert server.methods().count("resources/read") == 2, "only this skill's files are read"
    for method, params, headers in server.requests:
        meta = params["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == PROTOCOL_VERSION, method
        assert meta["io.modelcontextprotocol/clientCapabilities"] == {
            "extensions": {SKILLS_EXTENSION: {}}
        }, "every request should declare the skills extension"
        assert headers["mcp-method"] == method, "the method should be mirrored into a header"
        assert headers["authorization"] == "Bearer tok-1", "the endpoint's token should be sent"
        if method == "resources/read":
            assert headers["mcp-name"] == params["uri"], "a read should name its URI"


async def test_a_blob_file_is_decoded() -> None:
    """Binary files arrive base64-encoded and are kept as bytes."""
    async with _server().client() as http:
        fetched = await fetch_server_skill(http, ENDPOINT, server="docs", skill="release-notes")

    assert fetched.files["logo.bin"] == b"\x00\xff", "blobs should be decoded"


async def test_a_file_that_fails_its_digest_is_rejected() -> None:
    """Content that does not match the manifest never becomes a skill."""
    server = _server()
    server.served[SkillsServer.uri("triage", "scripts/run.sh")] = b"rm -rf\n!"

    async with server.client() as http:
        with pytest.raises(SkillRejected, match="digest does not match"):
            await fetch_server_skill(http, ENDPOINT, server="docs", skill="triage")


async def test_frontmatter_that_differs_from_the_listing_is_rejected() -> None:
    """`SKILL.md` must say what the listing says it does."""
    server = _server()
    server.skills["triage"]["SKILL.md"] = skill_md("triage").replace(b"Helps", b"Hides")

    async with server.client() as http:
        with pytest.raises(SkillRejected, match="frontmatter does not match"):
            await fetch_server_skill(http, ENDPOINT, server="docs", skill="triage")


@pytest.mark.parametrize(
    ("skill", "status", "message"),
    [
        ("live-report", 200, "built per request"),
        ("missing", 200, "serves no skill 'missing'"),
        ("triage", 400, "does not serve skills"),
        ("triage", 500, "Try again later"),
    ],
)
async def test_what_cannot_be_added_says_why(skill: str, status: int, message: str) -> None:
    """Dynamic, unknown, a server of an earlier revision, or a failing server: nothing."""
    server = _server()
    server.status = status

    async with server.client() as http:
        with pytest.raises(SkillIngestError, match=message):
            await fetch_server_skill(http, ENDPOINT, server="docs", skill=skill)
    assert "resources/read" not in server.methods(), "no file should be read"


async def test_a_listing_past_its_byte_budget_is_refused() -> None:
    """Pages under the per-page cap that together pass the budget stop the walk."""
    server = _server()
    server.padding = MAX_LISTING_BYTES // 3

    async with server.client() as http:
        with pytest.raises(SkillIngestError, match="Try again later"):
            await fetch_server_skill(http, ENDPOINT, server="docs", skill="live-report")
    assert server.methods().count("skills/list") == 3, "the third page should pass the budget"


async def test_a_stalling_server_hits_the_deadline() -> None:
    """One deadline bounds the whole fetch, however slowly the server answers."""
    server = _server()
    server.stall = 1.0

    async with server.client() as http:
        with pytest.raises(SkillIngestError, match="took too long"):
            await fetch_server_skill(http, ENDPOINT, server="docs", skill="triage", deadline_s=0.05)


def test_server_access_uses_the_shared_token_or_the_relay() -> None:
    """A shared token reaches the server itself; a relay URL reaches the configured server."""
    credential = ResolvedMcpCredential(mcp_server_url=URL + "/", token="tok-1", version="v1")
    signed: list[str] = []

    def relay_token(configured: SignedIdentityServer) -> str:
        signed.append(configured.name)
        return "signed"

    def access(url: str, credentials: tuple[ResolvedMcpCredential, ...] = ()) -> Endpoint:
        return server_access(
            server="docs",
            url=url,
            credentials=credentials,
            relay_base=RELAY_BASE,
            relay_servers={"tickets": TICKETS},
            relay_token=relay_token,
        ).endpoint

    assert access(URL, (credential,)) == Endpoint(URL, "tok-1"), "the shared token is used"
    assert access(RELAY_BASE + "tickets") == Endpoint(TICKETS.url, "signed"), "relay is signed"
    assert signed == ["tickets"], "only the relay server is signed for"
    with pytest.raises(SkillIngestError, match="signs each person in"):
        access(URL)
    with pytest.raises(SkillIngestError, match="cannot sign"):
        access(RELAY_BASE + "unknown")
