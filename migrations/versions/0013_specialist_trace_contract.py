"""Rename trace payloads and migrate legacy report intents."""

from alembic import op
import sqlalchemy as sa


revision = "0013_specialist_trace_contract"
down_revision = "0012_knowledge_ingestion_v2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("agent_run_traces") as batch:
        batch.alter_column(
            "retrieved_knowledge_json",
            existing_type=sa.Text(),
            existing_nullable=False,
            new_column_name="evidence_items_json",
        )
        batch.alter_column(
            "retrieval_diagnostics_json",
            existing_type=sa.Text(),
            existing_nullable=False,
            new_column_name="tool_diagnostics_json",
        )
    op.execute("UPDATE psychological_reports SET intent = 'MENTAL' WHERE intent = 'CONSULT'")


def downgrade():
    op.execute("UPDATE psychological_reports SET intent = 'CONSULT' WHERE intent = 'MENTAL'")
    with op.batch_alter_table("agent_run_traces") as batch:
        batch.alter_column(
            "evidence_items_json",
            existing_type=sa.Text(),
            existing_nullable=False,
            new_column_name="retrieved_knowledge_json",
        )
        batch.alter_column(
            "tool_diagnostics_json",
            existing_type=sa.Text(),
            existing_nullable=False,
            new_column_name="retrieval_diagnostics_json",
        )
