# Agent backend staging QA

H6 preparation only. Base: `048be92dabfe28cd5ff3db1aac88274ff543d6f2`.
Three possible points, one full staging cell per target backend; **zero earned**.
This packet grants no staging access, deployment, key read, mutation or paid call.
[The offline plan](fixtures/section_h/staging-plan.json) is an inventory, not an
execution entry point. All execution/financial bindings start unbound.

## Scope and owner bindings

Use the same admin surfaces as skill/MCP changes. Read the real setting,
revision, tenant allowlist and audit through N6's public service; use N4's real
admission/preparation and retained thread binding. No direct database writes,
MemoryStateStore turn substitutes, invented delivery or provider labels.

| Owner | Binding needed before execution | Evidence to pin |
| --- | --- | --- |
| N6 | Read setting/previous revision; compatibility preview; set/clear/revert; tenant allowlist; audit readback | Public module/signatures, interface NOTE, merged source SHA, transaction/CAS semantics and audit fields |
| N7 | Chat request, preview and admin-only Confirm through the existing ConfirmationHook | Tool registration, card renderer, current-admin recheck, revision/digest/expiry binding |
| N5 | /agent-setup details select/preview/Confirm; direct CLI and MCP set/clear | Public routes/commands/tool names, validation/audit parity and merged source SHA |
| N4 | Channel > agent > Anthropic resolution, native thread retention and backend footer | Admission/preparation/readback API and actual adapter capture |
| N8 | List-price preview and conservative whole-run quote/settlement | Dated reviewed prices, provider capacity/request bounds, runtime meter and signed ledger/config/envelope |

N6's 20261012T084539Z declaration and 20261012T085041Z reshape NOTE supply
these contracts. Import the final service/types from `daimon.core.channel_backend`;
this reuses existing `check_backend` rather than a parallel service. The v1
module/new policy/audit tables are superseded by D1–D6: use
`TenantAccessPolicy.enabled_backends` (default Anthropic only). Pin the final
merged source before execution; do not import an assumed unmerged module or
implement a parallel policy table. Apply/revert use the public service as-is.
Absence is `PENDING/OWNER_INTERFACE_UNBOUND`, never a successful operation.

- `BackendAgent` identifies the real tenant/scoped agent, configuration digest,
  attachments/capability requirements and shared-thread mode. A trusted fresh
  loader supplies it; tool input must never supply ownership or the digest.
- `BackendActor` joins tenant/account/current trusted admin subject. The service
  rechecks membership, tenant and server/workspace admin; a channel-only grant
  is insufficient.
- `CompatibilityPreview` carries setting and policy revisions, unsupported
  skills/MCPs/capabilities, shared-thread limits, cost line, eligibility and a
  digest over all facts and the actual agent configuration. `allowed` means
  tenant eligibility, not capability completeness.
- `service.current(agent)`, `tenant_policy(tenant_id)`, `preview(agent, target)`
  and `revision(agent, local)` / `audit(tenant_id, agent_id=...)` supply
  independent readback. `load_backend_agent(anthropic, sessionmaker,
  tenant_id=..., agent_name=..., resource_host=...)` is the shared trusted loader.
- `set_backend(agent, target, expected_revision, actor,
  expected_preview_digest=...)`, `clear_backend(agent, expected_revision,
  actor, expected_preview_digest=...)` and `revert(agent, actor,
  expected_revision=..., expected_preview_digest=...)` bind exact current
  local revision (0 when absent) and reread policy/preview before mutation.
  Revert **appends** a revision restoring the previous value; it never rewinds
  or deletes history. Mutation and audit commit in one owned transaction.
- Declared audit facts: id, tenant, agent, actor account, operation
  set/clear/revert/allowlist, previous/new revisions and backend values,
  preview digest and creation time are joined from setting revision/readback.
  The final audit returns `SecurityAuditRow`: `tool_name=agent_backend`, operation,
  fixed `reason=completed`, actor/tenant/agent ids, and
  `scope=agent_backend:<agent_uuid>:<local>`. Do not assume a new detailed audit
  table. PanelOp is a separate surface trail; service audit failure rolls back
  the setting mutation.
- `TenantBackendPolicy.revision` is the existing allowlist policy's **string
  digest**, not the initial v1 integer. The card/preview binds that digest.
  Effective agent selections materialize as non-override channel revisions with
  origin metadata; current channel override reads filter by origin. Retained
  revision 0 stays the default. Require N6/N4 readback evidence for these joins.

The settings surface is the existing **/agent-setup details panel**, not a web
panel. N5's direct MCP tool is `set_agent_backend` (absent backend clears);
N7 alone owns `request_agent_backend_switch`, using the existing
`ConfirmationHook` with opt-in admin enforcement and click-time admin recheck.
H1–H5 require their own dual-sign and CI; this preparation certifies none of them.

