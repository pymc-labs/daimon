"""Database triggers that keep an erased account's mux state erased, whoever writes.

`stores.mux_erasure.fence_writers` refuses a write for an erased account in application
code. A process still running the code from before the erasure shipped has no such
check, so the same rule is enforced where every writer passes: a trigger on each table
that holds a person's private mux state. A trigger never waits. It tries the account's
*erasure* advisory lock shared, the lock `fence_writers` takes and purge takes exclusive
(`mux-erasure:<id>`, not the account lock the self-hosted resource writers hold for
ordinary writes): if that fails, an erasure of the account is running or queued, and the
write is refused as "being erased". Otherwise it refuses an id found in `erased_account`
(or `erased_principal`). Waiting is what a trigger must not do: it runs after the writer
holds row locks, and a purge that needs those rows while the writer waits for the purge is
a cycle (Postgres cannot see one that spans two of the writer's transactions). A writer
that already took the lock through `fence_writers` in the same transaction holds it, so
the try succeeds. Account and principal ids are random UUIDs that are never reused, so a
returning person starts with ids no tombstone names.

Only a text that is a UUID can name an account; anything else (the operator, a
`selfhosted-billing:<binding id>` operation, its `host:selfhosted-billing` principal)
passes untouched, as does a NULL owner (a shared slot). The statements are plain SQL so
the migration that installs them can carry its own copy; a test keeps the two equal.

Not covered by a trigger: a write into a shared conversation, which names no account
in its row.
"""

from __future__ import annotations

from typing import Final

_UUID_TEXT = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _subject_function(name: str, tombstone_table: str, column: str) -> str:
    return f"""CREATE OR REPLACE FUNCTION {name}(subject text) RETURNS boolean
LANGUAGE plpgsql AS $guard$
DECLARE
    pattern constant text := '{_UUID_TEXT}';
    parsed uuid;
BEGIN
    IF subject IS NULL OR subject !~* pattern THEN
        RETURN false;
    END IF;
    parsed := subject::uuid;
    IF NOT pg_try_advisory_xact_lock_shared(
            hashtextextended('mux-erasure:' || parsed::text, 0)
        ) THEN
        RAISE EXCEPTION 'the account is being erased';
    END IF;
    RETURN EXISTS (SELECT 1 FROM {tombstone_table} WHERE {column} = parsed);
END
$guard$"""


def _trigger_function(name: str, test: str) -> str:
    return f"""CREATE OR REPLACE FUNCTION {name}() RETURNS trigger
LANGUAGE plpgsql AS $guard$
BEGIN
    IF {test} THEN
        RAISE EXCEPTION 'the account was erased';
    END IF;
    RETURN NEW;
END
$guard$"""


FUNCTIONS: Final[tuple[str, ...]] = (
    _subject_function("mux_account_erased", "erased_account", "account_id"),
    _subject_function("mux_principal_erased", "erased_principal", "principal_id"),
    _trigger_function("mux_reject_erased_owner", "mux_account_erased(NEW.account_id)"),
    _trigger_function(
        "mux_reject_erased_operation",
        "mux_account_erased(NEW.account_id) OR mux_principal_erased(NEW.principal_id)",
    ),
    _trigger_function(
        "mux_reject_erased_usage",
        "mux_account_erased(NEW.applied #>> '{observation,session,account_id}')",
    ),
)

#: table -> (trigger function, events). Slots, leases and release requests are owned by
#: `account_id`; an operation also by its principal; a usage observation by the account on
#: the session it observed (the one place a shared conversation names its writer).
GUARDED_TABLES: Final[dict[str, tuple[str, str]]] = {
    "provider_binding_slot": ("mux_reject_erased_owner", "INSERT OR UPDATE"),
    "thread_lease": ("mux_reject_erased_owner", "INSERT OR UPDATE"),
    "thread_provider_release": ("mux_reject_erased_owner", "INSERT OR UPDATE"),
    "operation": ("mux_reject_erased_operation", "INSERT OR UPDATE"),
    "usage_observation": ("mux_reject_erased_usage", "INSERT"),
}


def trigger_name(table: str) -> str:
    return f"{table}_erased_account_guard"


def create_trigger(table: str) -> str:
    function, events = GUARDED_TABLES[table]
    return (
        f"CREATE OR REPLACE TRIGGER {trigger_name(table)}\n"
        f"    BEFORE {events} ON {table}\n"
        f"    FOR EACH ROW EXECUTE FUNCTION {function}()"
    )


def drop_trigger(table: str) -> str:
    return f"DROP TRIGGER IF EXISTS {trigger_name(table)}\n    ON {table}"


INSTALL: Final[tuple[str, ...]] = (
    *FUNCTIONS,
    *(create_trigger(table) for table in GUARDED_TABLES),
)
"""Everything that installs the guards, in dependency order."""

REMOVE: Final[tuple[str, ...]] = (
    *(drop_trigger(table) for table in GUARDED_TABLES),
    "DROP FUNCTION IF EXISTS mux_reject_erased_usage()",
    "DROP FUNCTION IF EXISTS mux_reject_erased_operation()",
    "DROP FUNCTION IF EXISTS mux_reject_erased_owner()",
    "DROP FUNCTION IF EXISTS mux_principal_erased(text)",
    "DROP FUNCTION IF EXISTS mux_account_erased(text)",
)
