# Tenant defaults reconciliation concurrency

This model checks whether two concurrent reconciles for one tenant can create
duplicate managed agents and leave the resolver cache pointing at an agent a
later reconcile archives. Run from the repository root with TLC 2.19 available
as described in [`formal/README.md`](../README.md):

```sh
java -XX:+UseParallelGC -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 \
  -config formal/defaults_reconcile/DefaultsReconcileConcurrent.cfg \
  formal/defaults_reconcile/DefaultsReconcile.tla
java -XX:+UseParallelGC -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 \
  -config formal/defaults_reconcile/DefaultsReconcileLocked.cfg \
  formal/defaults_reconcile/DefaultsReconcile.tla
```

## Implementation and test mapping

| Model | Implementation |
| --- | --- |
| `ListEmpty`, `Create` | [`reconcile_agent`](../../packages/core/daimon/core/defaults/reconcile_agents.py#L97) calls [`find_agents_by_daimon_tag`](../../packages/core/daimon/core/defaults/ma_index.py#L57); if no matches exist, it creates the seeded agent. Separate calls can both observe an empty list before either create completes. |
| `owner`, `CanEnter`, `Finish` | [`_reconcile_core`](../../packages/core/daimon/core/defaults/_reconcile.py#L227) acquires a tenant-keyed `pg_advisory_xact_lock` before reconcile passes and holds it through writes and final sweeps. The transaction context releases it on completion, error, or cancellation. |
| `Resolve` | [`resolve_agent` and `_resolve`](../../packages/core/daimon/core/ma_resolver.py#L115) find the canonical live agent by tenant and name and store its ID in the process-local resolver cache. |
| `Deduplicate` | [`reconcile_agent`](../../packages/core/daimon/core/defaults/reconcile_agents.py#L98) archives matches after the canonical one; [`find_agents_by_daimon_tag`](../../packages/core/daimon/core/defaults/ma_index.py#L83) sorts matches newest-first. This is per-resource deduplication. The separate [removed-agent sweep](../../packages/core/daimon/core/defaults/sweep.py#L46) archives resources whose names are absent from the specs and is not the deduplication action modeled here. |
| Resolver-miss caller | [`admission._apply`](../../packages/core/daimon/core/turn/admission.py#L103) invokes `reconcile_tenant_defaults` from the resolver path after its config session has closed. |
| `CachedIdLive` | Resolver-cache safety property: a cached ID must still be among the active resources. |

The real-Postgres regression is
[`test_concurrent_same_tenant_reconciles_serialize_create_and_dedup`](../../packages/core/tests/defaults/test_concurrent_reconcile.py#L61).
It runs two calls through separate engines/connections against one tenant,
asserts only one create and one active agent, then resolves and retrieves the
agent to check that its ID is not archived. The adjacent
[`test_cancelled_reconcile_releases_tenant_lock`](../../packages/core/tests/defaults/test_concurrent_reconcile.py#L147)
checks transaction-scoped lock release after cancellation.

## Bounds and assumptions

- Two reconcile callers (`a`, `b`), one tenant, one seeded agent name, and at
  most two resource IDs. The model explores both callers observing an empty MA
  list before either create, and a later reconcile deduplicating the resulting
  two matches.
- The created IDs have a fixed order: `agent_a` is older and `agent_b` is
  newer. This makes the later canonical choice deterministic and reproduces
  the case where the resolver cached the older ID before deduplication.
- `Locking = FALSE` removes mutual exclusion; `Locking = TRUE` serializes the
  list/create or list-existing reconcile path with one owner. The invariant
  `CachedIdLive` is checked in both configurations. `SingleOwner` is an
  additional invariant in the locked configuration.
- Provider list/create/archive calls are atomic model actions. The model omits
  provider errors, pagination, cancellation, DB failures, lock hash collisions,
  multiple specs, cross-tenant activity, and process-local cache invalidation.
  Cancellation and lock release are covered by the separate Postgres test.
- There is no fairness or liveness claim. TLC checks only reachable-state
  safety in this finite abstraction; it does not prove the Python code.

## Results and pre-fix trace

With `Locking = FALSE`, TLC reports `CachedIdLive` violated (31 distinct
states). The counterexample is:

1. Both callers list an empty agent set.
2. Caller `a` creates `agent_a`; caller `b` creates `agent_b` from its earlier
   empty-list result.
3. Both reconciles finish; the resolver caches `agent_a`.
4. A later reconcile sees both same-name agents, keeps newer `agent_b`, and
   archives `agent_a`; the resolver cache now points to an archived ID.

With `Locking = TRUE`, TLC finds no invariant violation (12 distinct states).
Caller `a` creates and commits `agent_a` before caller `b` lists; `b` sees the
existing resource and does not create a duplicate. `CachedIdLive` remains true.