## Arrangement

A separate GO names one dedicated QA tenant, one supported chat platform, the
exact staging application and deployment SHA, and the allowed admin/non-admin
identities. All agents, channels and threads are disposable and tagged with the
attempt id. Keep a manifest of resources this run created. Borrowed resources
are read-only; never clean up tenant-wide or workspace-wide.

Enable only the existing `DAIMON_TURN__PATH=mux` and
`DAIMON_TURN__CHANNEL_BACKENDS=true` path in the isolated QA arrangement. Verify
unconfigured agents/channels and flag-off requests remain Anthropic with the
existing dual-path golden evidence. Do not reconfigure unrelated channels or
change deployment-wide defaults. A platform without all owner bindings remains
pending; one platform run is not proof for Discord, Slack and Teams together.

Each scored cell starts with a different predecessor provider and **no channel
backend override**, so an agent-setting switch genuinely changes new threads.
Use a QA-owned agent with a source-pinned skills/MCP/capability inventory and
record the preview against actual driver metadata. The GO must specify the
approved usable subset and any explicit compatibility refusals. Do not silently
drop unsupported skills, mounts or MCPs to make a turn pass. H6 does not include
F2 MCP/tool-execution certification or an authorization to publish an endpoint.

The selected native model must match both configuration and actual session/turn
readback. A model alias, fallback, fake revision or injected provider label is
not evidence. No fallback is authorized, including Gemini after a 503.

## Three scored cells

| Cell | Previous backend/model | Switch target/model | Planned host turns | Native sessions |
| --- | --- | --- | --- | --- |
| H6-ANTHROPIC | Gemini / gemini-3.8-flash | Anthropic / claude-haiku-5-5 | 3 predecessor + 2 target | 2 predecessor + 1 target |
| H6-OPENAI | Anthropic / claude-haiku-5-5 | OpenAI / gpt-6-luna | 3 predecessor + 2 target | 2 predecessor + 1 target |
| H6-GEMINI | OpenAI / gpt-6-luna | Gemini / gemini-3.8-flash | 3 predecessor + 2 target | 2 predecessor + 1 target |

For each cell, serially:

1. Provision the owned arrangement and save the current agent setting, revision,
   previous-setting identity, allowlist and relevant channel configuration.
   Set the predecessor through N6's service during setup. Record its audit row;
   setup is not a scored switch. Complete a nonce-tagged baseline turn in T0
   and read back its native provider/model/session and actual visible footer.
2. Request `@Daimon switch <agent> to <backend>` through N7's real chat tool.
   Capture the compatibility preview/card: unsupported skills/MCPs/capabilities,
   shared-thread limits, dated cost line and allowed target. Request alone must
   not mutate the setting. Confirm as the authorized admin; record the current
   admin check, preview digest, expected revision, CAS result and audit readback.
3. Read back the new setting/revision independently. In fresh T1, submit the
   fixed nonce prompt `Reply exactly H6-<attempt>-target`. Wait for an actual
   durable completed outcome. Join the accepted input, root, persisted binding,
   native provider/model/session, usage and actual platform reply/footer.
4. Reply in existing T0. It must still use the predecessor session/backend;
   configuration changes must not adopt or migrate it. Wait for real completion
   and capture its footer. A missing original driver must refuse before foreign
   provider effects; that negative control does not replace this positive turn.
5. Revert through the real one-step owner service exposed by /agent-setup.
   Restore the exact predecessor setting value (including profile/model) with
   the owner's revision semantics, not a guessed revision id or an ad hoc set.
   Capture the audit edge to the prior value and independent revision readback.
6. In fresh T2, a real completed turn must use the predecessor. Reply in T1:
   that existing thread must retain the target after revert. Capture both native
   identity joins and actual reply footers. Configuration or footer text alone
   cannot prove a routed turn.
7. Read complete usage/runtime before deleting native resources. Settle the
   signed run once, then delete only manifest-owned resources and revoke any
   temporary credentials granted by the GO. Persist cleanup acknowledgements,
   failures and final setting/audit readback. Stop before another cell if usage,
   runtime, bounds or cleanup safety is uncertain.

A reply supplies a unique nonce rather than a subjective prose grade. Use the
real adapter post path and capture its delivered content, truncation, cards and
footer; a headless return or screenshot without native/host joins is incomplete.
Wait for authoritative terminal occupancy before each follow-up. Do not reuse
C03's consumed GO or perform a resend after an ambiguous acceptance.

## Safety and parity controls

These properties need owner offline evidence at the exact composed source SHA.
Repeat a control on staging only if the separate GO explicitly includes it and
its effects/cost. Refused mutations must leave setting/revision/audit state
unchanged unless the owner documents a distinct refused-attempt audit, and
must cause zero provider writes or inference.

