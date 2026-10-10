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
artifact-output recipes and ten authenticated MCP/tool scenarios await their
concrete bindings. Five existing Anthropic authored tapes have exact source
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
