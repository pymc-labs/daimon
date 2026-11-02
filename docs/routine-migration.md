# Move routines into Daimon

How an operator moves scheduled routines from another system into Daimon
without running a slot twice or skipping one, and how to hand them back. Run
every command with the scheduler's environment and database credentials.
[Routines](routines.md) covers what a routine is.

## Before you start

Stop the source system's scheduler and let its runs drain. For each routine,
record the last **attempted** slot, T. That includes failed, timed-out and
cancelled runs, but not slots skipped while a run was going.

Leave out routines the source system has turned off, so they stay paused.

## Move routines in

1. Create each routine paused, so it has no next fire time:

   ```bash
   daimon routines create --tenant TENANT_UUID --owner DISCORD_USER_ID \
     --agent daimon --schedule '0 9 * * *' --timezone UTC \
     --trigger 'Summarize my tasks' --destination dm --paused --dry-run
   ```

   Drop `--dry-run` to write it. The dry run checks the owner, agent and any
   `--source-channel`, and prints the recipient and budget source.

2. Link each one to its source routine id. Without `--apply` it is a dry run:

   ```bash
   daimon routines link-source --tenant TENANT_UUID --routine DAIMON_ROUTINE_UUID --source-id SOURCE_UUID
   daimon routines link-source --tenant TENANT_UUID --routine DAIMON_ROUTINE_UUID --source-id SOURCE_UUID --apply
   ```

3. Write `seed.json`, a non-empty JSON array with one object per routine:

   ```json
   [
     {
       "source_routine_id": "00000000-0000-0000-0000-000000000001",
       "last_completed_slot": "2026-10-26T09:00:00+00:00",
       "source_next_run": "2026-10-27T09:00:00+00:00",
       "cron_expr": "0 9 * * *",
       "timezone": "UTC"
     }
   ]
   ```

4. Preview, then apply, the first runs:

   ```bash
   daimon routines seed-first-run --file seed.json --tenant TENANT_UUID
   daimon routines seed-first-run --file seed.json --tenant TENANT_UUID --apply
   ```

The preview prints, per routine, T, the first slot after it and one of
`wait`, `fire next tick` or `roll forward unrun under skip`. Review every
skipped-slot warning first. Freshness uses
[`DAIMON_SCHEDULER__MAX_AGE_S`](configuration.md#daimon_scheduler__max_age_s),
900 seconds by default.

## What linking and seeding refuse

Both refuse a routine that is enabled or has ever fired or delivered. Linking
also refuses a routine already linked, or a source id linked to another
routine. Prompt text never links a routine.

Seeding checks the whole file and writes nothing if any entry fails. It
refuses unknown or duplicate ids, ids outside `--tenant`, and a cron or
timezone that differs from the Daimon routine. Daimon's first slot after T
must equal `source_next_run` exactly. Timestamps need an explicit offset, and
T cannot be in the future.

Applying stores T and sets the next fire time and `enabled` together. An
ordinary resume schedules from now, so use seeding for the first move.

## Hand routines back

Stop the Daimon scheduler, pause the moved routines, drain their runs and
deliveries, then export. `moved-ids.json` is a non-empty JSON array of source
ids.

```bash
daimon routines pause-for-handback --tenant TENANT_UUID --ids-file moved-ids.json
daimon routines pause-for-handback --tenant TENANT_UUID --ids-file moved-ids.json --apply
daimon routines export-rollback --tenant TENANT_UUID --ids-file moved-ids.json --output rollback.json
```

The pause records each routine's enabled value and next run, N. Repeating it
keeps the first values, and seeding or resuming clears them. The export never
writes to the database.

For each routine the export gives `enabled`, `resume_after` and
`expected_next_run`. The source system computes its next slot strictly after
`resume_after`, checks it equals `expected_next_run`, and restores `enabled`.

- A routine paused for handback exports its recorded `enabled`, with N as
  `expected_next_run`.
- A routine a person paused exports `enabled=false` and no next run, so it
  stays off.
- A slot that was claimed counts as run, even if the run failed. Never rerun
  it on the source.

## When an export is not final

Use only an export with `final=true`. `not_final_reasons` says what blocks it:

- a selected routine is enabled or still in flight;
- a delivery is pending or claimed;
- a past fire has no recorded slot;
- the slot record looks stale, for example after running an older scheduler;
- the next-slot check fails, such as around a daylight-saving change.

A routine left in flight after the scheduler stopped already counts in
`resume_after`. Check its logs and destination to see whether its output went
out. Then reconcile that one claim in the database before exporting again.
