"""Add durable incremental conversation summaries."""
from alembic import op
import sqlalchemy as sa


revision = "0002_conversation_summaries"
down_revision = "20260727_07"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "conversation_summaries" in inspector.get_table_names():
        columns = {column["name"]: column for column in inspector.get_columns("conversation_summaries")}
        with op.batch_alter_table("conversation_summaries") as batch:
            if "user_id" in columns and not columns["user_id"]["nullable"]:
                batch.alter_column("user_id", existing_type=sa.Integer(), nullable=True)
            if "covered_until_message_id" in columns and columns["covered_until_message_id"]["nullable"]:
                bind.execute(sa.text("UPDATE conversation_summaries SET covered_until_message_id = 0 WHERE covered_until_message_id IS NULL"))
                batch.alter_column(
                    "covered_until_message_id",
                    existing_type=sa.Integer(),
                    nullable=False,
                    server_default="0",
                )
        return

    op.create_table(
        "conversation_summaries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("covered_until_message_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("session_id", name="uq_conversation_summaries_session_id"),
    )
    op.create_index("ix_conversation_summaries_session_id", "conversation_summaries", ["session_id"], unique=True)


def downgrade():
    op.drop_table("conversation_summaries")
