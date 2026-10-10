# Test schema teardown and agent face tasks

An agent creation test can queue a face render that opens a second Postgres
connection. If teardown begins `DROP TABLE` while that connection's transaction
is open, their table locks can overlap and the drop may block or deadlock.
`SchemaCleanup.tla` checks the narrow ordering rule: teardown starts only after
cancellation has completed and the face transaction has rolled back.

| Model action | Code boundary |
| --- | --- |
| `StartFace` | `agent_identity.py:202-251` starts `generate()` and its `background_factory.begin()` transaction |
| `BeginCleanup`, `CancelFace` | `db.py:407-420` calls `agent_identity.py:187-201` to request cancellation |
| `FaceRollback` | `agent_identity.py:196` awaits the task, including the transaction context's exit |
| `BeginDrop`, `FinishDrop` | `db.py:301-318`, `db.py:346-353`, and `db.py:427-444` |

| Config | Verdict | Distinct states | Witness |
| --- | --- | ---: | --- |
| `SchemaCleanupUnsafe` | Violates `NoDropDuringFaceTransaction` | 6 | Face transaction starts; schema drop begins before it exits. |
| `SchemaCleanupSafe` | Clean | 9 | Cancellation and rollback complete before schema drop. |

The unsafe config captures the old fixture path, which could begin the drop
while the face task still held a transaction. The safe config checks the new
ordering. This model does not simulate PostgreSQL's lock graph or prove that
every overlap deadlocks; the Postgres tests in `packages/testing/tests/test_db.py`
replay the unsafe overlap as a blocked drop and verify that awaiting task
cancellation releases the lock before the drop. Both use the real schema and
engine fixture code. The CI traceback identifies the same teardown boundary
but has no server query log to identify its competing transaction directly.

The model has one face task and one teardown, with each `await` or transaction
boundary represented separately. A face task may also finish on its own.
`FaceRollback` assumes cancellation reaches the task and SQLAlchemy's
transaction context returns its connection; the Postgres regression checks
that assumption. The model omits other background work and detailed table
dependencies, and its result is bounded to this one-task ordering.
