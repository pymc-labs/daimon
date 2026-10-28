---
name: toggl-edits
description: Update, stop or bulk edit existing Toggl Track entries for an explicitly named workspace member using a shared admin key.
---

Use the dedicated `time-editor` agent, never default Daimon. If needed, use
`request_agent_key` with `key=TOGGL_API_TOKEN` and the concrete waiting task.
The shared token owner needs the workspace administrator role and access to the
entry's project. Every user of this agent can read/use the mounted shared key;
the script does not enforce requester identity or sandbox credential isolation.
Do not ask for a pasted key or assume the token owner is the requester.

Require workspace ID, entry IDs, and exactly one explicit person: Toggl user ID
from that workspace's member list or their unique email. Never translate “my”
to the key owner. Resolve the requesting person from reliable workspace context,
or ask which person before writing. A concrete request with those targets
supplies authorization; do not add an approval loop. Do not run unattended.

Write request JSON to a working file and run
`python3 <this-skill>/scripts/toggl_edits.py <request.json>`.
Python standard library only. It reads only the named key from
`/mnt/session/uploads/.env`, without executing that file. Read
[references/requests.md](references/requests.md) for schemas and API references.

All target entries must belong to the named member and workspace. The script
fetches every entry before any mutation and refuses missing, ambiguous or
conflicting attribution. No fallback to the key owner's entries. It exposes
only update, stop and bulk edit: no creation, deletion, project/task/user admin,
or ownership reassignment. Stop uses workspace PUT with current UTC stop time
and duration computed from the entry's start; it does not call PATCH stop.

On 403, report the missing workspace-admin/access/plan permission; never retry as
another person. Bulk edits can partly apply: report successful and failed IDs,
and never retry successes. After any uncertain result, read the specified
entries before retrying. Live Preview acceptance with a member different from
the key owner is required before rollout; unit fakes do not prove vendor access.
