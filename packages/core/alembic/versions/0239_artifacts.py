"""Artifacts: web pages an agent publishes, with their versions and shared data.

`artifacts.link_secret` is Fernet ciphertext, set exactly when the page is public. The
checks hold the hard rules for anonymous viewers of a public page: no data writes, not
both data reads and events, no ask, and always an expiry. `web_threads.artifact_id`
marks a viewer's ask thread for a page, one per person and page. A version's thumbnail
and a viewer's consent to a page's asks are rows of their own. `artifacts.imported_from`
is the address a page had elsewhere, unique per workspace: a re-import finds the page by
it and a legacy host redirects by it. `imported_state` is a digest of the page as its
import left it: a re-import replaces it only while it still matches. Contract only for
the unique index on `web_threads.artifact_id`, which the previous release never writes.

downgrade: destructive
schema: contract
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0239_artifacts"
down_revision: str | None = "0238_dm_activity"
branch_labels: str | None = None
depends_on: str | None = None

_UUID = postgresql.UUID(as_uuid=True)


def _now(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "artifacts",
        sa.Column("id", _UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("public_id", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("owner_account_id", _UUID, nullable=False),
        sa.Column("access", sa.Text(), server_default="workspace", nullable=False),
        sa.Column("people", postgresql.ARRAY(_UUID), server_default="{}", nullable=False),
        sa.Column("editors", postgresql.ARRAY(_UUID), server_default="{}", nullable=False),
        sa.Column("editors_can_share", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("data", sa.Text(), server_default="off", nullable=False),
        sa.Column("events", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("ask", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("data_scale", sa.Integer(), server_default="1", nullable=False),
        sa.Column("link_secret", sa.LargeBinary(), nullable=True),
        sa.Column("imported_from", sa.Text(), nullable=True),
        sa.Column("imported_state", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("data_revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "collection_rules", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False
        ),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint("public_id ~ '^[a-z2-7]{10}$'", name="ck_artifacts_public_id"),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$'", name="ck_artifacts_slug"
        ),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 120", name="ck_artifacts_title"),
        sa.CheckConstraint(
            "access IN ('workspace', 'people', 'public')", name="ck_artifacts_access"
        ),
        sa.CheckConstraint("data IN ('off', 'read', 'write')", name="ck_artifacts_data"),
        sa.CheckConstraint("data_scale BETWEEN 1 AND 10", name="ck_artifacts_data_scale"),
        sa.CheckConstraint(
            "(access = 'public') = (link_secret IS NOT NULL)", name="ck_artifacts_link"
        ),
        sa.CheckConstraint(
            "access <> 'public' OR (data <> 'write' AND NOT (data = 'read' AND events)"
            " AND NOT ask)",
            name="ck_artifacts_public",
        ),
        sa.CheckConstraint(
            "NOT (owner_account_id = ANY (editors))", name="ck_artifacts_owner_not_editor"
        ),
        sa.CheckConstraint(
            "cardinality(people) <= 200 AND cardinality(editors) <= 50", name="ck_artifacts_sizes"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_artifacts_public_id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_artifacts_tenant_slug"),
        # The address leads, so a lookup by address alone uses this index too.
        sa.UniqueConstraint("imported_from", "tenant_id", name="uq_artifacts_imported_from"),
    )
    op.create_index("ix_artifacts_owner", "artifacts", ["owner_account_id"])
    op.create_index(
        "ix_artifacts_expires_at",
        "artifacts",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_table(
        "artifact_versions",
        sa.Column("id", _UUID, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("artifact_id", _UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("html", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("published_by_account_id", _UUID, nullable=True),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("origin_platform", sa.Text(), nullable=True),
        sa.Column("origin_thread_id", sa.Text(), nullable=True),
        sa.Column("origin_message_id", sa.Text(), nullable=True),
        _now("created_at"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["published_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_id", "version", name="uq_artifact_versions_version"),
    )
    op.create_index(
        "ix_artifact_versions_origin",
        "artifact_versions",
        ["origin_platform", "origin_thread_id", "origin_message_id"],
    )
    op.create_index(
        "ix_artifact_versions_publisher", "artifact_versions", ["published_by_account_id"]
    )
    op.create_table(
        "artifact_documents",
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("artifact_id", _UUID, nullable=False),
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column("doc_id", sa.Text(), nullable=False),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("updated_by_account_id", _UUID, nullable=True),
        sa.Column("created_by_account_id", _UUID, nullable=True),
        sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
        _now("created_at"),
        _now("updated_at"),
        sa.CheckConstraint(
            "collection ~ '^[A-Za-z0-9_-]{1,64}$'", name="ck_artifact_documents_collection"
        ),
        sa.CheckConstraint(
            "doc_id ~ '^[A-Za-z0-9_.:-]{1,128}$'", name="ck_artifact_documents_doc_id"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_account_id"], ["accounts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("artifact_id", "collection", "doc_id"),
    )
    op.create_index(
        "ix_artifact_documents_account", "artifact_documents", ["updated_by_account_id"]
    )
    op.create_index(
        "ix_artifact_documents_creator", "artifact_documents", ["created_by_account_id"]
    )
    op.add_column("web_threads", sa.Column("artifact_id", _UUID, nullable=True))
    op.create_foreign_key(
        "fk_web_threads_artifact",
        "web_threads",
        "artifacts",
        ["artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_web_threads_person_artifact",
        "web_threads",
        ["person_id", "artifact_id"],
        unique=True,
        postgresql_where=sa.text("artifact_id IS NOT NULL AND deleted_at IS NULL"),
    )
    # Hosting: version thumbnails and viewers' consent to a page's asks.
    op.create_table(
        "artifact_thumbnails",
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("artifact_id", _UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("png", sa.LargeBinary(), nullable=False),
        _now("created_at"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["artifact_id", "version"],
            ["artifact_versions.artifact_id", "artifact_versions.version"],
            ondelete="CASCADE",
            name="fk_artifact_thumbnails_version",
        ),
        sa.PrimaryKeyConstraint("artifact_id", "version"),
    )
    op.create_table(
        "artifact_ask_consents",
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("artifact_id", _UUID, nullable=False),
        sa.Column("person_id", _UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        _now("created_at"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifacts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_id"], ["web_people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("artifact_id", "person_id"),
    )


def downgrade() -> None:
    op.drop_table("artifact_ask_consents")
    op.drop_table("artifact_thumbnails")
    op.drop_index("uq_web_threads_person_artifact", table_name="web_threads")
    op.drop_constraint("fk_web_threads_artifact", "web_threads", type_="foreignkey")
    op.drop_column("web_threads", "artifact_id")
    op.drop_table("artifact_documents")
    op.drop_table("artifact_versions")
    op.drop_table("artifacts")
