-- After a restore with --no-privileges or --no-owner: put back what that drops.
-- Policies name no role, so only ownership and grants need reasserting.
do $$
declare v record;
begin
  for v in
    select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace
    where n.nspname = '{{views}}' and c.relkind = 'v'
      and pg_get_userbyid(c.relowner) <> 'daimon_data_view_owner'
  loop
    execute format('alter view %I.%I owner to daimon_data_view_owner', '{{views}}', v.relname);
  end loop;
end $$;
