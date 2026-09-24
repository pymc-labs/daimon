----------------------------- MODULE DiscordInstall -----------------------------
(***************************************************************************)
(* A Discord guild leaves and rejoins while independent discord.py event  *)
(* tasks are pending. Gateway state updates the guild cache before it     *)
(* dispatches each callback. The remove callback archives only if the     *)
(* cache still says absent; join provisions and clears the archive.        *)
(*                                                                        *)
(* In the unsafe shape callbacks write independently. In the safe shape   *)
(* their DB transitions share a lock. The gateway may still update cache  *)
(* while a callback holds that lock.                                      *)
(*                                                                        *)
(* Source: packages/adapters/discord/daimon/adapters/discord/bot.py      *)
(* (_provision_joined_guild, on_guild_remove); discord.py 2.7.1           *)
(* discord/state.py parse_guild_delete/parse_guild_create updates cache   *)
(* before dispatch, and discord/client.py schedules callbacks as tasks.  *)
(***************************************************************************)
EXTENDS Naturals, TLC

CONSTANTS UseLifecycleLock, InitiallyArchived, RecoverArchivedOnBoot

VARIABLES cache, tenant, remove, join, lock

vars == <<cache, tenant, remove, join, lock>>

Init ==
    /\ cache = "present"
    /\ tenant = IF InitiallyArchived THEN "archived" ELSE "live"
    /\ remove = "idle"
    /\ join = IF InitiallyArchived THEN "pending" ELSE "idle"
    /\ lock = "free"

GatewayRemove ==
    /\ cache = "present"
    /\ remove = "idle"
    /\ cache' = "absent"
    /\ remove' = IF UseLifecycleLock THEN "pending" ELSE "checked"
    /\ UNCHANGED <<tenant, join, lock>>

GatewayRejoin ==
    /\ cache = "absent"
    /\ remove \in {"pending", "checked", "done"}
    /\ join = "idle"
    /\ cache' = "present"
    /\ join' = "pending"
    /\ UNCHANGED <<tenant, remove, lock>>

BootSweepSkipsArchived ==
    /\ InitiallyArchived
    /\ ~RecoverArchivedOnBoot
    /\ join = "pending"
    /\ join' = "skipped"
    /\ UNCHANGED <<cache, tenant, remove, lock>>

AcquireRemove ==
    /\ UseLifecycleLock
    /\ remove = "pending"
    /\ lock = "free"
    /\ lock' = "remove"
    /\ UNCHANGED <<cache, tenant, remove, join>>

CheckRemoveLocked ==
    /\ UseLifecycleLock
    /\ remove = "pending"
    /\ lock = "remove"
    /\ IF cache = "present"
          THEN /\ remove' = "done" /\ lock' = "free"
          ELSE /\ remove' = "checked" /\ UNCHANGED lock
    /\ UNCHANGED <<cache, tenant, join>>

ArchiveRemove ==
    /\ remove = "checked"
    /\ IF UseLifecycleLock THEN lock = "remove" ELSE TRUE
    /\ tenant' = "archived"
    /\ remove' = "done"
    /\ lock' = IF UseLifecycleLock THEN "free" ELSE lock
    /\ UNCHANGED <<cache, join>>

AcquireJoin ==
    /\ UseLifecycleLock
    /\ join = "pending"
    /\ lock = "free"
    /\ lock' = "join"
    /\ UNCHANGED <<cache, tenant, remove, join>>

ClearArchiveLocked ==
    /\ UseLifecycleLock
    /\ (InitiallyArchived => RecoverArchivedOnBoot)
    /\ join = "pending"
    /\ lock = "join"
    /\ cache = "present"
    /\ tenant' = "live"
    /\ join' = "done"
    /\ lock' = "free"
    /\ UNCHANGED <<cache, remove>>

SkipStaleJoinLocked ==
    /\ UseLifecycleLock
    /\ join = "pending"
    /\ lock = "join"
    /\ cache = "absent"
    /\ join' = "done"
    /\ lock' = "free"
    /\ UNCHANGED <<cache, tenant, remove>>

ClearArchiveUnlocked ==
    /\ ~UseLifecycleLock
    /\ (InitiallyArchived => RecoverArchivedOnBoot)
    /\ join = "pending"
    /\ cache = "present"
    /\ tenant' = "live"
    /\ join' = "done"
    /\ UNCHANGED <<cache, remove, lock>>

Quiescent ==
    /\ remove = "done"
    /\ join = "done"
    /\ UNCHANGED vars

Next ==
    \/ GatewayRemove
    \/ GatewayRejoin
    \/ BootSweepSkipsArchived
    \/ AcquireRemove
    \/ CheckRemoveLocked
    \/ ArchiveRemove
    \/ AcquireJoin
    \/ ClearArchiveLocked
    \/ SkipStaleJoinLocked
    \/ ClearArchiveUnlocked
    \/ Quiescent

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ cache \in {"present", "absent"}
    /\ tenant \in {"live", "archived"}
    /\ remove \in {"idle", "pending", "checked", "done"}
    /\ join \in {"idle", "pending", "done", "skipped"}
    /\ lock \in {"free", "remove", "join"}

JoinedTenantLive == (join = "done" /\ remove = "done" /\ cache = "present") => tenant = "live"
BootRecoveryLive == (join \in {"done", "skipped"} /\ cache = "present") => tenant = "live"
=============================================================================
