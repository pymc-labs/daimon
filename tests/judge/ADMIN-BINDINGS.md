# Offline catalog admin bindings

`daimon.testing.admin_bindings` translates frozen catalog admin steps into product service
calls on a disposable lane test database. It neither launches a shell nor reads
operator configuration/keys. Product `provision_tenant` creates the account and
tenant; config revisions, grants, rules, budgets, caps and routine cleanup use
product APIs. Separate DB sessions read the resulting state. Provider selection
is persisted by `configure_channel`, and the common operations run identically
for Anthropic, OpenAI and Gemini. The owned Anthropic SDK uses only
`ScriptedTransport` through `httpx.MockTransport` with an offline fixture key,
explicit credential fields and empty header/query defaults through the shared
isolated SDK factory. Ambient provider settings (including empty values) refuse before SDK/HTTP allocation
and provisioning; actual SDK `copy`/`with_options` also refuse late overrides.
The guard checks names only and never reads, logs or erases operator values.
Database guards require PostgreSQL on localhost/127.0.0.1 with no URL query
overrides and an exact `daimon_test_nc_*` lane name. The sole `daimon_test`
exception requires both `GITHUB_ACTIONS` and `CI` exactly `true`, plus
`build_test_engine` registration for a current-process worker/fresh schema
verified by `isolated_test_schema()`. Local shared/operator databases, plain
engines, forged execution-option metadata and foreign-process schemas refuse
before connection. Mismatched tenant/accounts and foreign destinations refuse
before writes. The runner must supply an explicit manifest of its owned
parent channels; this module does not infer platform channel creation or remap
threads.

N9 owns runner hooks, original operation order, deadlines, receipt capture and
all outcome assertions. N6 owns this module and
`packages/testing/tests/test_qa_admin_bindings.py`. N2 owns the real credential-submit edge.
The pinned fixture retains 19 frozen source scenarios / 38 admin invocations,
source hashes and the frozen TARGET-53 digest. Source snapshots are not changed
to make an unsupported operation pass.

```python
binding = await provision_admin_run(
    sessions, workspace_id=owned_workspace, backend=selected_provider,
    channels=owned_parent_channel_ids, trace=scripted_anthropic_transport,
)
try:
    await binding.configure_channel(channel_id)
    receipt = await binding.execute(original_step, explicit_fixture_values)
finally:
    await binding.close()
```

An `AdminReceipt` is `executed`, `refused` or `pending`, with provider, operation,
observations and optional output fixture values. **Executed is operation evidence,
not a scenario PASS.** Unknown syntax/placeholders and missing edges return typed
pending; foreign scope errors propagate. The runner must stop at unbound setup
or step operations rather than run a turn against substitute state. Expansion
after tokenization prevents fixture data from adding options or shell commands.
Expected-failure handling belongs to the runner and cannot relabel an unrelated
refusal as success.

| Frozen scenario | Common admin binding, all three providers |
|---|---|
| QA-NEW13-WRITERS-NONE-NOT-SILENT | Rules set/restore; actual writers-none admission refusal |
| QA-NEW15-CHANNEL-BUDGET-EXHAUSTED | Budget set/clear with DB readback |
| QA-NEW6-TENANT-CAP-QUEUE | Tenant cap set/restore with product lookup |
| QA-NEW22-ROUTINE-OUTPUT-NOT-TRUNCATED | Tenant/channel-scoped routine teardown |
| QA-NEW23-ROUTINE-WITHOUT-DESTINATION | Tenant/channel-scoped routine teardown |
| QA-NEW35-ROUTINE-FAILURE-IS-REPORTED | Tenant/channel-scoped routine teardown |
| QA-D14-FORK-KEEPS-SKILLS | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-D5-UNBOUND-AGENT-EDIT-REFUSED | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-D7-BIG-WORKSPACE-CHECKPOINT | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-D8-GITHUB-CONNECT-OFFERED | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-ISO-TEAM-CHANNEL-ISOLATION | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-NEW12-AGENT-SWITCH-HANDOFF-COPY | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-NEW17-MCP-USABLE-NEXT-MESSAGE | Grant is bound; copy/cleanup edge is unbound |
| QA-NEW18-SKILL-ADD-YES-PLEASE | Grant is bound; copy/cleanup edge is unbound |
| QA-NEW19-SKILL-DELETE-WHILE-ATTACHED | ADMIN_RESOURCE_EDGE_UNBOUND |
| QA-NEW14-MISSING-THREAD-PERMISSION-COPY | ADMIN_PLATFORM_EDGE_UNBOUND |
| QA-NEW16-TENANT-CREDIT-DEPLETED-COPY | ADMIN_CREDIT_SOURCE_NOT_EXECUTABLE |
| QA-NEW27-MISTYPED-TOKEN-HEADLESS | ADMIN_TOOL_UNBOUND; N2 credential-submit edge |

