# Production canary: disabled pending driver approval

No production action or timer installation was performed for this change.
The default configuration sets `prod.enabled=false` and an empty guild allow-list.
Even `--go` cannot bypass those settings or the hard internal-guild allow-list.

The driver must obtain approval and make these changes before enabling it:

1. Invite the non-admin QA bot `1533049261032341668` into an approved internal
   guild: PyMC `745261709622771773` or Skunkworks `1533730917854609528`.
   Add the admin QA bot `1533287901595435129` only if separately needed;
   the two-turn production canary does not use admin actions.
2. Set the production worker environment variable
   `DAIMON_DISCORD__QA_BOT_USER_IDS` to a JSON array containing the actual QA bot
   IDs that will post, preserving existing approved IDs. If both bots are approved,
   the added IDs are `["1533049261032341668", "1533287901595435129"]`.
   Deploy/restart is a production change requiring the driver's approval path.
3. Create a dedicated internal QA category. Grant the QA bot View Channel,
   Send Messages, Read Message History, Manage Channels, Add Reactions,
   Manage Messages and Attach Files; grant Daimon View Channel, Send Messages,
   Read Message History, Create Public Threads, Manage Threads and Embed Links.
   Verify inherited overwrites. Grant no access to customer guilds/channels.
4. Confirm the effective agent in this category resolves to the approved
   `claude-haiku-4-5-20251001` canary agent. Ensure its costs fit the driver's
   per-turn estimate. Do not change the customer deployment's default model to
   run QA; use an approved category/channel scope when available.
5. Fill `prod.guild_id`, `category_id`, `daimon_id`, `project`, `qa_user_ids`,
   `guild_allowlist` and `database_env` with verified production values. The
   configured guild must be in both the explicit allow-list and the hard-coded
   two-guild set. `project` selects production Cloud Logging; do not reuse
   staging logs to prove production assertions. If approved read-only DB access
   is unavailable, accounting remains conservatively estimated and `db_check`
   returns PENDING.
6. Review the catalog's sole two-turn canary and the root Opus alert destination,
   then set `prod.enabled=true` and explicitly pass `--env prod --go`. Production
   permits only new-channel, mention, thread-reply and wait steps; no admin,
   restart, DM, setup or teardown hook is allowed.

The current runner creates a fresh dedicated QA channel for each run and deletes
it afterward, including its threads. It refuses writes to existing channels.
A persistent pre-existing canary channel would require a separately reviewed
ownership/cleanup contract; it is not enabled by this implementation.

Use the same shared daily ledger across staging and production. Review one
manual run before copying the staging timer to a separate production service.
Do not install or enable either service as part of a worker implementation task.