| Property | Required check |
| --- | --- |
| Flag-off/default parity | No new control/tool/card registration; unchanged unconfigured Anthropic golden behavior |
| Resolver precedence | Channel revision wins over agent; absent channel revision uses agent; cleared/absent agent inherits Anthropic. An explicit Anthropic channel revision (including existing channel clear) still overrides a native agent setting |
| Tenant allowlist | Disabled backend is neither offered nor accepted through chat/settings/CLI/MCP |
| Admin authorization | Non-admin requester/click and admin revoked after preview refuse at application time |
| Scope | Foreign tenant/agent/card/channel cannot mutate or dispatch |
| Stale confirmation | Changed setting/policy revision, changed fresh agent configuration/attachments, mismatched preview digest and expired card refuse; no automatic retry |
| Concurrent confirmation | At most one successful mutation/audit transition; stale contender refuses |
| Service parity | Chat/settings/direct CLI/MCP use identical validation and audit; set/clear/revert preserve revision history |
| Thread retention/footer | Existing native binding survives switch/revert; new thread uses resolved setting; actual footer matches native provider |
| Manifest bounds (D3) | A new grant-enabled admin revision/digest within the authorized tenant/backend/account/field bounds is admitted; foreign identity or out-of-bounds model/profile/tool/mount fields refuse before provider I/O. Legacy manifests without a grant still require the exact config digest; existing CERT receipt validation remains intact |
| Accounting admission | Unbound H6 identity, stale prices, insufficient room or missing signed GO refuse before credential/provider access |

CLI/MCP parity is a readback/offline control unless its separate write is listed
in the GO. Do not perform additional set/clear actions to claim coverage without
counting their audit effects, turns and spend.

## Spend bounds and settlement

N8's declared pure API is `daimon.core.pricing.list_price_preview(model, price=...)`.
Bind its merged source and actual selected model; unknown/mismatched pricing
returns an explicit unknown line. N8 owns the preview cost line: Anthropic from `daimon.core.pricing`,
OpenAI/Gemini from their documented/operator-reviewed rates (Gemini
`packages/mux/mux/drivers/gemini/PRICING.md`). Label it **list price**.
The general persistent-workspace profile has no proven finite per-turn maximum:
its preview must say unknown for that bound. A preview is not a reservation or billed total. The admission/settlement quote
separately uses **existing reviewed dated tables only**;
there is no new price review or financial approval in this packet. Before GO,
pin the table/config hash and ensure the request timestamp fits its effective
interval. Never advance a test clock to make a live price valid.

The inspected canonical `live-budget.host.json` currently has these conservative
long-prompt rates per million tokens. They are source observations, **not a
current executable quote**:

| Model | Max input category USD/M | Output USD/M | Infrastructure term |
| --- | --- | --- | --- |
| claude-haiku-5-5 | 1.00 (cache write; uncached 0.50) | 2.50 | agent_session: 0.08 USD/hour, proportional |
| gpt-6-luna | 0.25 (cache write; uncached 0.20) | 0.75 | hosted_container: 1 GB 0.03 USD per started 1200-second quantum, if applicable |
| gemini-3.8-flash | 0.75 | 3.75 | Must verify the selected native execution has no additional billable infrastructure; absence of a table is not proof of zero |

Canonical price sources are the official Anthropic, OpenAI and Gemini URLs
stored with those rows; N8's cost-line API must retain their effective dates.
Anthropic model/session rows inspected here end on 2026-10-12; N8 must resolve
current-date coverage before execution. OpenAI's 1 GB rate requires proven
memory/runtime selection; unknown/mismatched container size refuses the quote.
Use measured short/long tiers and disjoint cache categories for actual settlement,
rather than applying the conservative maximum category to billed actual.

For provider p, the admission bound is:

`B_p = sum_j((Imax_j * max_valid_input_rate_p + Omax_j * output_rate_p) / 1e6) + runtime_bound_p + other_reviewed_charges_p`

Each j is one billable model request, including bootstrap, admin-chat/tool loops,
automatic titles and any paid setup/cleanup. Imax/Omax must be audited native
limits or another reviewed conservative provider bound. A requested small answer,
20k/2k accounting defaults, session deadline or host request count alone is not a
provider-enforced token/request ceiling. Do not assume one model request per
host turn; the N8 quote must prove/cover the complete request inventory.

The positive inventory is 15 host turns and 9 native sessions: 5 host turns and
3 native sessions per provider across all cells, **plus any extra model work**.
Proposed per-cell total deadline is 900 seconds and one shared 90-second cleanup
budget, including setup, confirmation and post-run reads. For Anthropic, sum all
created session lifetimes through verified cleanup (unknown duration keeps its
hold). For OpenAI, apply the published minimum/rounded container quantum for
each actual container, or require independent proof of no billable container.
The timer is not a substitute for a provider-side monetary cap.

