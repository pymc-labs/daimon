# Toggl request schema

```json
{"operation":"update","workspace_id":123,"person":{"email":"named.member@example.com"},"entry_ids":[456],"changes":{"description":"Corrected description"}}
```

`person` contains exactly one of `email` or `user_id`. The latter is the Toggl
user's `id` in `GET /api/v9/workspaces/{workspace_id}/users`, not the separate
workspace-membership row ID. Matching email is case-insensitive and must resolve
to exactly one active member. The key owner's user ID is never used as a default.

`update` and `stop` require exactly one entry ID. `update` changes can include
`description`, `project_id`, `task_id`, `start`, `stop`, `duration`, `tags`,
`billable`. Project/task IDs can be null to clear them. Start/stop timestamps
must include timezone. A duration change computes a matching stop; explicit
start/stop/duration must agree. Stop supplies no changes; its timestamp is UTC now
and duration is elapsed whole seconds from the verified entry's start. Stopped
entries and future starts are refused. PUT explicitly retains verified user_id
and workspace_id. Concurrent edits between read and write remain possible;
read back after uncertain outcomes and use this workflow for discrete corrections.

```json
{"operation":"stop","workspace_id":123,"person":{"user_id":789},"entry_ids":[456]}
```

`bulk-edit` requires 1–100 unique entry IDs belonging to that one person and
`operations` containing 1–20 JSON Patch `replace` operations. Supported paths are
`/description`, `/project_id`, `/tags`, `/billable`. No changes field. Other patch
verbs/paths (including user/workspace reassignment) are refused.

```json
{"operation":"bulk-edit","workspace_id":123,"person":{"email":"named.member@example.com"},"entry_ids":[456,457],"operations":[{"op":"replace","path":"/description","value":"Corrected"}]}
```

The [Track v9 reference](https://engineering.toggl.com/docs/track/api/time_entries/)
documents workspace PUT update and bulk PATCH. Stop here is a PUT update with
stop/duration; it deliberately does not use PATCH stop. The official
[role guide](https://support.toggl.com/en-us/article/access-rights-and-privileges-108y786/)
establishes workspace-admin editing of other members' entries. Actual token,
plan, private-project and locked-entry permissions need live acceptance.
Ownership preflight uses workspace-scoped
[Reports v3 detailed search](https://engineering.toggl.com/docs/track/reports/detailed_reports/),
never `/me`. For each explicit entry ID it sends `time_entry_ids: [id]`,
`user_ids: [resolved_user]`, `grouped: false`, `page_size: 2`, `rounding: 0`.
It requires exactly one row containing exactly one matching entry. Report rows
provide `user_id`; nested `time_entries` provide `id`, `seconds`, `start`, `stop`
(as described in the [report schema](https://engineering.toggl.com/docs/track/reports/saved_reports/)).
Workspace identity comes from the authenticated workspace-scoped endpoint;
conflicting owner/workspace fields, missing timing or ambiguous results refuse
all writes. Null `stop` explicitly identifies a running entry. Report access
requires the relevant plan and workspace-admin/project permission.
