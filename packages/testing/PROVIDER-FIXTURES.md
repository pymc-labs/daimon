# Offline provider fixtures

`daimon.testing.provider_replay` constructs real OpenAI or Gemini SDK transports
over HTTPX MockTransport. The caller supplies a source-pinned `ProviderTape` and
the persisted backend, profile and model. A different selection, changed source,
or different installed SDK version refuses before client construction. There is
no key/environment lookup, network fallback, retry or permissive success router.

OpenAI's G1 contract uses `/agents/sessions/{id}/events` with
`agent.session.turn.*` envelopes and Responses-style `input_text` / `output_text`
content. It does **not** consume `/responses` or `response.*` events. The tape
labels this family `agents-sessions`. Gemini G2 uses Interactions revision
`2026-05-20`; a terminal SSE signal triggers a canonical saved GET. Preview
deltas and stream EOF cannot certify completion. Models are pinned to
`gpt-6-luna` and `gemini-3.8-flash`; versions are OpenAI 2.54.0 and
google-genai 2.7.0. Codec source hashes and full commit IDs live in each fixture.

## Runner handoff

Load `packages/testing/fixtures/target53/index.json` with
`load_provider_fixtures(path, catalog_root=path.parent / "catalog")`, then use
`pack.select(scenario_id, persisted_backend)`. All 53 source documents, assets,
native blobs and the frozen TARGET-53 digest are verified. Setup, context,
steps, assertions, manual checklists and teardown remain available unchanged.
Each scenario retains the original catalog SHA and a separate public projection
SHA. The public copy omits only bibliographic `sources` metadata; execution
fields are verified against the embedded scenario. The loader accepts either
the original source or this pinned projection. Replay tapes use the bundled
projection root.
Native input numbering follows the catalog planner: context consumes a catalog
turn number, so I6's bare mention is turn 2. Placeholders have explicit authored
fixture values; runtime identities must never silently replace them.

`turn_tape(scenario, fixture, turn, key=claimed_key)` supplies an exact send,
stream and saved-read script for an **already owned binding**.
`scenario_tape(..., keys={catalog_turn: claimed_key})` also scripts Gemini's
retained interaction GETs and environment/history reuse. Cold preparation,
recovery, usage and other host requests need exact additional `WireReply` records
from the runner. No unknown request gets an invented success reply. The minimal
OpenAI resource snapshot uses tenant `qa-tenant`, agent `qa-agent`, environment
`qa-environment`; the runner must explicitly bind these or author its own exact
resource replies. Native fixture session IDs are available on each turn.

Pass the tape to `provider_replay(tape, backend=..., profile=..., model=...,
source_root=catalog_root)`. It yields the actual private SDK `transport` and
`wire`. Inject the transport into the real driver/runtime with runner-owned
storage, authorization, leases and accounting. Keep the context open throughout
the turn. After closing consumed streams, call `wire.assert_consumed()`; it
checks HTTP records, SSE frames, stream closure and remembered violations even
when an SDK wrapped an assertion as a connection error. Recorded headers contain
only protocol fields, never authorization/cookies/API keys. Matching is exact
for method, path, query, JSON body, protocol headers and reply dependencies.

Independent stream opens can precede POST acceptance, while their first frames
wait on the acceptance gate. Long-turn scripts also wait on
`turn.N.tool.finished`; the runner releases that gate through its actual clock
and overlap hook. These fixtures never sleep. Serially releasing gates does not
prove queue, cap or progress-card timing. `hold_open` supports recovery streams
that stay live until the host closes them. Neither opening nor closing a stream
counts as consuming undelivered frames.

## Coverage and outcome limits

The first slice has 32 authored turns per provider across 20 scenarios. Every
other host invocation retains a typed `BLOCKED` gap at its original location:
manual triggers, admin/readback-only sources, host refusal/dedupe gates, unbound
native tool schemas, artifact protocol hooks and runner hooks. Thirteen manual
and four readback/admin-only scenarios have no automatic host trigger. Three
artifact-output recipes await their concrete bindings. The ten MCP/tool recipes
are described below. Five existing Anthropic authored tapes have exact source
pointers; the other 48 have `ANTHROPIC_SOURCE_UNBOUND`, without substituting an
unrelated golden or treating a provider fixture as an Anthropic recording.

The runner must retain these gaps in `CellResult` until it actually binds them.
Authored native tool results test SDK decoding and host display, never remote
execution, permissions or workspace state. Likewise, source assets do not prove
that a provider read them. Synthetic token counts test native usage extraction;
they are not a live measurement, price certificate or infrastructure zero.
There is no PASS/verdict field in the fixture pack. The real turn produces
evidence, and the shared outcome oracle judges that captured evidence. Missing
adapter, readback, approval, effect or clock observations stay PENDING. No
cross-provider byte parity or model-quality claim follows from a consumed tape.

## Reauthoring

Run the existing N9 planner against the frozen catalog with an exact integration
SHA, `--run-id provider-fixture`, and an explicit `--output matrix.json`. Then:

