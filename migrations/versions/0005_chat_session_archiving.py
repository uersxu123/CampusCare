"""Add one-way student conversation archiving."""

from alembic import op
import sqlalchemy as sa


revision = "0005_chat_session_archiving"
down_revision = "0004_migrate_legacy_knowledge"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("chat_sessions") as batch:
        batch.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_chat_sessions_archived_at", ["archived_at"], unique=False)
        batch.create_index(
            "ix_chat_sessions_user_archive_updated",
            ["user_id", "archived_at", "updated_at"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table("chat_sessions") as batch:
        batch.drop_index("ix_chat_sessions_user_archive_updated")
        batch.drop_index("ix_chat_sessions_archived_at")
        batch.drop_column("archived_at")
