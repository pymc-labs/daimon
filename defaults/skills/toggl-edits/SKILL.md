---
name: toggl-edits
description: Update or bulk edit stopped Toggl Track entries for an explicitly named workspace member using a shared admin key.
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

toggl-edits changes only stopped entries that the Reports API attributes to the
explicitly named member in that workspace; running timers are out of scope.
The script checks every target before any mutation and refuses missing,
ambiguous or conflicting attribution. No fallback to the key owner's entries.
It exposes only update and bulk edit: no creation, deletion, project/task/user
admin or ownership reassignment. Every Reports query uses explicit YYYY-MM-DD
bounds, from today minus 365 days to today, chosen in UTC. Older entries refuse.
Running entries and very recent edits (Reports lag) are not supported; if the
report omits a target, nothing is changed. Do not bypass this check with another
endpoint or retry writes while waiting for Reports to catch up.

Stop someone's running timer in Toggl itself. Reports omit running entries, so a
shared key cannot verify a running entry's owner through this workflow.

On 403, report the missing workspace-admin/access/plan permission; never retry as
another person. Bulk edits can partly apply: report successful and failed IDs,
and never retry successes. After any uncertain result, read the specified
entries before retrying. Live Preview acceptance with a member different from
the key owner is required before rollout; unit fakes do not prove vendor access.
