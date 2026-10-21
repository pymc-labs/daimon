Apply `per-conversation.patch` to the in-tree `deploy/helm/daimon-selfhost`
chart on `azure-selfhost` (#1273 is merged):

```sh
git apply deploy/helm/patches/per-conversation.patch
```

This PR carries the opt-in overlay. Set
`harness.mode=perConversation`. The chart owns both `DAIMON_MCP__PUBLIC_URL`
on the host and `HARNESS_DAIMON_MCP_URL` in sandboxes, using
`https://<release>-daimon-mcp.<namespace>.svc:8765/mcp` (configured MCP port).
Conflicting `host.env`, service env or `harness.env` values fail rendering;
matching values are emitted once. PR #1273's `scripts/register.sh` currently
writes short-name overrides: remove those two overrides, or set both to the
namespace-qualified URL before the upgrade. This is a #1273 follow-up; this
overlay does not edit its registration script.

The chart CA signs MCP and controller HTTPS listeners; host clients trust its
combined CA bundle and sandboxes use a scoped public CA projection. The
controller signs IP-SAN sandbox server certificates and presents a separate
client certificate for mTLS bootstrap and C3. No plaintext fallback is supported.

First install with `sandboxSecret` empty and warm pool 0 to create the sandbox
namespace, create the model-only Secret there, then upgrade with its name and
the desired warm pool. Do not copy host credentials into that Secret.
Capacity defaults to **4 conversations**, including any warm slot. At cap 1,
a second user gets busy; at the default cap, a fifth gets busy immediately.
Set `harness.perConversation.idleSeconds` (default 300) to tune idle reaping.
The corrected D2s_v6 budget removes the replaced shared harness (250m/1Gi)
and temporary proof support from the original measurement: 1547m baseline +
25m controller + 4 × 50m sandboxes = 1772m of 1900m allocatable CPU. The
original memory requests leave room for four 256Mi sandboxes as well. This
is a requests budget, not a throughput guarantee; size the cap for your release. Sandboxes recycle
when idle and checkpointed after 24 hours; unknown activity is evicted after
three failed probes only after a fresh durability confirmation. Pending state
persists in a pod annotation; after controller restart, all bound pods stay
protected until a fresh successful checkpoint report. Non-durable pods
are retained and their idle checkpoints retried; readiness exposes their count.
The 48-hour hard lifetime applies after a successful checkpoint, below 7-day
cert validity.
Rotate the sandbox signer Secret (including its controller client certificate)
before its 365-day client expiry and restart the controller and sandboxes.

The chart overlay tests copy the in-tree chart into scratch and apply the
overlay, without requiring Git history. See the harness README for RBAC
and durability guarantees.
