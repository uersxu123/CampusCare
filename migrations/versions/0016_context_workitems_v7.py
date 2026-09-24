"""Add on-demand tool evidence and context manifest tables."""
from alembic import op
import sqlalchemy as sa

revision = "0016_context_workitems_v7"
down_revision = "0015_three_layer_memory_v3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if {"tool_execution_records", "tool_result_views", "model_context_manifests"}.issubset(existing):
        return
    op.create_table("tool_execution_records",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("execution_id", sa.String(96), nullable=False),
        sa.Column("user_id", sa.String(96), nullable=False, server_default=""), sa.Column("session_id", sa.String(96), nullable=False, server_default=""),
        sa.Column("turn_id", sa.String(96), nullable=False, server_default=""), sa.Column("work_item_id", sa.String(96), nullable=False, server_default=""),
        sa.Column("agent_run_id", sa.String(96), nullable=False, server_default=""), sa.Column("agent_name", sa.String(96), nullable=False, server_default=""),
        sa.Column("tool_call_id", sa.String(128), nullable=False, server_default=""), sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tool_name", sa.String(160), nullable=False, server_default=""), sa.Column("tool_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("arguments_json", sa.Text(), nullable=False), sa.Column("wire_result_json", sa.Text(), nullable=False), sa.Column("normalized_result_json", sa.Text(), nullable=False),
        sa.Column("raw_hash", sa.String(80), nullable=False), sa.Column("payload_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("observed_at", sa.DateTime(), nullable=False), sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("corpus_signature", sa.String(128), nullable=False, server_default=""), sa.Column("index_signature", sa.String(128), nullable=False, server_default=""),
        sa.Column("transport_status", sa.String(32), nullable=False, server_default="OK"), sa.Column("business_status", sa.String(32), nullable=False, server_default="OK"),
        sa.Column("quality_status", sa.String(32), nullable=False, server_default=""), sa.Column("error_code", sa.String(64), nullable=False, server_default=""),
        sa.Column("cached_from_execution_id", sa.String(96), nullable=False, server_default=""), sa.Column("persist_reason", sa.String(64), nullable=False, server_default=""),
        sa.UniqueConstraint("execution_id", name="uq_tool_execution_id"))
    op.create_index("ix_tool_execution_scope", "tool_execution_records", ["user_id", "session_id", "execution_id"])
    op.create_index("ix_tool_execution_raw_hash", "tool_execution_records", ["raw_hash"])
    op.create_table("tool_result_views", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("view_id", sa.String(96), nullable=False, unique=True),
        sa.Column("execution_id", sa.String(96), nullable=False), sa.Column("raw_hash", sa.String(80), nullable=False), sa.Column("goal_hash", sa.String(80), nullable=False, server_default=""),
        sa.Column("audience", sa.String(64), nullable=False, server_default=""), sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("summary_prompt_version", sa.String(64), nullable=False, server_default=""), sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("view_json", sa.Text(), nullable=False), sa.Column("quality_status", sa.String(32), nullable=False, server_default=""),
        sa.Column("token_estimate", sa.Integer(), nullable=False, server_default="0"), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_table("model_context_manifests", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("request_id", sa.String(96), nullable=False, unique=True),
        sa.Column("turn_id", sa.String(96), nullable=False, server_default=""), sa.Column("work_item_id", sa.String(96), nullable=False, server_default=""),
        sa.Column("agent_run_id", sa.String(96), nullable=False, server_default=""), sa.Column("model_round", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("manifest_json", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))


def downgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for name in ("model_context_manifests", "tool_result_views", "tool_execution_records"):
        if name in existing:
            op.drop_table(name)
