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
when an SDK wrapped an assertion as a connection error. Failed turn assertions
report sanitized violations without dumping request records or raw bodies/headers.
Recorded headers contain
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
are described below. The original native index retains five Anthropic source pointers and 48
`ANTHROPIC_SOURCE_UNBOUND` annotations. The Anthropic overlay below replaces
those unavailable-input gaps with verified authored tapes or typed unsupported
triggers, without substituting unrelated goldens or claiming live recordings.

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
shape with literal `headers.Authorization`. The scoped Gemini auth guard refuses
these unbound templates with `unbound_mcp_authentication` before SDK HTTP I/O;
their hypothetical HTTP/SSE tapes remain unconsumed. Separate pure-decoder tests
check saved MCP call/result names, arguments, server identity and error outcomes.
Those decoded records are never observations for a scenario PASS. Every Gemini
MCP recipe retains typed `BLOCKED` gap `MCP_AUTH_UNBOUND`: the runner needs an
owned scoped resolver binding, and the authored required-tool policy and NEW17
multiple-server intent remain unsupported. No anonymous substitution is allowed.
Ordinary Gemini request templates carry explicit `tools: []` to match G2 #708;
responses, source triggers and assertions remain unchanged. Deepwiki's authored `ask_question` schema retains
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

After main #812, the authored MCP registry snapshot follows the current tool
schemas and source hashes. The ten MCP envelopes change only their schema
binding hashes; the native index and Anthropic input manifest reflect those
bytes. User turns, tool arguments and replies, native frames, frozen catalog
assertions and Anthropic tapes stay unchanged. Registry inspection uses the
isolated SDK factory with an HTTP transport that refuses every request; ambient
provider settings refuse before client allocation. These refreshed inputs add
no outcome credit.


NEW37 uses the same authored Markdown response in all three backends: outer
pipes, a separator, and four rows under Fruit/Colour/Price. Its Anthropic tape
previously supplied a different internally pipe-delimited Taste table that
evaded the catalog regex. The corrected tape and all five references to its
file hash now agree. The frozen catalog, request and assertions are unchanged.
With Discord table rendering at its default False, every matched response
retains raw pipes and must be reported as the catalog's expected failure;
incomplete adapter evidence remains PENDING. This is an offline input correction
and confers no provider quality or live PASS credit.


### Unscored NEW37 uploaded-table pixel control

`daimon.testing.table_image_oracle.check_table_image(target, upload, png=bytes)`
returns a separate `CheckResult(kind="table_image")`. It never changes the frozen
catalog assertion, generic text completeness, default-disabled expected failure,
or overall cell status. The harness must explicitly select table rendering.

N9 constructs `TableImageTarget` from the independently routed native root and
the surviving answer reconstructed from complete actual posts, then takes an
assertion-time snapshot after NEW37's final `steps[2]` wait. Identity includes the
frozen source SHA, backend/run/session/root, routed channel, exact message, unique
upload URL, native evidence, final-wait completion and snapshot evidence. Native,
upload, wait and snapshot sequences/clocks must retain their causal ordering
through JSON serialization. Every surviving attachment must be accounted for;
this control admits exactly its one table PNG.

N5 supplies actual uploaded bytes through the exact attachment URL, plus the
delivered upload identity, byte length and SHA. N9 joins that receipt to the same
selected snapshot in `UploadedTableImage`. A draft/render buffer, guessed filename,
foreign root/message/channel, pre-wait snapshot, replaced bytes or incomplete
capture cannot grant PASS. Missing owner receipt or runner integration stays
PENDING; a corrupt or substituted PNG is an observed failure.

The pixel proof is narrowly bounded to the current single-line Discord table:
three Fruit/Colour/Price headers, four data rows, printable ASCII cells that fit
the reviewed layout, and SHA-pinned 18px Inter font assets. It independently
parses the narrow native table and projects every cell before comparing actual
decoded RGB pixels; it never invokes the production parser or renderer to create
its reference. PNG compression and metadata may
differ. Native cell values are the input to this projection, so providers are
compared by the verified outcome rather than image bytes or wording. Different
fonts/layouts, escaped/fenced cells, surrounding prose, wrapping, alpha/color
modes, blank/cropped/changed cells, or unknown
extra images stay PENDING. This is a verified renderer domain, not generic OCR,
image readability certification, provider execution or a live scenario PASS.

The N3 unit verifies actual renderer output and adversarial receipts. N5/N9 own
the upload/snapshot capture and executor integration; the hook alone does not
promote text capture through unknown image boundaries.
## Anthropic TARGET-53 authored replay inputs

