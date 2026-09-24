"""Track clarification progress separately from total rounds."""

from alembic import op
import sqlalchemy as sa


revision = "0011_clarification_retry"
down_revision = "0010_clarification_resume"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("pending_clarifications") as batch:
        batch.add_column(
            sa.Column("no_progress_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("finish_reason", sa.String(length=64), nullable=False, server_default="")
        )


def downgrade():
    with op.batch_alter_table("pending_clarifications") as batch:
        batch.drop_column("finish_reason")
        batch.drop_column("no_progress_count")
