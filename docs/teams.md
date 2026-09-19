# Teams Adapter — Trust Model

This page documents how daimon's Teams adapter handles per-user access and
what operators should understand about the resulting trust model.

### Scope: personal chat only

The adapter answers only personal (1:1) `MessageActivity`s — the conversation
must have `conversation_type == "personal"` and no `is_group` flag. Channel,
team, and group-chat messages get a short refusal and never reach a turn.
Message extensions, commands, panels, and proactive outbound delivery are not
part of this adapter.

### Ingress is HTTP, not a dial-out

Discord (gateway) and Slack (Socket Mode) both dial out; Teams does not.
The adapter runs its own FastAPI listener on `DAIMON_TEAMS__PORT` (default
`3978`). The Microsoft `App`/`FastAPIAdapter` owns `POST /api/messages` and
validates the inbound Bot Framework JWT against the configured client id —
unauthenticated or wrong-audience activities are rejected by the SDK before
daimon code runs. The same listener serves `/healthz` and `/readyz`;
`DAIMON_TEAMS__ENABLED=false` makes `/api/messages` answer 503 while health
stays live, and a 64 KiB body limit is enforced before SDK parsing. The
`docker-compose.yml` `teams` service (opt-in `teams` profile) publishes the
port; the Bot Framework messaging endpoint must be able to reach it.

### Identity: one Entra tenant, provisioned by hand

There is no install flow. A Teams deployment is single-tenant: the adapter
fails closed unless the activity's conversation tenant *and* channel-data
tenant both equal `DAIMON_TEAMS__TENANT_ID`, the sender carries a well-formed
`aad_object_id`, and a matching tenant row already exists.

Because there is no callback to create it, the tenant row is provisioned
manually — the same `provision_tenant` helper the Discord adapter calls on
guild join:

```python
result = await provision_tenant(session_factory, platform="teams", workspace_id="<Entra tenant id>")
# New tenants are provision_status="ready" by column default — flip to
# pending so the resolver keeps denying while defaults reconcile (the same
# pending -> reconcile -> ready ordering Discord's guild-join runs).
await set_provision_status(session_factory, tenant_id=result.tenant_id, status="pending")
report = await reconcile_tenant_defaults(
    anthropic, session_factory, settings.defaults_root, tenant_id=result.tenant_id
)
await set_provision_status(
    session_factory, tenant_id=result.tenant_id, status="ready", clear_reason=True
)
```

If the reconcile report shows a failure, leave the tenant non-ready — a
ready row without reconciled defaults turns the first real message into a
user-facing missing-config reply instead of a clean deny.

`tenant_id` is deterministic — `derive_tenant_uuid(platform="teams",
workspace_id=<Entra tenant id>)` — and `tenants.external_id` carries the Entra
tenant id itself. The resolver rejects the activity unless that row exists
with `provision_status="ready"` and is unarchived.

User principals need no manual step: `admit()` get-or-creates the
`platform_principals` row on first contact, with `external_id` set to the
sender's verified `aad_object_id` (their Entra / Azure AD object id). A
first-contact user in a provisioned tenant is authorized like users on the
other adapters.

### Restart behavior

A redeploy freezes the in-flight progress message in place; `drain()` cancels
the turn task, which deliberately keeps its active-turn marker. On next boot
the sweep finds the orphaned row by that marker, edits the frozen message to
an interrupted state, and compare-and-clears the marker. Delivery does not
resume — no queued or retried sends — and the sweep clears only the three
marker columns, leaving the session row's status untouched.

### Files

Session output files are not delivered to Teams in this slice — the
Slack-style `/mnt/session/outputs` upload sweep has no Teams counterpart yet.
