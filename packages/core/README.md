# Daimon core

Core owns schema, stores and the turn pipeline. It does not import platform
adapters. See [CONTRIBUTING.md](../../CONTRIBUTING.md) for development and gates.

## Self-hosted tool preparation

`daimon.core.turn.selfhosted_tools` prepares the C3 harness fields without any
Anthropic calls. This is a host integration building block; selecting the
profile alone does not enable a runtime.

The authorized host supplies `build_turn_tools` with the agent's MCPConnection
records, agent SkillRefs (including defaults), already selected platform refs,
channel skill rows and a map of `(skill_id, version)` to validated SkillBundles.
It also supplies the admitted tenant, platform, channel and agent names. Load
channel rows using `stores.channel_skills.list_channel_skills` for that exact
scope. Agent-owned channel skills only apply to their owning agent. Earlier
channel additions win a mount clash; an agent/platform skill wins over a channel
addition. Conflicting agent pins or mount names fail preparation.

Use `await load_turn_skill_bundles(skills_root, refs, scope=scope, catalog=catalog)`
to load unpinned local defaults off the event loop and exact retained versions
from an optional authorized neutral catalog. Existing seed names (including
underscores) remain valid. The synchronous `load_seeded_turn_skill_bundles` is
also available. There is no provider lookup or fallback from a missing pin to
latest. Bundle provenance, including the catalog's provider workspace, belongs
to admission. Platform selection also belongs to admission; the builder does
not infer defaults from an agent's display name. Call the pure builder through
`asyncio.to_thread` from async turn preparation.

Inline skills preserve UTF-8 text in `files` and other bytes as base64 in
`binary_files`, with the shared wire `skill_sha256` over every decoded file.
Skills and paths are sorted. Decoded bytes are capped at 8 MiB per skill and
24 MiB per turn, with bounded decompression; exceeding either refuses the turn.
Provider built-ins and unavailable channel pins are skipped. Platform/channel
additions use the shared skill selection and count-cap helpers to fit the
remaining room. `TurnTools.skipped` reports names and reasons for the host's
once-per-thread notice. Missing custom agent bundles still refuse preparation.

The returned `TurnTools` contains credential-free MCPServer and InlineSkill tuples. Convert
them to lists when filling TurnRequest. The reserved `daimon-mcp` entry uses the
operator-supplied public MCP URL and `headers_env.Authorization` set to
`DAIMON_MCP_AUTHORIZATION`. Other servers use streamable HTTP; `credential_ref`
must name a `HARNESS_MCP_CRED_` environment variable holding the complete
Authorization value (including `Bearer ` when needed). Inject
`settings.selfhosted.mcp_credential_bindings` as `mcp_credential_bindings`.
The operator sets `DAIMON_SELFHOSTED__MCP_CREDENTIAL_BINDINGS` to a JSON list
of `{name, origin, tenants}` records. Each credential requires its exact HTTPS
origin (including any nondefault port) and the admitted tenant UUID; unbound
names are refused. `tenants: ["*"]` declares a single-tenant installation only.
Configure harness `HARNESS_MCP_CRED_BINDINGS` from the same operator source.
The reserved Daimon connection permits loopback HTTP; operator-bound credentials
require HTTPS. URLs are
canonicalized; another server cannot alias Daimon's URL. Secret URLs, reserved
names and unsupported per-server tool policies are refused rather than discarded.
The turn runtime supplies the separate C3 tool permission policy.

For each admitted turn, the async host calls `mint_turn_mcp_authorization` with
its authorized Scope, selfhosted agent ResourceRef, injected Agents port and turn
`deadline_s`. This retrieves the live tenant-owned agent, verifies the caller's
tenant, derives the agent UUID with the existing MCP identity helper, and
registers a chat token expiring after the deadline plus 60 seconds. Preparation
refuses a deadline whose grace would exceed the one-hour chat token lifetime
(maximum deadline: 3540 seconds).
It returns `MintedMcpAuthorization(header, jti, expires_at)` with a redacted,
repr-excluded SecretStr header. The host must revoke `jti` in its turn-finally
path. Pass `header.get_secret_value()` only to the transport
as `X-Daimon-MCP-Authorization`. This header is separate from the harness's own
Authorization header. Never put it in TurnRequest, session native refs, events,
logs or journals. The harness resolves the reserved env name in a per-turn
in-memory mapping and checks its configured Daimon MCP URL; it must not mutate
process env. Direct and Foundry transports must both carry the header; a gateway
that strips it cannot run authenticated Daimon MCP calls through this protocol.
