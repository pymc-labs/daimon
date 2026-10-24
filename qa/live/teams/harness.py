"""Raw Microsoft Graph access for agent-driven Teams QA."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote

import httpx

from qa.live.teams.graph import GraphRequestError, GraphTeamsDriver
from qa.live.teams.tokens import ReauthRequired


def _error(exc: Exception, persona: str) -> dict[str, Any]:
    if isinstance(exc, ReauthRequired):
        return {"ok": False, "error": "reauth_required", "persona": persona}
    if isinstance(exc, ValueError):
        return {"ok": False, "error": str(exc), "persona": persona}
    if isinstance(exc, GraphRequestError):
        return {"ok": False, "error": "graph_request_failed", "persona": persona}
    raise exc


def _persona(driver: GraphTeamsDriver, persona: str) -> None:
    if persona not in driver.config.personas:
        raise ValueError("unknown_persona")


GRAPH_DEFAULT = ["https://graph.microsoft.com/.default"]
GRAPH_METHODS = ("GET", "POST", "PATCH", "PUT", "DELETE")
GRAPH_ROOT = "https://graph.microsoft.com"
MAX_BODY_CHARS = 200_000


def _graph_path(path: str) -> tuple[str, str]:
    """Split a Graph path into its API version and a version-free path."""
    if path.startswith(GRAPH_ROOT):
        path = path[len(GRAPH_ROOT) :]
    version = "v1.0"
    for prefix in ("/v1.0/", "/beta/"):
        if path.startswith(prefix):
            version, path = prefix.strip("/"), path[len(prefix) - 1 :]
    if not path.startswith("/") or "://" in path or "#" in path or _escapes(path.split("?", 1)[0]):
        raise ValueError("invalid_path")
    return version, path


def _escapes(bare: str) -> bool:
    """Whether a path climbs or hides a separator, even percent-encoded (``%2e%2e``, ``%2f``)."""
    decoded = bare
    while (once := unquote(decoded)) != decoded:
        decoded = once
    hidden_separator = decoded.count("/") != bare.count("/")
    return ".." in decoded or "\\" in decoded or hidden_separator


def _team_upload(path: str, team_id: str) -> bool:
    """Whether ``path`` puts one file's content into the QA team's library, and nothing else.

    Only content uploads: sharing (``invite``, ``createLink``, ``permissions``) and
    ``copy`` live under the same drive and could reach people or other drives.
    """
    drive = re.escape(f"/groups/{team_id}/drive".casefold())
    return re.fullmatch(drive + r"/(root:/[^:]+:|items/[^/]+)/content", path) is not None


def _within(path: str, root: str) -> bool:
    bare = path.split("?", 1)[0].casefold()
    root = root.casefold()
    return bare == root or bare.startswith(root + "/")


async def graph(
    driver: GraphTeamsDriver,
    persona: str,
    method: str,
    path: str,
    body: str | None = None,
    *,
    upload: tuple[bytes, str] | None = None,
) -> dict[str, Any]:
    """Make one Graph call as a persona; writes stay in the QA team, its files or its bot chat.

    ``upload`` sends raw bytes with their content type instead of a JSON body. It is
    only a ``PUT`` of one file's content into the QA team's document library
    (``/groups/<team>/drive/root:/<path>:/content`` or ``.../items/<id>/content``).
    """
    try:
        _persona(driver, persona)
        method = method.upper()
        if method not in GRAPH_METHODS:
            raise ValueError("invalid_method")
        version, bare = _graph_path(path)
        payload: object = None
        if upload is not None and (body is not None or method != "PUT"):
            raise ValueError("invalid_body")
        if body is not None:
            try:
                payload = json.loads(body)
            except ValueError:
                raise ValueError("invalid_body") from None
        team = f"/teams/{driver.config.team_id}"
        bare_path = bare.split("?", 1)[0].rstrip("/").casefold()
        if (method == "DELETE" and bare_path == team.casefold()) or bare_path == (
            f"{team}/archive".casefold()
        ):
            raise ValueError("qa_team_protected")
        uploading = upload is not None
        if uploading and not _team_upload(bare_path, driver.config.team_id):
            raise ValueError("write_outside_qa_team")
        if method != "GET" and not (uploading or _within(bare, team)):
            chat_id, _ = await driver.bot_chat(persona)
            if not _within(bare, f"/chats/{chat_id}"):
                raise ValueError("write_outside_qa_team")
        token = driver.tokens.acquire(persona, GRAPH_DEFAULT)
        headers = {"Authorization": f"Bearer {token.value}"}
        if upload is not None:
            headers["Content-Type"] = upload[1]
        try:
            response = await driver.http.request(
                method,
                f"{GRAPH_ROOT}/{version}{bare}",
                headers=headers,
                json=payload,
                content=None if upload is None else upload[0],
                timeout=30,
            )
        except httpx.RequestError:
            error = "post_outcome_unknown" if method != "GET" else "graph_request_failed"
            return {"ok": False, "error": error, "persona": persona}
        try:
            content: object = response.json() if response.content else None
        except ValueError:
            content = response.text
        text = json.dumps(content)
        result: dict[str, Any] = {
            "ok": not response.is_error,
            "persona": persona,
            "status": response.status_code,
            "body": content,
        }
        if len(text) > MAX_BODY_CHARS:
            result["body"] = text[:MAX_BODY_CHARS]
            result["truncated"] = True
        return result
    except (ReauthRequired, GraphRequestError, ValueError) as exc:
        return _error(exc, persona)
