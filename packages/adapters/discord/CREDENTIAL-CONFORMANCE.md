# Offline rejected-token submit/readback

`daimon.testing.discord_credential_conformance.rejected_token_submit` is an explicit offline hook. No
application imports it. It invokes the existing `McpCredentialModal.on_submit`
with an owned request row, interaction and runtime. It does not modify the
production submit, consume, refusal or credential-write paths.

Construct an owned form through the credential-request store, configure the
runtime's `mcp_token_probe` to a fresh `LoopbackRejectingProbe`, and enter that
probe's async context before calling the hook. One instance belongs to one
submit. The real MCP initialize implementation posts to an owned loopback HTTP
server which replies 403. A fixture-only transport maps exactly one synthetic
public HTTPS origin to that server; production URL validation remains intact.
All other destinations and methods are refused before network I/O. No provider
network requests are made. Supply an isolated SDK client for the owned agent's
read fixtures; do not use the ambient product client builder.

The hook uses the shared `require_owned_sessionmaker` guard before opening any
connection, including validation of every routed bind and the shared isolated-CI
exception. Its only readback query is a fixed parameterized SELECT. It binds the
genuine token, tenant, account, agent, requester, MCP origin and native agent ID;
there is no arbitrary SQL input. Tests skip on a contributor's plain local
`daimon_test`, and run on an owned lane database or registered isolated CI schema.

The closed immutable receipt records `used_at`, `outcome` and the original
`used_at IS NULL` predicate before submit, at the production probe's entry, and
after submit. It requires an actual loopback initialize POST and the actual
parsed HTTP 403 response. Tokens appear only as SHA256 hashes in the receipt.
`unused_assertion` is the observed final predicate, never an invented PASS or
FAIL. The platform interaction/card fixture is not a live Discord wire receipt;
this unit's observation is the real submit/DB/MCP path. N9 owns dispatcher binding
and N3 owns oracle acceptance; this hook alone awards no matrix credit.

## Frozen source binding

Scenario: `QA-NEW27-MISTYPED-TOKEN-HEADLESS`, Anthropic cell only.
Frozen catalog source:
`catalog/scenarios/QA-NEW27-MISTYPED-TOKEN-HEADLESS.yaml` in the QA driver.
SHA256: `5a62a3ded200f694869a1e5c9b436587f7844cc09743272d58d6b9ed72ff72fd`.
Original assertion (expect `true`):

```sql
select used_at is null from credential_requests where id = '{headless.request_id}'
```

The actual schema at `packages/core/daimon/core/_models.py:1732` defines
`CredentialRequest.token` as its primary key and has no `id` column. The lead's
accepted binding interpretation maps `headless.request_id` to the genuine minted
`CredentialRequest.token`, queried as `token = :request_id`. Tenant/account/agent
and requester bindings narrow that same genuine row; the source predicate stays
`used_at IS NULL`. The TARGET-53 catalog/YAML stays unchanged. N3 confirms this
mapping in the oracle pin.

On the unchanged production path, `consume_credential_submit` commits before
`is_token_rejected` calls the probe. `_refuse_for_rejected_token` records
`token_rejected` on that SPENT request. The receipt must report that real outcome,
even when it disagrees with the frozen expected predicate. A consume-removal
mutation must change the observed predicate; a submit-removal mutation must
prevent certification entirely. Any product fix is a separate lead decision.
