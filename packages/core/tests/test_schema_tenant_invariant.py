"""Systemic guard against the recurring cross-tenant-isolation bug class.

Three tenant-isolation incidents (#197 Slack thread-key map, #198 resolver
liveness, #199/#1 GitHub App installation token) share one root cause: a table
that holds tenant-owned data but has no ``tenant_id`` column, so a lookup by a
"unique-looking" key silently crosses tenants.

This test makes that class impossible to introduce silently. Every ORM table
must EITHER carry a ``tenant_id`` column OR be listed in
``_TENANT_ID_EXEMPT`` with a written reason. A new table with no ``tenant_id``
fails the test until someone justifies the exemption — turning "did anyone
think about tenancy for this table?" from tribal knowledge into a hard gate.

Being on the exempt list is NOT a clean bill of health: an entry can still be
a real bug (e.g. ``github_app_installations`` is exempt because tenant is
genuinely unknowable at GitHub-install time, yet its unfiltered
``get_for_repo`` is issue #1). The list documents WHY the column is absent and
what carries isolation instead; the store-level tenant filtering is what
actually enforces it.
"""

from __future__ import annotations

from daimon.core import _models

# Table name -> reason it legitimately has no tenant_id column.
# Adding a table here is a deliberate act: state what carries isolation instead.
_TENANT_ID_EXEMPT: dict[str, str] = {
    # The tenant identity table itself; its `id` IS the tenant_id.
    "tenants": "identity table — `id` is the tenant_id",
    # Keyed by agent_id, which is uuid5(tenant_id, ma_agent_id) via
    # derive_agent_uuid — tenant-salted, so globally unique per tenant.
    "agent_github_binding": "keyed by tenant-salted agent_id (uuid5)",
    "agent_google_binding": "keyed by tenant-salted agent_id (uuid5)",
    # Keyed by principal_id (globally unique UUID). Teardown-orphan gap is #199.
    "github_credentials": "keyed by principal_id; teardown gap tracked in #199",
    # GitHub-side concept: the install webhook carries no Daimon tenant, so
    # tenant is not knowable at install time. Isolation must be enforced by
    # the binding side (repo-access proven at bind). Unfiltered get_for_repo
    # is issue #1 — this exemption documents the structural gap, not safety.
    "github_app_installations": "no Daimon tenant at GitHub-install time; see #1",
    # Slack-native tables keyed by team_id, which is 1:1 with a tenant's
    # external_id (tenant = uuid5('slack', team_id)).
    "slack_bot_tokens": "keyed by team_id (1:1 with tenant external_id)",
    "slack_user_tokens": "keyed by team_id (1:1 with tenant external_id)",
    "slack_connect_prompts": "keyed by team_id (1:1 with tenant external_id)",
    "slack_event_dedup": "keyed by team_id (1:1 with tenant external_id)",
    # Account-scoped: account_id is a globally unique UUID that maps to exactly
    # one tenant. user_config cascades from accounts.
    "user_config": "keyed by account_id (globally unique, maps to one tenant)",
    # File-GC queue keyed by server-minted file_id; rows are transient and
    # reference no tenant-owned data beyond the opaque handle.
    "pending_file_deletes": "keyed by server-minted file_id; transient GC queue",
    # Cross-tenant BY DESIGN: links a CLI principal to a platform principal for
    # operator impersonation. Both endpoints are globally unique principal UUIDs.
    "principal_links": "cross-principal link table (both PKs globally unique UUIDs)",
}


def test_every_table_has_tenant_id_or_documented_exemption() -> None:
    """Every ORM table carries tenant_id or is a documented exemption.

    Catches the recurring bug class: a tenant-owned table with no tenant_id
    column, reachable by a non-globally-unique key.
    """
    offenders: list[str] = []
    for table in _models.Base.metadata.sorted_tables:
        has_tenant_id = "tenant_id" in {c.name for c in table.columns}
        if not has_tenant_id and table.name not in _TENANT_ID_EXEMPT:
            offenders.append(table.name)

    assert not offenders, (
        "these tables have no `tenant_id` column and no documented exemption in "
        "_TENANT_ID_EXEMPT — either add tenant_id or justify why isolation is "
        f"carried some other way: {sorted(offenders)}"
    )


def test_exemption_list_has_no_stale_entries() -> None:
    """Every exemption names a table that (a) exists and (b) still lacks tenant_id.

    Keeps the ledger honest: if a table gains a tenant_id column later, its
    now-obsolete exemption must be removed rather than masking a future table
    that shadows the same name.
    """
    tables = {t.name: t for t in _models.Base.metadata.sorted_tables}
    stale: list[str] = []
    for name in _TENANT_ID_EXEMPT:
        table = tables.get(name)
        if table is None:
            stale.append(f"{name} (table no longer exists)")
        elif "tenant_id" in {c.name for c in table.columns}:
            stale.append(f"{name} (now has tenant_id — drop the exemption)")

    assert not stale, f"stale entries in _TENANT_ID_EXEMPT: {stale}"
