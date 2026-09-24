"""Add verified response completion and generation trace fields."""

from alembic import op
import sqlalchemy as sa


revision = "0008_response_completion"
down_revision = "0007_context_memory_v2"
branch_labels = None
depends_on = None


LEGACY_GENERATION = (
    '{"schemaVersion":1,"legacy":true,"completionVerified":false,'
    '"finishReason":"LEGACY_UNKNOWN"}'
)
LEGACY_RETRIEVAL = '{"schemaVersion":1,"legacy":true}'


def upgrade():
    with op.batch_alter_table("chat_turns") as batch:
        batch.add_column(sa.Column("finish_reason", sa.String(32), nullable=True))
        batch.add_column(sa.Column("completion_verified", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("generation_metadata_json", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE chat_turns SET finish_reason = CASE WHEN status = 'COMPLETED' "
            "THEN 'LEGACY_UNKNOWN' ELSE NULL END, completion_verified = :verified, "
            "generation_metadata_json = :metadata"
        ).bindparams(verified=False, metadata=LEGACY_GENERATION)
    )
    with op.batch_alter_table("chat_turns") as batch:
        batch.alter_column("completion_verified", existing_type=sa.Boolean(), nullable=False)
        batch.alter_column("generation_metadata_json", existing_type=sa.Text(), nullable=False)

    with op.batch_alter_table("agent_run_traces") as batch:
        batch.add_column(sa.Column("generation_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("retrieval_diagnostics_json", sa.Text(), nullable=True))
        batch.add_column(sa.Column("finalized_at", sa.DateTime(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE agent_run_traces SET generation_json = :generation, "
            "retrieval_diagnostics_json = :retrieval, finalized_at = CURRENT_TIMESTAMP"
        ).bindparams(generation=LEGACY_GENERATION, retrieval=LEGACY_RETRIEVAL)
    )
    with op.batch_alter_table("agent_run_traces") as batch:
        batch.alter_column("generation_json", existing_type=sa.Text(), nullable=False)
        batch.alter_column("retrieval_diagnostics_json", existing_type=sa.Text(), nullable=False)


def downgrade():
    with op.batch_alter_table("agent_run_traces") as batch:
        batch.drop_column("finalized_at")
        batch.drop_column("retrieval_diagnostics_json")
        batch.drop_column("generation_json")
    with op.batch_alter_table("chat_turns") as batch:
        batch.drop_column("generation_metadata_json")
        batch.drop_column("completion_verified")
        batch.drop_column("finish_reason")
