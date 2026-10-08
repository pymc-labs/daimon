"""Store assigned face variants and pre-rendered public image sizes.

downgrade: destructive
"""

from io import BytesIO

import sqlalchemy as sa
from alembic import op
from PIL import Image

revision: str = "0063_agent_avatar_face_combo"
down_revision: str | None = "0062_github_connect_origin"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("agent_avatars", sa.Column("face_combo", sa.Text(), nullable=True))
    op.add_column("agent_avatars", sa.Column("face_thumbnail", sa.LargeBinary(), nullable=True))
    op.add_column("agent_avatars", sa.Column("png_128", sa.LargeBinary(), nullable=True))
    op.add_column("agent_avatars", sa.Column("png_512", sa.LargeBinary(), nullable=True))
    op.add_column("agent_avatars", sa.Column("previous_sha256", sa.Text(), nullable=True))
    op.add_column("agent_avatars", sa.Column("previous_png", sa.LargeBinary(), nullable=True))
    op.add_column("agent_avatars", sa.Column("previous_png_128", sa.LargeBinary(), nullable=True))
    op.add_column("agent_avatars", sa.Column("previous_png_512", sa.LargeBinary(), nullable=True))
    connection = op.get_bind()
    for row in connection.execute(
        sa.text("SELECT tenant_id, agent_name, png FROM agent_avatars")
    ).mappings():
        png = row["png"]
        with Image.open(BytesIO(png)) as image:
            rgb = image.convert("RGB")
            sizes: list[bytes] = []
            for size in (128, 512):
                output = BytesIO()
                rgb.resize((size, size), Image.Resampling.LANCZOS).save(output, format="PNG")
                sizes.append(output.getvalue())
        connection.execute(
            sa.text(
                "UPDATE agent_avatars SET png_128 = :small, png_512 = :large "
                "WHERE tenant_id = :tenant_id AND agent_name = :agent_name"
            ),
            {
                "small": sizes[0],
                "large": sizes[1],
                "tenant_id": row["tenant_id"],
                "agent_name": row["agent_name"],
            },
        )
    # Old worker pods may still insert initials rows during a rolling deploy.
    # Leave these nullable and let the public route serve the original PNG.


def downgrade() -> None:
    op.drop_column("agent_avatars", "previous_png_512")
    op.drop_column("agent_avatars", "previous_png_128")
    op.drop_column("agent_avatars", "previous_png")
    op.drop_column("agent_avatars", "previous_sha256")
    op.drop_column("agent_avatars", "png_512")
    op.drop_column("agent_avatars", "png_128")
    op.drop_column("agent_avatars", "face_thumbnail")
    op.drop_column("agent_avatars", "face_combo")
