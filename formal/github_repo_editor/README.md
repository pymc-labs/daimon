# GitHub repository editor saves

A browser page must not overwrite a chat edit made after it was opened. Two
submissions of one invitation must apply at most one save. The model checks
those two properties for changes to existing grant rows.

| Action | Implementation |
| --- | --- |
| `Render` | The agent branch of `confirm` in `packages/adapters/mcp/daimon/adapters/mcp/oauth_github.py` renders `agent_repo_snapshot` and signs it with the flow state and invitation. |
| `Begin`, `Check` | `github_connect.confirm` locks the flow and invitation, checks single use, then calls `agent_repo_snapshot(lock=True)` before writing. The snapshot includes grant versions, access, working selection and authorization status/version. |
| `Commit` | The route's transaction calls the existing grant, removal and working-repo stores, then commits. |
| `Rollback` | The transaction rolls back on validation errors or a serialization conflict. It leaves the invitation reusable. |
| `ChatChange` | `github_access.set_working_repo` and `remove_grant` lock existing grant rows before changing them. |

The model uses two browser submissions, one invitation, one rendered snapshot,
and one chat edit. Revision 0 represents the rendered grant values; each
committed edit advances it. Production uses the complete snapshot, rather than
a global revision counter. Both browser submissions carry the same form.
`Check` and `Commit` are separate actions with the database lock held between
them. A database transaction groups the grant changes and invitation consumption.

| Configuration | Verdict | Distinct states |
| --- | --- | ---: |
| `RepoEditorSafe` | Clean | 59 |
| `RepoEditorStale` | Violates `NoStaleSave` | 22 |
| `RepoEditorReplay` | Violates `AtMostOneSave` | 55 |

The stale-save counterexample is:

1. The person opens the page.
2. Chat changes an existing grant.
3. The person submits the old form.
4. With the snapshot check disabled, the browser change commits against the old choices.

The replay counterexample is:

1. Two submissions carry the same form.
2. The first submission commits and consumes the invitation.
3. With both checks disabled, the second submission commits again.

These checks are bounded safety results. They do not prove GitHub permissions,
OAuth, expiry, cancellation, new-grant insertion races, lock ordering across all
stores, or session refresh behavior. Existing cancellation and session models
cover their own narrower questions. No liveness claim is made. The model assumes
PostgreSQL row-lock and transaction semantics. The database regression
`test_agent_editor_saves_explicit_changes_and_rejects_stale_page` replays a
chat change between rendering and saving, a change between the display and
snapshot reads, a forced rollback, and simultaneous submissions. It checks
that rejected edits leave grants and the invitation intact and that concurrent
submissions produce one audit event. No live GitHub contract test or deployment
trace is part of this PR.

Run with:

```sh
FORMAL_CHECK_FILTER=github_repo_editor TLA2TOOLS_JAR=/path/to/tla2tools.jar formal/check.sh
```

The shared CI workflow already watches both implementation files. The registry
pins the verdict and state count for each configuration, uses one TLC worker,
and applies the existing time and metadir size guards.
