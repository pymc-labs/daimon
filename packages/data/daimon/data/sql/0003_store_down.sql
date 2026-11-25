-- Revision 0233's downgrade: drop what 0003_store.sql and its grants made, so a later upgrade
-- makes it again.
drop view if exists {{views}}.sources, {{views}}.records, {{views}}.datasets, {{views}}.metrics;
drop function if exists {{data}}.caller_sql(text, int), {{data}}.protect_view(regclass, text[]);
drop table if exists {{data}}.metrics, {{data}}.datasets;
delete from {{data}}.schema_version where file = '0003_store.sql';