`anthropic/index.json` now supplies a binding for every frozen TARGET-53 entry.
Its 53 authored turns span 33 scenarios; the other 20 entries retain typed
manual, admin/readback or host-refusal gaps. No entry asserts an outcome.
The original five N9 tapes and all 33 golden recordings remain unchanged.
When the overlay is explicitly selected, it is authoritative for **all 53**
Anthropic cells, including the five previously bound cells. Do not mix the old
five-tape inputs with overlay rows; NEW8, NEW9 and D13 intentionally use the
equivalent provider recipes in the overlay. Without that opt-in, N9's original
five-tape mode retains its existing inputs.

Of the 48 previously unavailable cells, 28 now have authored native turns;
20 have no turns and retain typed exclusions. This is **28/48 drivable inputs,
0 scored**, with effect/admission/step barriers still enforced by the runner.

Load with `load_anthropic_fixtures(DATA / "anthropic/index.json", source_root=REPO)`
from `daimon.testing.anthropic_fixtures`. The loader verifies the frozen digest,
the exact input index and native recipe blobs, every replay blob, original
scenario SHA, expanded user input, turn numbering, root IDs, session reuse,
completion gates, required effect gaps, authoring-script hash and codec source. `pack.select(id)` returns
`(entry, scenario)`; `scenario.model_dump(mode="json")` matches N9's
`ScenarioReplay`, and `entry.gaps` supplies its native gap tuple. Consumers must
join **both** values. In particular, NEW4/NEW15 contain an authored first turn
and a `HOST_GATE` for the second: never dispatch a fabricated second reply.
Manual/readback-only entries have empty turns, preserving their unsupported
trigger rather than guessing a user message from human instructions.

The tapes use Anthropic SDK 0.117.0 Managed Agents shapes:
`user.message`, `session.status_running`, native bash/MCP use/result pairs,
`agent.message`, paired `span.model_request_start`/`span.model_request_end`, and
`session.status_idle` with `stop_reason.type=end_turn`.
Every authored turn has one native usage span. The end references its earlier
start, and its four stages map from the pinned OpenAI recipe: uncached
`input_tokens = input_tokens - cached_tokens`, `cache_read_input_tokens =
cached_tokens`, `cache_creation_input_tokens = 0`, and `output_tokens =
output_tokens`. Output already includes reasoning; Anthropic exposes no
separate reasoning stage, so that detail is not added again. The loader checks
the paired SDK fields, nonnegative counts and exact peer recipe totals. Real
legacy-host tests consume these spans into turn usage totals. Authored usage is
fixture input, not actual provider spend, pricing or ledger settlement proof.
Their processed timestamps and IDs are fixed authored inputs. MCP names,
arguments, server names, result contents and error dispositions match the
source-pinned other-provider recipes. They are **not** authenticated remote
execution or approval evidence. The three artifact recipes supply the same
commands and reply text as the artifact fixture unit; runner-owned file bytes,
downloads, attachment delivery and persistence remain PENDING. NEW37 retains
identical Fruit/Colour/Price markdown with separator and outer pipes, exposing
the known default-disabled table rendering outcome rather than avoiding it.
Native command/result parity is checked wherever that unit's recipes are
present, including the artifact cases when its native pack is composed. Until
then, the five supplementary artifact turns remain copied authoring with an
explicit remote-effect gap; no merge or artifact outcome is inferred.

`anthropic_turn_tape(entry, scenario, turn, codec=pack.index.codec)` scripts an
already admitted session GET, exact input POST and event-named SSE stream.
`anthropic_replay(tape, source_root=DATA / "anthropic", codec_root=REPO)` yields
real `AsyncAnthropic` and the strict shared `WireReplay`. All requests match
`beta=true`, the Managed Agents beta and Anthropic version headers, input body
and native path. SDK retries are zero; unexpected requests are remembered
violations without network fallback. Frames wait for POST acceptance; long
commands additionally wait for the explicit runner clock gate. A gate release
in a decoder test does not prove real latency, queue overlap or remote sleep.
The runner may explicitly override a pre-admitted session path when building
the tape; input roots and payloads remain fixed. `anthropic_scenario_tape`
composes multiple already admitted turns, retaining each follow-up session.
The runner owns admission, effects,
platform receipts, evidence windows and outcome judging.

The owned SDK client uses the shared `build_async_anthropic` factory with explicit
placeholder authentication, no webhook key and empty header/query maps, plus an
HTTP client with environment proxy discovery disabled. Admission refuses the presence of any `ANTHROPIC_*` setting,
including empty settings, by inspecting names only. It never retrieves or clears
operator values. The same constructor guard runs for actual SDK `copy`,
`with_options` and `with_middleware` copies, including settings introduced after
admission and copies of copies. Explicit SDK arguments alone cannot prevent the
pinned SDK from discovering ambient custom headers.

