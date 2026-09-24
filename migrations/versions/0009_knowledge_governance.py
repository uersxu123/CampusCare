"""Add canonical knowledge ownership and versioned index registry."""

from alembic import op
import sqlalchemy as sa


revision = "0009_knowledge_governance"
down_revision = "0008_response_completion"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("knowledge_documents") as batch:
        batch.add_column(sa.Column("canonical_key", sa.String(160), nullable=True))
        batch.add_column(sa.Column("managed_by", sa.String(32), nullable=True))
        batch.create_index("ix_knowledge_documents_canonical_key", ["canonical_key"], unique=False)
        batch.create_index("ix_knowledge_documents_managed_by", ["managed_by"], unique=False)
    op.execute(
        sa.text(
            "UPDATE knowledge_documents SET canonical_key = source_key, managed_by = 'LEGACY' "
            "WHERE canonical_key IS NULL OR managed_by IS NULL"
        )
    )

    op.create_table(
        "knowledge_index_registry",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("logical_name", sa.String(64), nullable=False, unique=True),
        sa.Column("active_collection", sa.String(160), nullable=True),
        sa.Column("previous_collection", sa.String(160), nullable=True),
        sa.Column("ready_collection", sa.String(160), nullable=True),
        sa.Column("active_signature", sa.String(64), nullable=True),
        sa.Column("ready_signature", sa.String(64), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("knowledge_index_registry")
    with op.batch_alter_table("knowledge_documents") as batch:
        batch.drop_index("ix_knowledge_documents_managed_by")
        batch.drop_index("ix_knowledge_documents_canonical_key")
        batch.drop_column("managed_by")
        batch.drop_column("canonical_key")
