"""Probe: Phase 88 blocking spike — resolve A1 (per-turn header?) and A3 (per-turn cred re-read?).

This is the BLOCKING spike for Phase 88 (intra-tenant RBAC bypass, #162). The fix
branches on TWO independent unknowns, and this probe answers BOTH in one run:

A1 — Does MA forward any UNDOCUMENTED per-turn caller signal (a header, or
     session/turn metadata that varies per turn) to a URL MCP server?
  - If yes, admin could become a true per-turn property carried
    bot -> session event -> MA -> MCP with ZERO vault writes (option B).
  - If only the static vault `static_bearer` credential reaches the server,
    option B is infeasible -> fall back to option A.

A3 — Does MA RE-READ the vault credential on every turn of a reused session,
     or does it CACHE the credential for the session lifetime?
  - This is the gating unknown for option A's mechanism on the buggy reuse path.
  - If MA re-reads per turn: a mid-session JIT re-mint of the static_bearer is
    honored on the NEXT turn -> 88-02 can re-mint a short-lived admin cred
    before run_turn on admin reuse turns.
  - If MA caches per session: a mid-session re-mint is SILENTLY INERT (a
    non-fix) -> 88-02 MUST force a fresh session for admin-initiated reuse turns.

What we already know from the SDK surface (verified, see RESEARCH.md):
  - The URL MCP server is attached to the agent definition as exactly
    {name, type:"url", url} — no header config, no metadata forwarding field.
  - The session vault `static_bearer` credential is the ONLY thing
    authenticating MA -> MCP. It is updated only via delete + recreate
    (MA blocks PATCH 405 / duplicate POST 409).
  - `SessionCreateParams.metadata` is Dict[str,str] set at create time; our
    sessions are reused per thread, so it is frozen for the reuse path.
  - The per-turn `user.message` event params expose only {content, type} —
    no metadata, no header channel.

So the SDK has NO documented per-turn channel. This probe rules out an
UNDOCUMENTED per-turn header/field (A1) AND empirically determines the cred
re-read behavior (A3) — neither of which static type inspection can prove.

Why this can't run from a bare laptop: MA only calls publicly-reachable URLs.
Expose the capture server via a tunnel (cloudflared/ngrok) OR run against the
deployed daimon-mcp and read its request logs. Set CAPTURE_PUBLIC_URL to the
tunnel URL that forwards to the local capture server started here.

Run (with a tunnel pointing CAPTURE_PUBLIC_URL -> http://127.0.0.1:8788):
    CAPTURE_PUBLIC_URL=https://<your-tunnel>.trycloudflare.com \
      uv run python scripts/probes/managed_agents/mcp_per_turn_caller_metadata.py

Flow: create env + agent (URL MCP server) + vault + static_bearer cred carrying
a DISTINCT sentinel token ("probe-frozen-token-turn1"). Create ONE session.
Drive turn 1. Then DELETE + RECREATE the static_bearer credential at the SAME
URL with a SECOND distinct sentinel token ("probe-reminted-token-turn2"). Drive
turn 2 into the SAME session. The capture server logs every inbound request's
full header set across both turns, and the verdict block reports:

  A1: whether any header beyond `Authorization: Bearer` varies per turn / carries
      caller identity.
  A3: whether turn 2's inbound `Authorization: Bearer` carries the OLD
      (turn-1 = CACHED per session) or NEW (turn-2 = RE-READ per turn) token.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import uuid

import httpx
from dotenv import load_dotenv

API_BASE = "https://api.anthropic.com/v1"
BETA = "managed-agents-2026-04-01"
CAPTURE_PORT = 8788

# Two distinct sentinel tokens let us tell — from the inbound Bearer alone —
# whether turn 2 presented the credential as it stood at turn 1 (cached) or as
# re-minted between turns (re-read). These resolve A3.
TOKEN_TURN1 = "probe-frozen-token-turn1"
TOKEN_TURN2 = "probe-reminted-token-turn2"

_captured: list[dict[str, object]] = []


def hdrs() -> dict[str, str]:
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ["DAIMON_ANTHROPIC__API_KEY"]
    return {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": BETA,
        "content-type": "application/json",
    }


async def _capture_app(scope, receive, send) -> None:  # type: ignore[no-untyped-def]
    """Minimal ASGI app that logs every inbound request and returns a stub
    MCP-ish JSON-RPC response so MA proceeds far enough to reveal its headers."""
    if scope["type"] != "http":
        return
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    headers = {k.decode(): v.decode() for k, v in scope["headers"]}
    _captured.append(
        {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "method": scope["method"],
            "path": scope["path"],
            "headers": headers,
            "authorization": headers.get("authorization", ""),
            "body": body.decode("utf-8", "replace")[:2000],
        }
    )
    print(f"\n[CAPTURE] {scope['method']} {scope['path']}")
    print("  headers:")
    for k, v in sorted(headers.items()):
        shown = v if k.lower() != "authorization" else v[:48]
        print(f"    {k}: {shown}")
    # Minimal JSON-RPC ack so MA does not immediately error out.
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}, "tools": []}}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _token_in(authorization: str) -> str:
    """Map a captured Authorization header to which sentinel token it carries."""
    if TOKEN_TURN2 in authorization:
        return "TURN2 (re-minted)"
    if TOKEN_TURN1 in authorization:
        return "TURN1 (original)"
    return "UNKNOWN"


async def main() -> None:
    load_dotenv()
    public_url = os.environ.get("CAPTURE_PUBLIC_URL")
    if not public_url:
        print(
            "CAPTURE_PUBLIC_URL not set.\n"
            "MA can only call publicly-reachable URLs. Start a tunnel that\n"
            "forwards to http://127.0.0.1:%d and re-run with CAPTURE_PUBLIC_URL=<tunnel>.\n"
            "Without it this probe CANNOT answer A1/A3 — the result is UNVERIFIED."
            % CAPTURE_PORT
        )
        return

    import uvicorn

    config = uvicorn.Config(_capture_app, host="127.0.0.1", port=CAPTURE_PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    await asyncio.sleep(1.0)

    suffix = uuid.uuid4().hex[:6]
    async with httpx.AsyncClient(timeout=120.0) as http:
        # 1. Create an environment + agent that references the capture URL as a
        #    daimon-mcp-style URL MCP server with an mcp_toolset.
        env_r = await http.post(
            f"{API_BASE}/environments",
            headers=hdrs(),
            json={"display_name": f"probe-env-{suffix}", "type": "cloud"},
        )
        env_r.raise_for_status()
        env_id = env_r.json()["id"]

        agent_r = await http.post(
            f"{API_BASE}/agents",
            headers=hdrs(),
            json={
                "display_name": f"probe-agent-{suffix}",
                "model": {"id": "claude-sonnet-4-5"},
                "system_prompt": "Call the capture MCP tool, then say done.",
                "mcp_servers": [{"name": "cap", "type": "url", "url": public_url}],
                "tools": [{"type": "mcp_toolset", "mcp_server_name": "cap"}],
            },
        )
        print(f"\nagent create: {agent_r.status_code} {agent_r.text[:300]}")
        agent_r.raise_for_status()
        agent_id = agent_r.json()["id"]

        # 2. Create a vault + static_bearer credential at the capture URL,
        #    carrying the TURN1 sentinel token.
        v_r = await http.post(
            f"{API_BASE}/vaults", headers=hdrs(), json={"display_name": f"probe-vault-{suffix}"}
        )
        v_r.raise_for_status()
        vault_id = v_r.json()["id"]
        cred_r = await http.post(
            f"{API_BASE}/vaults/{vault_id}/credentials",
            headers=hdrs(),
            json={
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": public_url,
                    "token": TOKEN_TURN1,
                }
            },
        )
        cred_r.raise_for_status()
        turn1_cred_id = cred_r.json()["id"]

        # 3. Create ONE session and drive TWO turns into it (reuse path).
        s_r = await http.post(
            f"{API_BASE}/sessions",
            headers=hdrs(),
            json={"agent": agent_id, "environment_id": env_id, "vault_ids": [vault_id]},
        )
        s_r.raise_for_status()
        session_id = s_r.json()["id"]

        def _captured_count() -> int:
            return len(_captured)

        # --- Turn 1: drive against the original (TURN1) credential. ---
        print(f"\n=== driving turn 1 into session {session_id} (cred=TURN1) ===")
        before_turn1 = _captured_count()
        await http.post(
            f"{API_BASE}/sessions/{session_id}/events",
            headers=hdrs(),
            json={
                "events": [
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": "turn 1: call cap tool"}],
                    }
                ]
            },
        )
        await asyncio.sleep(20.0)
        turn1_requests = _captured[before_turn1:]

        # --- A3 setup: mid-session, DELETE + RECREATE the static_bearer at the
        #     SAME url with the DISTINCT TURN2 sentinel token. MA blocks PATCH
        #     (405) and duplicate POST (409), so delete+recreate is the only
        #     update path. If MA re-reads the cred per turn, turn 2 presents
        #     TURN2; if MA cached it for the session, turn 2 still presents TURN1.
        print("\n=== mid-session re-mint: delete+recreate static_bearer with TURN2 token ===")
        await http.request(
            "DELETE",
            f"{API_BASE}/vaults/{vault_id}/credentials/{turn1_cred_id}",
            headers=hdrs(),
        )
        recred_r = await http.post(
            f"{API_BASE}/vaults/{vault_id}/credentials",
            headers=hdrs(),
            json={
                "auth": {
                    "type": "static_bearer",
                    "mcp_server_url": public_url,
                    "token": TOKEN_TURN2,
                }
            },
        )
        recred_r.raise_for_status()
        # Small settle window for the re-mint to land server-side.
        await asyncio.sleep(3.0)

        # --- Turn 2: drive into the SAME session after the re-mint. ---
        print(f"\n=== driving turn 2 into session {session_id} (cred RE-MINTED to TURN2) ===")
        before_turn2 = _captured_count()
        await http.post(
            f"{API_BASE}/sessions/{session_id}/events",
            headers=hdrs(),
            json={
                "events": [
                    {
                        "type": "user.message",
                        "content": [{"type": "text", "text": "turn 2: call cap tool"}],
                    }
                ]
            },
        )
        await asyncio.sleep(20.0)
        turn2_requests = _captured[before_turn2:]

        # 4. Cleanup (best-effort).
        for path in (
            f"/sessions/{session_id}",
            f"/vaults/{vault_id}",
            f"/agents/{agent_id}",
            f"/environments/{env_id}",
        ):
            await http.request("DELETE", f"{API_BASE}{path}", headers=hdrs())

    server.should_exit = True
    await server_task

    # --- Verdicts ---
    print("\n\n=== VERDICT INPUT ===")
    print(f"captured {len(_captured)} inbound requests "
          f"(turn1={len(turn1_requests)}, turn2={len(turn2_requests)})")
    print(json.dumps(_captured, indent=2)[:6000])

    # A1: per-turn header presence — diff the captured header KEYS across turns.
    turn1_keys = {k.lower() for r in turn1_requests for k in r["headers"]}  # type: ignore[attr-defined]
    turn2_keys = {k.lower() for r in turn2_requests for k in r["headers"]}  # type: ignore[attr-defined]
    only_turn1 = sorted(turn1_keys - turn2_keys)
    only_turn2 = sorted(turn2_keys - turn1_keys)

    print("\n--- A1 (per-turn header?) ---")
    if only_turn1 or only_turn2:
        print(f"  Headers present in only one turn: only_turn1={only_turn1} only_turn2={only_turn2}")
        print("  -> INSPECT these for caller identity. If any is a caller signal,")
        print("     a narrower option B becomes available (NOT required; A stays committed).")
    else:
        print("  Header KEY sets identical across turns (modulo values).")
        print("  -> No per-turn header channel observed. Option B INFEASIBLE; option A stands.")
    print("  (Also inspect values of non-Authorization headers above for per-turn caller data.)")

    # A3: did turn 2 present the re-minted (TURN2) token or the cached (TURN1) token?
    print("\n--- A3 (does MA re-read the vault cred per turn, or cache per session?) ---")
    turn2_tokens = {_token_in(str(r.get("authorization", ""))) for r in turn2_requests}
    if not turn2_requests:
        print("  No turn-2 requests captured -> MA never reached the server on turn 2")
        print("     (tunnel/agent/cred misconfig). A3 UNVERIFIED — re-run.")
    elif "TURN2 (re-minted)" in turn2_tokens:
        print("  Turn 2 presented the RE-MINTED (TURN2) token.")
        print("  -> VERDICT A3 = RE-READ: MA re-reads the vault credential per turn.")
        print("     88-02 BRANCH: JIT admin re-mint before run_turn on admin reuse turns SUFFICES.")
    elif "TURN1 (original)" in turn2_tokens:
        print("  Turn 2 presented the ORIGINAL (TURN1) token despite the mid-session re-mint.")
        print("  -> VERDICT A3 = CACHED: MA caches the credential for the session lifetime.")
        print("     88-02 BRANCH: JIT re-mint is INERT mid-session — admin reuse turns MUST")
        print("     force a fresh session create so the cold-path admin cred applies.")
    else:
        print(f"  Turn 2 Authorization carried neither sentinel ({turn2_tokens}).")
        print("     A3 UNVERIFIED — inspect the raw capture above and re-run.")

    print(
        "\n=== RECORD BOTH VERDICTS in 88-RESEARCH.md '## Plan-0 Verdict (A1/A3 resolved)' ===\n"
        "  A1: per-turn header present? (yes + field name / no)\n"
        "  A3: re-read (turn-2=TURN2) OR cached (turn-2=TURN1) -> the committed 88-02 branch.\n"
        "  Zero captured requests on either turn -> UNVERIFIED, re-run with a working tunnel."
    )


if __name__ == "__main__":
    asyncio.run(main())