OpenAI and Gemini replays use the same `daimon.testing.isolated_sdk` factories as
the catalog's Anthropic sentinel. Both Gemini HTTP clients receive the scripted
transport and are closed after replay; no default HTTP, TLS or proxy discovery
path supplies a fallback. Ambient settings refuse before SDK or HTTP allocation,
including empty settings. OpenAI and Anthropic SDK copies repeat the shared
admission check. The original recipe inputs, request paths, protocol headers and
provider selection remain pinned; isolation does not supply outcome evidence.

Reauthor offline with `uv run python -m scripts.qa_anthropic_fixtures`.
`--input` selects the pinned native pack and `--output` selects the destination;
two regens must be byte-identical. Existing native blobs and catalog documents
are never written. Negative tests poison every assertion while retaining all
recipe inputs: generated replay bytes remain identical. Fixtures drive the
real legacy turn path, and only captured host evidence enters the oracle.

The golden HTTP capture adapter also uses the shared Anthropic factory, retaining
its original fixture key, native URL, SDK timeout and zero retries. This changes
SDK admission only; recorded request bodies, beta query/header values and the
33 stored golden transcripts remain unchanged.


The authenticated MCP recipe tests and Gemini guard/loopback tests also construct
every SDK client through the shared isolated factories. Gemini closes both its
sync and async paths. Tests refuse ambient settings by name before HTTP allocation
instead of clearing operator variables; hostile settings never expose their values.


### Observing a current wire gate

`await wire.wait_blocked(name)` waits for an actual SSE iterator to reach an
authored `after_gate` boundary. It returns immutable `GateWait` records containing
the gate name, reply ID and zero-based frame index. `wire.held(name)` reads the
current holds without waiting. Opening an HTTP response alone earns no hold;
released, cancelled and closed streams earn none. A JSON-roundtripped record
retains its identity but is a snapshot: compare it to the current holds before
using it, since a stored receipt does not establish that a stream remains paused.
The caller supplies an outer deadline with `asyncio.wait_for`; cancelling an
observer does not release the gate or cancel the stream.

These APIs do not read environment settings. Actual OpenAI, Gemini and Anthropic
SDK-stream controls use the shared isolated factories and the existing
source-pinned long-turn recipe, including cancellation and late synthetic
settings whose values cannot be read. No frames, source pins or goldens are
re-recorded. Gemini's existing wire recipe gates its completed interaction at
frame zero; that pause supplies no running-preview evidence. Anthropic's actual
tool-use prefix is delivered before the tool-result/terminal gate.

A wire pause proves only that native bytes are held. The runner still owns
independent accepted-operation, native-root, current binding, adapter-card and
clock observations before release. Full running-render and routing certification
remain PENDING until those owner joins and their source contract are bound; this
helper does not score a cell, infer elapsed time or certify a remote tool effect.

The TARGET-53 Gemini codec metadata is pinned to reviewed integration commit
`80c43f2f237a65273518f80778d6069d8b835daf` (#784), source digest
`63e8ba4e704d17fa389cd1a5d7eb29b1b6d329953aa05c0efa76c69e698339d6`.
This refresh changes only the Gemini codec commit/source digest and the native
index and Anthropic input-manifest hashes derived from those actual bytes.
Frozen scenario content, assertions, native requests/results, literal spelling,
and Anthropic replay bytes stay unchanged. A source pin refresh provides no
new scenario outcome credit; missing product hooks and artifacts remain PENDING.

Anthropic regeneration verifies the checked-out codec against its reviewed SHA256.
The recorded codec commit remains provenance; the generator requires no historical
local Git object. Changed codec bytes refuse before any replay output is written.

### Main #824 product-host catch-up

For a fresh first product turn, `anthropic_turn_tape(...,
post_turn_usage_catchup=True)` appends the exact post-turn history GET introduced
by main #824 (`0733181847dc775bda444d51c2467016780b8d5d`). It waits on that
turn's send and stream requests, matches the bound session, beta/protocol
headers and `limit=100&order=asc&types[]=span.model_request_end`, and returns the
original authored span objects in a native page with `next_page: null`. The
session override remains explicit. The default is `False`, preserving existing
direct-driver tapes. Turns other than the first refuse this first-pass option;
reused sessions need separately authored cursor/repair queries. Wrong queries,
foreign sessions, premature or repeated reads still record violations; the
shared `WireReplay` matcher and consumption checks are unchanged. N9 opts in
when publishing fresh preparation tapes and tests the real product consumer.
