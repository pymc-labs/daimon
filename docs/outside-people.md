# Rooms with outside people

How Daimon treats Discord rooms that people outside your organisation can view,
and rooms that block someone by name. It applies when the **Rooms with outside
people** feature is on ([Features](features.md)). In Report only mode nothing
changes; Daimon only logs what it would do.

## Contained and narrow rooms

Daimon sorts each room by who can view it. A room that an outsider can view is
**contained**: it gets no internal data. A room is **narrow** when it is limited
to the people inside it.

A room is narrow in these cases:

- three or more team members cannot read it;
- a member overwrite on it denies View Channel or Read Message History to an
  internal person who cannot read it by any other route. This is seal by name;
- it is a private thread;
- Daimon does not know who can view it. An unknown room counts as contained
  and narrow until the next check.

Admins and owners never count. A person with no internal role is an outsider,
so a block on that person makes no room narrow. A public thread and a forum
post follow their parent room. A change to a role or an overwrite makes Daimon
check the server again.

## Read a narrow room from your own DM

Only people inside a narrow room can read it through Daimon. On Discord, a routine that
posts to a narrow room, or to a thread in it, also reads that room, as a chat there does. It
reads no other narrow room. A routine with no destination, one that reports to a DM, or one
on another platform gets only the exception below. That exception: in
your own DM, Daimon reads a narrow room for you when Discord lets you view it
and read its history. This covers your DM chats and your own DM routines.

Daimon checks your access on every read, so a removed person loses it at once.
Private threads need membership. Watches, timers, helpers, web chat, agent keys
and Slack get no exception.

The exception covers only rooms that this feature made narrow. A room whose
readers an admin limited by hand ([Permissions](permissions.md)) keeps its rule:
only conversations inside it read it.

## After a narrow read

For the rest of a turn that read a narrow room this way, Daimon:

- posts in no channel, and sends a direct message only to you;
- saves no routine and no timer, because a later run would carry what it read.

The reply and the content stay in your DM chat. Other tools keep their own
rules, as for a turn inside the room. A watch needs your approval on a card.
Publishing and connector writes follow the agent's tool rules, and a run that
nobody watches follows the [unattended rules](unattended.md). The limit does
not seal the content: a person who can read a room can carry it out.

A turn that you start later is your own act and has no such limit. DM memory
stays read-only, so a narrow room's content does not enter shared memory.
