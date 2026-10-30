"""A stateless MCP server that declares the Skills extension, for `add_skill` tests."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from daimon.core.mcp_skills.protocol import PROTOCOL_VERSION, SKILLS_EXTENSION


def skill_md(name: str) -> bytes:
    """A `SKILL.md` whose frontmatter matches what `SkillsServer` lists for `name`."""
    return f"---\nname: {name}\ndescription: Helps with {name}.\n---\nDo {name}.\n".encode()


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


@dataclass
class SkillsServer:
    """Serves `skills` (name -> path -> bytes) one listing entry a page.

    `served` replaces a file's content by URI, `dynamic` lists skills without a
    manifest, and `status` answers every request with that status instead.
    Files ending `.bin` are served as blobs. Every request is kept in `requests`.
    """

    skills: dict[str, dict[str, bytes]]
    served: dict[str, bytes] = field(default_factory=dict[str, bytes])
    dynamic: tuple[str, ...] = ()
    status: int = 200
    requests: list[tuple[str, dict[str, Any], httpx.Headers]] = field(
        default_factory=list[tuple[str, dict[str, Any], httpx.Headers]]
    )

    @staticmethod
    def uri(name: str, path: str = "SKILL.md") -> str:
        return f"skill://example/{name}/{path}"

    def methods(self) -> list[str]:
        return [method for method, _, _ in self.requests]

    def entries(self) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = [
            {
                "uri": self.uri(name),
                "frontmatter": {"name": name, "description": f"Helps with {name}."},
                "resources": [
                    {"uri": self.uri(name, path), "digest": _digest(body), "size": len(body)}
                    for path, body in sorted(files.items())
                ],
            }
            for name, files in self.skills.items()
        ]
        return listed + [
            {
                "uri": self.uri(name),
                "frontmatter": {"name": name, "description": "Built per request."},
                "resources": "dynamic",
            }
            for name in self.dynamic
        ]

    def _read(self, uri: str) -> dict[str, Any]:
        body = self.served.get(uri)
        if body is None:
            name, path = uri.removeprefix("skill://example/").split("/", 1)
            body = self.skills[name][path]
        if uri.endswith(".bin"):
            return {"uri": uri, "blob": base64.b64encode(body).decode()}
        return {"uri": uri, "mimeType": "text/markdown", "text": body.decode()}

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        self.requests.append((method, params, request.headers))
        if self.status != 200:
            return httpx.Response(self.status)
        result: dict[str, Any]
        if method == "server/discover":
            result = {
                "supportedVersions": [PROTOCOL_VERSION],
                "capabilities": {"resources": {}, "extensions": {SKILLS_EXTENSION: {}}},
            }
        elif method == "skills/list":
            start = int(params.get("cursor", "0"))
            listed = self.entries()
            result = {"skills": listed[start : start + 1], "ttlMs": 60_000}
            if start + 1 < len(listed):
                result["nextCursor"] = str(start + 1)
        elif method == "resources/read":
            result = {"contents": [self._read(params["uri"])]}
        else:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": method},
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def client(self, *, verify: object = True) -> httpx.AsyncClient:
        """A client whose every request this server answers."""
        del verify
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