Six scenarios × three providers have every **admin** invocation bound here,
including teardown. This removes 18 admin-bucket blockers when the runner binds
these APIs; it does not remove other source assertions, native fixtures, platform
observations or admission gaps. Resource operations require the pending neutral
catalog/resource-host dependencies, which this module does not copy or replace.
The optional `resource_edge(AdminCommand) -> AdminReceipt` is explicitly injected;
its absence is always pending. An injected edge must use the real product
resource services, retain scope and provider routing, and provide actual readback.
A callback's presence alone is not evidence or cell credit.

The frozen negative credit command contains `-<balance>`, which is not numeric;
the current CLI accepts only positive credits. This binding does not invent a
negative ledger mutation or silently choose an empty initial balance instead.
Native Gemini resource deletion also lacks a supported product edge; that
teardown cannot be certified as successful native deletion.
The current Gemini preparation edge refuses channel-scoped extra skills, and
the resource-host stack does not provide neutral channel-copy archive. Binding
an agent-scoped upload would not preserve the frozen channel-skill semantics.
These paths need their owning product edges before they can execute; the
optional callback does not waive those gaps.
observations or admission gaps. Resource operations use the optional `resource_edge(AdminCommand) -> AdminReceipt`.
`daimon.testing.admin_resources.ResourceAdminBindings` now supplies real rule/copy
and confirmed Add-skill services. Attach it to `binding.resource_edge`; provide
an actual driver, explicit provider workspace and run operation-key namespace
for OpenAI/Gemini, plus an exact path-to-checked-bundle fixture manifest. Commands
never read arbitrary fixture paths. Each operation derives its key from the
original command and bundle hash; a two-channel isolation setup does not reuse
one mutation key. Fresh authorization also runs inside the callback.

Fork receipts expose the persisted `fork.name`; alternate backends also expose
`fork.id`, `fork.catalog_id` and `fork.catalog_revision`. Add-skill refreshes
those identities after immutable publication, so subsequent preparation can
use the current catalog revision. N9 must merge these returned fixture values
and prepare with the actual binding scope, persisted backend revision and fresh
policy. Default agents/library fixtures must themselves be provisioned through
product APIs, as the tests do; a fixture value alone cannot create an agent.

| Resource source sets | Anthropic | OpenAI/Gemini |
|---|---|---|
| D14, D8, ISO, NEW12, NEW17, NEW18 | Actual copy and product archive | Actual copy; cleanup pending |
| D1, D7 | Actual channel-skill add/remove with readback | ADMIN_NEUTRAL_CHANNEL_SKILLS_UNBOUND |
| NEW19 | Actual fork/Add; delete pending | Actual fork/Add; delete and cleanup pending |
| D5 | ADMIN_AGENT_SPEC_FIXTURE_UNBOUND / ADMIN_AGENT_ARCHIVE_UNBOUND | Same |

