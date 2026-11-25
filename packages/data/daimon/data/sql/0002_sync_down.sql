-- Revision 0232's downgrade: drop what 0002_sync.sql made, so a later upgrade makes it again.
drop table if exists {{data}}.attachments, {{data}}.source_cursors, {{data}}.sync_runs;
delete from {{data}}.schema_version where file = '0002_sync.sql';
