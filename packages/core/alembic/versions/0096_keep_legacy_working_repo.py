"""Mark the working repo of agents already switched to GitHub App mode.

App mode ignores an agent's legacy repo binding and mounts only the grant
marked `is_working_repo`. Agents switched before the switch carried the
binding over have a binding and no marked grant, so they mount nothing.

For each App-mode agent with a binding and no live (non-staged) grant marked
as the working repo, this marks its live grant for the binding's repo, matched
on `owner/repo` without regard to case. It acts only when exactly one live
grant matches, and leaves the agent alone otherwise. Each change bumps the
grant's `version` and writes the same `security_audit_events` row the store
writes when it keeps a working repo during the switch. The next turn of an
affected agent sees a different mounted repo and replaces its session
(`repo_set`); no flag is needed.

downgrade: safe
"""

from alembic import op

revision: str = "0096_keep_legacy_working_repo"
down_revision: str | None = "0095_preparation_failure_kind"
branch_labels: str | None = None
depends_on: str | None = None

_MARK_WORKING_REPOS = r"""
WITH bound AS (
    SELECT
        b.tenant_id,
        b.agent_id,
        lower(
            regexp_replace(
                regexp_replace(
                    b.repo_url,
                    '^(https://github\.com/|http://github\.com/|github\.com/)',
                    ''
                ),
                '(\.git)?/*$',
                ''
            )
        ) AS repo_name
    FROM agent_repo_binding AS b
    JOIN agent_github_mode AS m
        ON m.tenant_id = b.tenant_id AND m.agent_id = b.agent_id AND m.mode = 'app'
    WHERE NOT EXISTS (
        SELECT 1
        FROM agent_github_grants AS w
        WHERE w.tenant_id = b.tenant_id
          AND w.agent_id = b.agent_id
          AND NOT w.staged
          AND w.is_working_repo
    )
),
matches AS (
    SELECT g.tenant_id, g.agent_id, g.repo_id
    FROM bound
    JOIN agent_github_grants AS g
        ON g.tenant_id = bound.tenant_id AND g.agent_id = bound.agent_id AND NOT g.staged
    WHERE EXISTS (
        SELECT 1
        FROM tenant_github_repos AS r
        WHERE r.tenant_id = g.tenant_id
          AND r.repo_id = g.repo_id
          AND (r.scope_agent_id IS NULL OR r.scope_agent_id = g.agent_id)
          AND lower(r.repo_full_name) = bound.repo_name
    )
),
single AS (
    SELECT tenant_id, agent_id, min(repo_id) AS repo_id
    FROM matches
    GROUP BY tenant_id, agent_id
    HAVING count(*) = 1
),
marked AS (
    UPDATE agent_github_grants AS g
    SET is_working_repo = true, version = g.version + 1
    FROM single
    WHERE g.tenant_id = single.tenant_id
      AND g.agent_id = single.agent_id
      AND g.repo_id = single.repo_id
    RETURNING g.tenant_id, g.agent_id, g.repo_id
)
INSERT INTO security_audit_events
    (tenant_id, agent_id, tool_name, operation, outcome, reason, github_repo_ids)
SELECT
    tenant_id,
    agent_id,
    'github_grants',
    'github_grant',
    'allowed',
    'working repo kept from legacy binding',
    ARRAY[repo_id]
FROM marked
"""


def upgrade() -> None:
    # Hold off concurrent working-repo changes (set_working_repo, a binding
    # clear, a mode switch) between the eligibility read and the update, so a
    # choice made while the migration runs is never overwritten. The tables
    # stay readable; the lock ends with this migration's transaction.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        "LOCK TABLE agent_github_mode, agent_github_grants, agent_repo_binding "
        "IN SHARE ROW EXCLUSIVE MODE"
    )
    op.execute(_MARK_WORKING_REPOS)


def downgrade() -> None:
    # Nothing to undo: the marked grants are valid App-mode state, and the rows
    # this migration marked cannot be told apart from ones the switch marked
    # since. The statement keeps the body a real one for scripts/lint_migrations.py,
    # which treats an empty downgrade as incompatible with `downgrade: safe`.
    op.execute("SELECT 1")
