-- Idempotent, run by every apply once 0002_sync.sql is in. A reader sees an attachment only
-- through its record's policy, and never a source's runs or cursor.
revoke all on {{data}}.sync_runs, {{data}}.source_cursors, {{data}}.attachments from daimon_data_reader;
grant select on {{data}}.attachments to daimon_data_reader;
