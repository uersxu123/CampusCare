"""Add backward-compatible clarification resume context."""

from alembic import op
import sqlalchemy as sa


revision = "0010_clarification_resume"
down_revision = "0009_knowledge_governance"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("pending_clarifications") as batch:
        batch.add_column(
            sa.Column("resume_context_json", sa.Text(), nullable=False, server_default="{}")
        )


def downgrade():
    with op.batch_alter_table("pending_clarifications") as batch:
        batch.drop_column("resume_context_json")
