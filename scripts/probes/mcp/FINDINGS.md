# Phase-2 MCP spec — probe findings (2026-04-23)

Six probes targeted load-bearing assumptions in the Phase-2 MCP server
design.

## Summary of spec edits needed

### FastMCP runtime

- **State API is `get_state` / `set_state`, not `.state["…"]`.** Both are async
  coroutines; `await ctx.set_state("auth", ...)` / `await ctx.get_state("auth")`.
  Spec §4.1 step 6 must update the wording. (probe:
  `scripts/probes/mcp/fastmcp_middleware_state.py`)

- **Auth rejection must hook `on_call_tool`, not `on_request`.** Raising
  `ToolError` in `on_request` fires on `initialize` and breaks the MCP handshake
  before any client can list tools. Spec doesn't say which, but the natural
  reading is "on_request". Pin it to `on_call_tool`. (same probe)

- **Better: do auth at HTTP layer, not FastMCP middleware.** A Starlette
  `BaseHTTPMiddleware` rejecting missing `Authorization` with 401 before MCP
  protocol entry works cleanly under both `httpx.ASGITransport` and real
  uvicorn. This also sidesteps the `on_call_tool` vs `on_request` question
  entirely — unauth'd callers never reach the MCP protocol layer. Recommend
  hybrid: HTTP middleware enforces bearer presence + signature; FastMCP
  middleware loads identity from `request.scope["auth_sub"]` into
  `ctx.set_state`. (probes:
  `scripts/probes/mcp/fastmcp_asgi_transport.py`,
  `scripts/probes/mcp/fastmcp_factory_uvicorn.py`)

- **`ToolError` serializes as `msg` on the wire with no stack trace leak.**
  Bare `RuntimeError` gets wrapped as `"Error calling tool '<n>': <msg>"`.
  Spec §7 "Raise `fastmcp.ToolError(...) from e`" stands; the `from e` chain
  is for server-side logging only. (probe:
  `scripts/probes/mcp/fastmcp_middleware_state.py`)

- **In-process tests require `asgi-lifespan.LifespanManager`.** Raw
  `httpx.ASGITransport` does not run lifespans, so FastMCP's
  `StreamableHTTPSessionManager` task group never initializes. Add
  `asgi-lifespan` to the adapter's dev deps. Spec §8 needs a sentence; test
  factory needs to wrap every app in `LifespanManager` before handing out
  a transport. (probe: `scripts/probes/mcp/fastmcp_asgi_transport.py`)

- **`uvicorn … --factory` works.** `FastMCP.http_app()` returns a
  `StarletteWithLifespan` that uvicorn runs with correct lifespan boot —
  no wrapper needed. Spec §9.1 stands as written. (probe:
  `scripts/probes/mcp/fastmcp_factory_uvicorn.py`)

### MA vault model — **major spec correction**

The spec's assumption "write a `static_bearer` vault credential with
`mcp_server_url`" glossed over the vault/credential distinction. Actual model:

- **Vault = container.** `POST /v1/vaults` requires only `{display_name}`.
  Returns `{id: "vlt_…", type: "vault", display_name, metadata, archived_at, …}`.
  Vaults are `DELETE`-able (returns `{id, type: "vault_deleted"}`), not
  `archive`-able.

- **Credential nested under vault.** `POST /v1/vaults/{vault_id}/credentials`
  with body `{auth: {type, mcp_server_url, token}}`. Returns credential with
  id prefix `vcrd_`. **`token` is never echoed back** — only
  `auth.mcp_server_url` and `auth.type` appear in responses. MA already
  enforces secret-hiding; daimon's "safe-field allowlist" (§5.1 vault.list)
  can be a defense-in-depth confirmation, not the primary guard.

- **Credential `auth.type` is a discriminator, values: `static_bearer` |
  `mcp_oauth`**. No other types accepted. This rules out future daimon
  third-party hookups with OAuth2 client-credentials, API-key, or basic auth
  unless MA adds them. (§10 Q1 option 2 "MA-native hookup" should note this
  constraint.)

- **One credential per `(vault, mcp_server_url)` pair — 409 on duplicate.**
  This is the sharpest operational constraint. If every agent has a
  credential for the same `DAIMON_MCP__PUBLIC_URL`, each agent needs its own
  vault. **Design question the spec did not resolve:** is the per-agent
  daimon-mcp credential stored in a per-agent vault (1:1), or do we invent
  a different URL per agent? Recommend per-agent vault: minimal overhead,
  simplest mental model, matches MA's "vault = bundle of secrets for one
  principal" intent. Spec §4.1 needs one paragraph.

