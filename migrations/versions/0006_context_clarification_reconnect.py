"""Add context clarification, reconnect turns, and user memories."""

from alembic import op
import sqlalchemy as sa


revision = "0006_context_reconnect"
down_revision = "0005_chat_session_archiving"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if {"pending_clarifications", "chat_turns", "user_memories"}.issubset(tables):
        columns = {column["name"] for column in inspector.get_columns("pending_clarifications")}
        if {"task_kind", "original_message", "round_count"}.issubset(columns):
            return
    if "pending_clarifications" in tables:
        columns = {column["name"] for column in inspector.get_columns("pending_clarifications")}
        target_columns = {"task_kind", "original_message", "round_count"}
        if not target_columns.issubset(columns):
            if "pending_clarifications_legacy_0006" in tables:
                raise RuntimeError("存在两个不兼容的 pending_clarifications 历史表，无法安全迁移")
            op.rename_table("pending_clarifications", "pending_clarifications_legacy_0006")
    op.create_table(
        "pending_clarifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="WAITING_USER"),
        sa.Column("task_kind", sa.String(64), nullable=False),
        sa.Column("original_message", sa.Text(), nullable=False),
        sa.Column("known_arguments_json", sa.Text(), nullable=False),
        sa.Column("missing_arguments_json", sa.Text(), nullable=False),
        sa.Column("approved_question", sa.Text(), nullable=False),
        sa.Column("round_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("max_rounds", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    _indexes("pending_clarifications", ["public_id", "user_id", "session_id", "status", "task_kind", "expires_at"])
    op.create_index("ix_pending_clarifications_session_status", "pending_clarifications", ["session_id", "status"])

    op.create_table(
        "chat_turns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("user_message_id", sa.Integer(), sa.ForeignKey("chat_messages.id"), nullable=True),
        sa.Column("assistant_message_id", sa.Integer(), sa.ForeignKey("chat_messages.id"), nullable=True),
        sa.Column("trace_id", sa.Integer(), sa.ForeignKey("agent_run_traces.id"), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="RECEIVED"),
        sa.Column("partial_content", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("user_id", "request_id", name="uq_chat_turns_user_request"),
    )
    _indexes("chat_turns", ["public_id", "user_id", "session_id", "user_message_id", "assistant_message_id", "trace_id", "status"])
    op.create_index("ix_chat_turns_session_status", "chat_turns", ["session_id", "status"])

    op.create_table(
        "user_memories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.Integer(), sa.ForeignKey("chat_messages.id"), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    _indexes("user_memories", ["public_id", "user_id", "category", "source_message_id", "status", "expires_at"])


def downgrade():
    op.drop_table("user_memories")
    op.drop_table("chat_turns")
    op.drop_table("pending_clarifications")
    inspector = sa.inspect(op.get_bind())
    if "pending_clarifications_legacy_0006" in inspector.get_table_names():
        op.rename_table("pending_clarifications_legacy_0006", "pending_clarifications")


def _indexes(table: str, columns: list[str]) -> None:
    for column in columns:
        op.create_index(f"ix_{table}_{column}", table, [column], unique=column == "public_id")
