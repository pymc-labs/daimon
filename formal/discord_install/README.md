# Discord guild install lifecycle

`DiscordInstall.tla` checks one guild leaving and rejoining while
`guild_remove`, `guild_join`, or startup recovery work is pending. The unsafe
configuration keeps the independent callback writes and retains the trace where
remove archives after rejoin clears the archive. The safe configuration
serializes tenant transitions per guild and checks current cache membership
under that lock. `DiscordBootUnsafe` and `DiscordBootSafe` separately start with
an archived tenant whose guild is present after startup; the unsafe shape
skips this already-known tenant, and the fixed path revives it.

The Python adapter uses discord.py 2.7.1. In that version,
`ConnectionState.parse_guild_delete` removes the guild from cache before
dispatching `guild_remove`; `parse_guild_create` adds it before dispatching
`guild_join`; and `Client.dispatch` schedules each handler as a separate task.
The production guard relies on those observed library semantics, not a general
Discord ordering guarantee. The same helper handles `on_ready` and mention
self-healing, which must check cache membership again after waiting for the
lock.

For startup recovery, `InitiallyArchived` starts with an archived DB row and a
populated cache, and `RecoverArchivedOnBoot` selects whether `on_ready` runs
the recovery path. A process restart resets the lock to free while the tenant's
archive remains in the DB. The model assumes the previous process is gone and
does not model a crash during a tenant status transaction or two live bot
processes. The Postgres regression test covers startup recovery and verifies it
does not reissue signup credit or post a second welcome; it also checks guild
and global command synchronization still run.

## Results

| Configuration | Property | Result |
| --- | --- | --- |
| `DiscordInstallUnsafe` | `JoinedTenantLive` | Counterexample: remove checks absence, gateway rejoins, join clears archive, delayed remove archives |
| `DiscordInstallSafe` | `JoinedTenantLive` | Clean within the finite lifecycle model |
| `DiscordBootUnsafe` | `BootRecoveryLive` | Counterexample: an archived known guild remains archived when `on_ready` skips it |
| `DiscordBootSafe` | `BootRecoveryLive` | Clean: cached archived tenant is revived |

These are bounded safety checks only. There is no fairness assumption and no
claim that a callback or database write eventually succeeds. The callback
property is evaluated only after the one remove and one join have completed;
the boot property is evaluated after that one startup recovery decision. The
bound is one guild, one remove/rejoin pair or one boot recovery, and one bot
process. Overlapping bot processes are outside the model.

Run both through `formal/check.sh`; state counts are pinned in
[`expected.tsv`](../expected.tsv).