- **Cleanup: `DELETE /v1/vaults/{id}` returns `type: "vault_deleted"`.**
  Spec §6 "revocation = delete the vault credential" should explicitly say
  we delete the whole vault (not just the credential), which maps cleanly
  to "per-agent vault" above. There is no endpoint at
  `DELETE /v1/vaults/{id}/credentials/{id}` probed yet — add a follow-up
  characterization if we decide to keep multi-credential vaults.

(probes:
`scripts/probes/managed_agents/mcp_vault_injection.py`,
`scripts/probes/managed_agents/mcp_vault_shape_sweep.py`,
`scripts/probes/managed_agents/mcp_vault_model.py`,
`scripts/probes/managed_agents/mcp_vault_credential_create.py`)

### MA `sessions.events.create` as send_message

- **Posting `user.message` outside a turn works and MA auto-drives.**
  No need for the caller to open an SSE stream — POST returns
  `{"data": [{type: "user.message", id: "sevt_…", content: [...]}]}` and
  MA emits `session.status_running` → model request → `agent.message` →
  `session.status_idle` on its own. Spec §5.1 `sessions.send_message`
  design is correct. (probe:
  `scripts/probes/managed_agents/mcp_events_direct_post.py`)

- **Replies arrive as a single `agent.message` event, not `text_delta`s.**
  Matters for anyone implementing "wait for reply to show up in GET
  /events" semantics. Not part of Phase 2's `send_message` (which is
  fire-and-forget), but worth noting.

- **Mid-turn concurrent POST behavior: UNKNOWN.** Probe didn't get there
  before the test turn completed. Open follow-up question if we ever ship
  a user-facing flow where two writers can race on one session. Phase 2
  `sessions.send_message` blocks self-send at the app layer; cross-caller
  mid-turn is a theoretical risk only.

### JWT

- **PyJWT 2.x HS256 with `sub` + `iat` only works as expected.** `options={
  "require": ["sub", "iat"]}` enforces presence; wrong secret and tampered
  payload raise `InvalidSignatureError`; `algorithms=["HS256"]` rejects
  `alg: none` tokens with `InvalidAlgorithmError`. (probe:
  `scripts/probes/mcp/jwt_roundtrip.py`)

- **Gotcha: `verify_exp=True` is silently a no-op if the token has no `exp`
  claim.** Do not add `verify_exp` to decode options in an attempt at
  belt-and-braces — it will give a false sense of security. The spec's
  "no exp" position is correct; just don't add it back defensively.

- **PyJWT warns `InsecureKeyLengthWarning` on secrets < 32 bytes.** Factory
  should validate `DAIMON_MCP__JWT_SECRET` is at least 32 bytes at startup.
  Add to §9.1 bootstrap validation list.

## Still unprobed (known gaps)

- **What headers does MA actually send to our MCP server?** Requires a
  publicly-reachable URL (tunnel or staging deploy). Server-side shape is
  verified (`scripts/probes/mcp/fastmcp_factory_uvicorn.py` accepts
  `Authorization: Bearer …`). Confirmation that MA sends exactly that
  header format is deferred to first staging deploy.

- **Mid-turn concurrent `events.create`.** Whether MA queues, rejects,
  or merges concurrent user.message writes is unresolved. Not blocking
  Phase 2 given self-send block + rare cross-caller race.

- **Per-credential `DELETE /v1/vaults/{vault_id}/credentials/{id}`** endpoint
  existence — unprobed. Relevant only if we choose shared-vault multi-
  credential model (not recommended).

- **Vault credential rotation timing.** When a credential's token is
  rotated (delete + re-create, or PUT), does MA re-read the vault per
  turn or cache the token at session-bind time? Partially probeable
  without a receiving server by rotating between turns on the same
  session and observing error shapes (sketch:
  `scripts/probes/mcp/vault_credential_rotation_timing.py`). Cannot
  confirm token-value propagation without a tunneled MCP endpoint.
  Until probed, spec assumes worst case (session-bind caching) and
  documents rotation as "rotate secret → drain sessions → resume."

## Dependency implications

Add to `packages/adapters/mcp/pyproject.toml`:
- `fastmcp>=3.2,<4`
- `pyjwt>=2.8`
- `uvicorn[standard]>=0.30`

Add to test extras:
- `asgi-lifespan>=2.1`