N8 supplies `maximum_model_requests`, native Imax/Omax, runtime/memory selection,
per-provider `B_p`, the single-request margin and the total signed maximum. Those
numbers are **UNBOUND**, so numeric whole-run bounds and holds remain unknown.
No reservation may proceed until the sum fits fresh remaining signed line room,
with existing holds and latches preserved. Run serially; if exact actual is
missing, retain conservative held spend and stop at the next admission gate.

The base `ProbePlan.fixture_id` permits C01–C18/F1/A7 and rejects H6. A reviewed
H6 fixture identity/admission extension is required in the eventual execution
harness; do not relabel this work as C03 or F1. This packet contains no live
runner and performs no guard reservation, ledger initialization or approval edit.

Each receipt/report shows `actual_usd`, `held_usd` and
`accounting_status=actual|estimated_unverified|pending`. Unknown is null, never
zero. Settle complete, freshness-checked disjoint provider usage with the dated
price table plus infrastructure. Anthropic uses `observation_from_event` and
requires actual agent-session duration; an empty duration tuple is unknown.
OpenAI's floored `spend_control.consumed` is only a cross-check/lower bound.
Retain proof/overrun floors independently of actual and keep the admission latch;
never release a hold from partial or stale counters. Post receipts to N8.

## Evidence and scoring

| Receipt | Minimum proof |
| --- | --- |
| GO/finance | Exact source/deployment, recipe/runtime/model/price/config/ledger hashes; signed envelopes; attempt id, deadlines and authority |
| Arrangement | Tenant/actor/channel/agent/thread scope; created-resource tags; before settings/allowlist/revision; credentials recorded only as references/digests |
| Preview/confirm | Real tool/card identity, all compatibility facts/cost/shared limits, digest/expiry/revision, actor authorization at click, service CAS acknowledgement |
| Setting/audit | Independent after/revert readback, source revision lineage, set/revert actor/tenant/agent/action/time and owner audit ids |
| Each turn | Accepted input/root and durable terminal; prepared identity + persisted binding + native provider/profile/model/session agree; source revision and existing/new-thread facts |
| Visible reply | Actual adapter message ids/content/footer joined to the same native root/session; capture complete |
| Usage/settlement | Request ids/timestamps, disjoint token/cache usage, runtime/container measurements and rates; separate actual/held/status; all setup/cleanup costs |
| Cleanup | Manifest-only deletion/revocation acknowledgements or retained failures; no borrowed-resource deletion; final settings/audit readback |
| Final score | PASS/FAIL/PENDING reason, all receipt hashes, per-backend actual/held, gaps and bugs; lead-signed complete evidence manifest |

Award **one PASS/one H6 point** only when that target's entire real staging
switch/turn/revert plus required retention/receipt chain is complete and signed.
No averaging or partial point. A demonstrated violated assertion is FAIL, even
if the switch itself worked; an unbound surface, absent receipt or unattempted
cell is PENDING (reported NOT RUNNABLE with reason). Unknown accounting is never
reported as zero and leaves final receipt sign-off pending. A failed/ambiguous
attempt consumes its pin; fix through review and request a fresh GO for any rerun.

Initial score: Anthropic PENDING, OpenAI PENDING, Gemini PENDING; no run ids,
actual or held amounts. This preparation leaves the C01–C18 board unchanged.

## What a separate staging GO must authorize

The lead must sign the exact execution packet after H1–H5 and the execution
harness are dual-signed, merged and deployed through a separately authorized
route. This unit does not request or perform that deployment.

- The precise staging service/URL, immutable deployed/source/runtime hashes,
  QA tenant, platform, actors, agent/channel/thread namespace and flag scope.
- Three named backend cells and their source-pinned owner interfaces, model/profile
  readbacks, predecessor setup, five host turns per cell, confirmation/revert and
  explicitly allowed negative or parity controls. No automatic fallback or retry.
- Read/write scope for each resource and setting/audit transition; fresh temporary
  credential references/expiry/revocation; allowed connector/tool subset and
  native cleanup. No production tenant, workspace-wide sweep or borrowed deletion.
- Current dated price/config/ledger and approved envelope digests; each provider's
  remaining cap/stop/latch/hold baseline, total request/native capacity/runtime
  bounds and worst-case reserved spend; H6 admission identity and key-access gate.
- One attempt per cell, serial order, whole-cell/cleanup deadlines, terminal and
  ambiguous-acceptance behavior, usage collection/settlement, hard stop and
  retained-evidence/receipt locations. No inference after a stop to repair a grade.
- Actual platform receipt collection, evidence scoring and lead sign-off; no reuse
  of C03/F2 or earlier staging permissions as authorization for this packet.

Missing any binding is a refused/unexecuted cell, not implied consent. PREP may be
reviewed and merged now; scored staging work waits for this separate signed GO.
