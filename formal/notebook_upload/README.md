# Notebook upload capability

Run both configurations from the repository root with the pinned TLC jar
described in [`formal/README.md`](../README.md):

```sh
formal/check.sh
```

To inspect the retained loss trace directly (TLC exits 13 for the expected
temporal-property violation):

```sh
cd formal/notebook_upload
java -cp "$TLA2TOOLS_JAR" tlc2.TLC -workers 1 -config CrashLoss.cfg NotebookUpload.tla
```

The model represents two concurrent request attempts and a possible post-restart
retry, all carrying the same capability `jti`.
`Burn` is one atomic action: the host synchronously reads, checks, and replaces
the consumed-token file before the upload handler's first await. The attempts
can interleave after this action while the first reads its body. A retry sees
the persistent burned bit, even after `Restart`, and is rejected. An accepted
body represents the remaining size check and successful file write as one
abstract transition; an oversize body fails after the token has already been
burned.

| Model item | Implementation |
| --- | --- |
| Verify token and extract `jti` | [`admin.py`](../../apps/notebook-host/src/notebook_host/admin.py:455) |
| Synchronous burn before body read and reject replay | [`admin.py`](../../apps/notebook-host/src/notebook_host/admin.py:462) |
| Body read, size rejection, and accepted upload path | [`admin.py`](../../apps/notebook-host/src/notebook_host/admin.py:470) |
| Read, prune, insert, and persist the consumed `jti` | [`consumed_store.py`](../../apps/notebook-host/src/notebook_host/consumed_store.py:79) |
| Durable replacement of `consumed.json` | [`consumed_store.py`](../../apps/notebook-host/src/notebook_host/consumed_store.py:52) |
| Default path under the host data directory | [`config.py`](../../apps/notebook-host/src/notebook_host/config.py:162) |
| One Uvicorn worker in the checked-in entrypoint | [`__main__.py`](../../apps/notebook-host/src/notebook_host/__main__.py:11) |
| Concurrent same-token requests | [`test_admin_upload.py`](../../apps/notebook-host/tests/test_admin_upload.py:166) |
| Oversize rejection still burns the token | [`test_admin_upload.py`](../../apps/notebook-host/tests/test_admin_upload.py:190) |
| Replay after rebuilding host state | [`test_admin_upload.py`](../../apps/notebook-host/tests/test_admin_upload.py:205) |

`UploadSafe.cfg` checks type correctness, at most one successful upload, and
the result of a crash after burn but before body read. It explores 29 distinct
states. `CrashLoss.cfg` intentionally violates `CrashEventuallyUploads`. Weak
fairness for restart and retry makes TLC show the retry after restart:

1. One concurrent attempt burns the token.
2. The host dies before reading the body; the burn remains durable.
3. The host restarts.
4. The pending concurrent attempt is rejected as a replay.
5. A retry after restart is also rejected; no upload succeeded.
6. The system stutters with no way to reuse the token.

`LateBurnMutation.cfg` moves the check before the body await and delays the
burn until after the body. TLC violates `NoDuplicateSuccessfulUpload`: both
attempts observe an unused token before either writes, then both succeed. This
mutation is a model-sensitivity check, not a claim that this historical
implementation was deployed.

This is the chosen single-use tradeoff: a request can lose its upload if the
host dies after persisting the burn or if body validation rejects it. The
existing oversize regression checks that a smaller retry receives 409. The
crash trace is model evidence for the same ordering, not a separate runtime
crash-injection test.

## Bounds and assumptions

- The checked-in process entrypoint starts Uvicorn without a worker count, so
  it uses one worker. The current deployment runs one notebook-host service
  container. In that topology, the synchronous check-and-replace executes
  without another request task interleaving inside `burn_jti`; the two request
  attempts may interleave at the following body-read await.
- This is not a cross-process atomic check-and-set. Two Uvicorn workers or
  multiple host processes sharing `consumed.json` can both read the jti as
  unused and race their file replacements. That topology is excluded and the
  model makes no universal multi-process one-use claim.
- The durable burn is modeled as atomic. The temp-file write, chmod, and
  `os.replace` failure modes are not explored. A crash before replacement may
  leave the jti unburned; the loss trace begins after replacement.
- Token signature, expiry, slug/name validation, distinct tokens, body
  streaming, partial file writes, and host ceilings are outside the state
  space. Each request attempt is for one shared jti, and a valid body succeeds
  without filesystem failure.
- TLC explores this finite two-attempt abstraction; it does not prove the
  Python implementation, operating-system durability, or deployment topology.