```sh
uv run python scripts/qa_provider_fixtures.py --catalog /path/to/catalog --matrix matrix.json
uv run pytest -q -n 2 packages/testing/tests/test_provider_replay.py
```

The generator uses hand-authored recipes derived from user turns, never assertion
patterns. It pins the current local G1/G2 branch commits and code hashes; run it
only with those reviewed sources available. Review changed native blobs and
source pins. It never touches `tests/golden/`, executes tools or calls providers.

## Authenticated MCP/tool recipes

This slice extends the original 64 native turns to 96 (48 per provider across
30 scenarios). All 53 source documents and source invocations remain unchanged;
the 43 unrelated native scenario blobs remain byte-identical. Nine Daimon tool
input/output schemas come from the actual configured registry and carry source
hashes in `catalog/fixtures/mcp-tools.json`. Authoring validates call arguments
against those input schemas and results against MCP's `CallToolResult` shape.
Result text is authored, not a capture or a claim that it satisfies a tool's
structured output schema.

| Scenario | Authored operations | Required runner observations |
| --- | --- | --- |
| D22 | `get_agent`, then `read_channel` | Actual answering identity and channel context |
| D5 | Denied `update_agent` and `attach_mcp_server` | Unchanged agent/server readbacks and refusal |
| D8 | `github_connect` | Private connection card delivered to the correct admin |
| ISO | `read_channel`, `search_messages`, native file commands | Real channel/workspace isolation and independent sessions |
| NEW17 | `attach_mcp_server`, then Deepwiki `ask_question` | BLOCKED: approved OpenAI preparation refuses two bound servers; follow-up/discovery still require future support |
| NEW18 | `add_skill` preview and content-hash follow-up, native skill-file read | Approval, upload/pin, persistence and skill use |
| NEW21 | `request_agent_key` | Private form identity, owned clock advance and expiry edit |
| NEW22 | `create_routine` with channel destination | Scheduled execution, memory effect and untruncated delivery |
| NEW23 | `create_routine` without destination | Schedule and execution persist; no destination delivery |
| NEW35 | `create_routine` with channel destination | Controlled routine failure and reported error |

`fixture.mcp_binding` supplies explicit `MCPConnection` intent, a closed allowed
tool list, source/schema pins and approved auth commit
`9cf23f6c1eaf1d269ed9cab3f281ddd0cc9a6b82`. The OpenAI proof prepares agents and
sessions through the real SDK and driver, resolving scoped credential references
only into session overrides. It covers the nine admitted recipes, including
the denied tool results, and checks secret-free persisted agents and SDK DEBUG logs.
NEW17 retains its two resolver-bound connections and authored source turns,
but carries typed `BLOCKED` gap `MCP_SERVER_LIMIT`: the approved driver must
refuse with `single_bound_mcp_server` during agent preparation. The real SDK
negative controls verify zero resolver calls, agent/session POSTs and tool
dispatch, both with and without a resolver. Its authored follow-up frames
remain available for future support and are not consumed or treated as
execution evidence by the authenticated preparation proof.
Missing resolver, revocation, changed destination and foreign scope refuse
before session or tool dispatch. The offline fixture URLs and resolver values
are fictional; they are not credentials or a public endpoint.

N2's loopback host is pinned separately. Its gate admits only `describe_agent`
and `list_my_sessions`; it does not implement these nine channel-management
tools. Its focused tests prove local authentication, isolation and cleanup.
Broader tool execution and host effects require an owned runner binding; this
slice neither widens that gate nor treats an authored output as a host mutation.

Gemini requests retain the installed Interactions SDK's native `mcp_server`
shape with `headers.Authorization`. Native requests, saved MCP call/result
steps and normalization are replayed through the actual SDK. Its current driver
rejects `credential_ref` or `tool_policy`, so every Gemini MCP recipe retains
`MCP_AUTH_UNBOUND` until the owner provides that resolver seam. No anonymous
substitution is allowed. Deepwiki's authored `ask_question` schema retains
`EXTERNAL_TOOL_SCHEMA_UNBOUND`; discovery and multiple-server preparation are
not proven offline. Every recipe also retains `RUNNER_HOOK` at `mcp` for the
actual approvals, forms, permissions, mutations, scheduling and delivery.

The fixture does not certify a scenario. The proof deliberately feeds only
decoded terminal evidence to the outcome oracle and checks that it cannot PASS
the source assertions without host observations. N9 must preserve the gaps,
run the real turn with owned bindings, capture the observations, and let the
oracle judge the resulting evidence.

Extend this frozen pack without consulting the moving external catalog:

```sh
uv run python -m scripts.qa_provider_mcp_fixtures
uv run pytest -q -n 2 packages/testing/tests/test_provider_mcp_fixtures.py packages/testing/tests/test_provider_replay.py
```

For a reproducibility check use `--index` pointing at this pack and `--output`
pointing at a temporary directory. The generator verifies the approved auth
source against its commit and regenerates only these ten blobs and the index;
all other scenario bytes are copied unchanged. No golden file is edited.
