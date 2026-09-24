"""Add context-memory V2 governance and trace manifest fields."""

from alembic import op
import sqlalchemy as sa


revision = "0007_context_memory_v2"
down_revision = "0006_context_reconnect"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_memories") as batch:
        batch.add_column(sa.Column("memory_key", sa.String(128), nullable=True))
        batch.add_column(sa.Column("last_used_at", sa.DateTime(), nullable=True))
        batch.create_index(
            "ix_user_memories_user_status_category_key",
            ["user_id", "status", "category", "memory_key"],
            unique=False,
        )

    with op.batch_alter_table("agent_run_traces") as batch:
        batch.add_column(sa.Column("context_manifest_json", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE agent_run_traces SET context_manifest_json = '{}' "
            "WHERE context_manifest_json IS NULL"
        )
    )
    with op.batch_alter_table("agent_run_traces") as batch:
        batch.alter_column(
            "context_manifest_json",
            existing_type=sa.Text(),
            existing_nullable=True,
            nullable=False,
        )


def downgrade():
    with op.batch_alter_table("agent_run_traces") as batch:
        batch.drop_column("context_manifest_json")
    with op.batch_alter_table("user_memories") as batch:
        batch.drop_index("ix_user_memories_user_status_category_key")
        batch.drop_column("last_used_at")
        batch.drop_column("memory_key")
