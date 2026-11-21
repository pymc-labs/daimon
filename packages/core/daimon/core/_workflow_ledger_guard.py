"""The trigger that keeps the run ledger append-only, whoever writes.

Updates are refused. A delete passes only inside the cascade of a run, workflow or tenant
delete (its run or tenant is already gone; retention and erasure delete runs, never
events), or under `daimon.workflow_ledger_maintenance` (the test wipe). Plain SQL, so
migration 0222 carries its own copy; a test keeps the two equal.
"""

from __future__ import annotations

from typing import Final

INSTALL: Final = (
    """CREATE OR REPLACE FUNCTION workflow_run_events_append_only() RETURNS trigger
LANGUAGE plpgsql AS $guard$
DECLARE
    gone boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF current_setting('daimon.workflow_ledger_maintenance', true) = 'on' THEN
            RETURN OLD;
        END IF;
        EXECUTE 'SELECT NOT EXISTS (SELECT 1 FROM ' || quote_ident(TG_TABLE_SCHEMA)
            || '.workflow_runs WHERE id = $1) OR NOT EXISTS (SELECT 1 FROM '
            || quote_ident(TG_TABLE_SCHEMA) || '.tenants WHERE id = $2)'
            INTO STRICT gone USING OLD.run_id, OLD.tenant_id;
        IF gone THEN
            RETURN OLD;
        END IF;
    END IF;
    RAISE EXCEPTION 'workflow run events are append-only' USING ERRCODE = 'restrict_violation';
END
$guard$""",
    """CREATE OR REPLACE TRIGGER workflow_run_events_append_only
BEFORE UPDATE OR DELETE ON workflow_run_events
FOR EACH ROW EXECUTE FUNCTION workflow_run_events_append_only()""",
)
REMOVE: Final = ("DROP FUNCTION IF EXISTS workflow_run_events_append_only() CASCADE",)