This continuation binds eight additional Anthropic **admin sets** (potential
8 bucket removals, total26 with common's18) after runner composition; no full
cell PASS is claimed. All resource sets on OpenAI/Gemini still have a pending
teardown or channel-skill edge. OpenAI cleanup is
`ADMIN_NEUTRAL_COPY_ARCHIVE_UNBOUND`; Gemini is
`ADMIN_GEMINI_AGENT_DELETE_UNSUPPORTED`. Both refuse before catalog/native
mutation. Channel-extra skills are never replaced by an agent upload. NEW19's
missing delete edge is `ADMIN_SKILL_DELETE_PRODUCT_EDGE_UNBOUND`, even though
its source permits one specific expected CLI failure: pending is not that failure.
The unbound agent YAML is not replaced with a fabricated resource.

The frozen negative credit command contains `-<balance>`; current CLI accepts
positive credits only. N2 owns credential submit and D14's CLI assertions;
N9 owns source order, outcomes and platform observations. These callbacks
neither wire the runner nor replace the unchanged actual CLI with a simulated
output. Actual-SDK proofs include source-owned skill reupload on fork and the
retained Gemini skill in an interaction request. Scripted native responses and
Gemini MemoryStorage/StateStore are offline fixtures, not production durability
or live capability certification. Default Anthropic fork requests are compared
as raw bytes with the direct product service. No production edge is changed.

Run the included focused tests on the lane DB. They also cover repeated product
provisioning/configuration, denied cross-tenant/channel operations, token/unknown
platform pending results, placeholder injection refusal, non-lane DB refusal,
and cleanup preserving another tenant and another channel's routines. There
are no live provider, Discord, Slack, Teams, tunnel or model calls.

Every sessionmaker destination is checked before I/O: the main bind and each per-model `binds` entry, including connection-bound engines, must satisfy the same owned-database and isolated-CI rule. An accepted main engine cannot authorize a foreign model route.

Both provisioning and callbacks use the public `daimon.testing.db.require_owned_sessionmaker()` guard. They validate every main/model bind with the shared isolated-CI exception; no admin-local database policy remains. Offline HTTP clients disable ambient proxy and TLS overrides.

Admin SDK clients are built through the shared `mux.testing.isolated_sdk` factories, including their guarded SDK reconstruction and native User-Agent preservation. ORM-specific bind routing tests live in the permitted `packages/testing/tests` tree; the T9 allowlist is unchanged.

Dispatched and direct resource callbacks require the same loopback/database/CI
provenance guard before DB or SDK I/O. CI regressions read the original fixture
database/schema and real fork/attachment catalog state; plain shared-CI engines,
remote hosts and URL overrides refuse before connection or native requests.

Resource fixtures refuse provider SDK environment overrides by name rather than erasing them, and construct explicit offline SDK clients. Direct resource callbacks also reject late Anthropic overrides and foreign per-model routes before I/O.

Resource fixtures use shared OpenAI/Gemini factories, with complete owned sync/async/TLS transports and SDK reconstruction guards. Actual fixture-constructor value traps cover SSL_CERT_FILE, SSL_CERT_DIR and HTTPS_PROXY; late OpenAI settings are rejected by copy, with_options and the product SDKTransport constructor before native I/O. Both admin API test modules now live in packages/testing/tests.

## Policy, budget and credit mutation slice (C4)

Only these literal shapes execute for this slice; tokens are parsed in process:

| CLI shape | Receipt operation and actual readback |
|---|---|
| `daimon channels rule set discord WORKSPACE CHANNEL --writers none` (restore with `any`) | `channel_rule_set`; real access policy and writers denial decision |
| `daimon channels budget set discord WORKSPACE CHANNEL USD --window total` | `channel_budget_set`; committed full budget equals the service write result |
| `daimon channels budget clear discord WORKSPACE CHANNEL` | `channel_budget_clear`; independent lookup must return no budget |
| `daimon tenants credit discord WORKSPACE USD --note NOTE [--id ID]` | `tenant_credit`; finite positive USD with at most two decimal places, actual ledger row and balance |
| `daimon tenants turn-cap discord WORKSPACE POSITIVE_INTEGER_OR_DEFAULT` | `tenant_turn-cap`; actual tenant override and effective product cap |

All destinations must belong to the explicitly scoped run. These operations
make zero native provider requests on every backend. Budget and credit readback
failures cannot issue an executed receipt. Invalid credit amounts or notes
remain `ADMIN_CREDIT_SOURCE_NOT_EXECUTABLE` before ledger mutation.

| Frozen scenario | Anthropic | OpenAI | Gemini | Remaining evidence |
|---|---|---|---|---|
| NEW13-WRITERS-NONE | PENDING | PENDING | PENDING | Admin set/restore ready; N4 actual host denial/log/visible signal |
| NEW14-MISSING-THREAD-PERMISSION | PENDING | PENDING | PENDING | `ADMIN_PLATFORM_EDGE_UNBOUND`; N7 actual Forbidden gateway and parent notice |
| NEW15-CHANNEL-BUDGET | PENDING | PENDING | PENDING | Admin set/clear ready; N4 denial plus C5 actual metered turn and independent debit join |
| NEW16-TENANT-CREDIT | PENDING | PENDING | PENDING | `ADMIN_CREDIT_SOURCE_NOT_EXECUTABLE` for frozen negative placeholder, then N4 credit refusal/copy |

Six of twelve admin prerequisites are bound; none of the twelve full cells is
certified by this mutation slice. Host denial and Discord permission hooks are
separate owner interfaces and cannot manufacture a provider turn identity.
