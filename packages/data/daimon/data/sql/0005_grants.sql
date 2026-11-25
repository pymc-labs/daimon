-- Idempotent, run by every apply once 0005_ops.sql is in. A reader sees none of the
-- operations tables; only the parity role may ask what a person would read.
revoke all on {{data}}.declared_state, {{data}}.declared_bindings, {{data}}.parity_runs
  from daimon_data_reader;
revoke all on function {{data}}.parity_visible(uuid, uuid) from public;
grant execute on function {{data}}.parity_visible(uuid, uuid) to daimon_data_parity;
