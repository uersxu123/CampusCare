"""Add target knowledge document metadata and extend chunks."""
from alembic import op
import sqlalchemy as sa


revision = "0003_knowledge_metadata"
down_revision = "0002_conversation_summaries"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "knowledge_documents" in table_names:
        document_columns = {column["name"]: column for column in inspector.get_columns("knowledge_documents")}
        document_indexes = {index["name"] for index in inspector.get_indexes("knowledge_documents")}
        with op.batch_alter_table("knowledge_documents") as batch:
            if "source_type" not in document_columns:
                batch.add_column(sa.Column("source_type", sa.String(64), nullable=True))
            if "verified_at" not in document_columns:
                batch.add_column(sa.Column("verified_at", sa.DateTime(), nullable=True))
            if "source_url" in document_columns and not document_columns["source_url"]["nullable"]:
                batch.alter_column("source_url", existing_type=sa.String(1024), nullable=True)
            if "campus" in document_columns and not document_columns["campus"]["nullable"]:
                batch.alter_column("campus", existing_type=sa.String(32), nullable=True)
            if "document_type" in document_columns and not document_columns["document_type"]["nullable"]:
                batch.alter_column("document_type", existing_type=sa.String(64), nullable=True)
        if "document_type" in document_columns:
            bind.execute(sa.text("UPDATE knowledge_documents SET source_type = document_type WHERE source_type IS NULL"))
        bind.execute(sa.text("UPDATE knowledge_documents SET source_type = 'INTERNAL_GUIDANCE' WHERE source_type IS NULL"))
        with op.batch_alter_table("knowledge_documents") as batch:
            batch.alter_column("source_type", existing_type=sa.String(64), nullable=False)
            if "ix_knowledge_documents_source_type" not in document_indexes:
                batch.create_index("ix_knowledge_documents_source_type", ["source_type"])
    else:
        op.create_table(
            "knowledge_documents",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source_key", sa.String(160), nullable=False),
            sa.Column("title", sa.String(256), nullable=False),
            sa.Column("source_url", sa.String(1024), nullable=True),
            sa.Column("source_type", sa.String(64), nullable=False),
            sa.Column("domain", sa.String(64), nullable=False),
            sa.Column("tags_json", sa.Text(), nullable=False),
            sa.Column("site", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("verified_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("version", sa.String(64), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("source_key", name="uq_knowledge_documents_source_key"),
        )
        for name in ("source_key", "source_type", "domain", "site", "status", "expires_at", "content_hash"):
            op.create_index(f"ix_knowledge_documents_{name}", "knowledge_documents", [name], unique=name == "source_key")

    inspector = sa.inspect(bind)
    chunk_columns = {column["name"]: column for column in inspector.get_columns("knowledge_chunks")}
    chunk_indexes = {index["name"] for index in inspector.get_indexes("knowledge_chunks")}
    chunk_uniques = {constraint["name"] for constraint in inspector.get_unique_constraints("knowledge_chunks")}
    with op.batch_alter_table("knowledge_chunks") as batch:
        if "document_id" not in chunk_columns:
            batch.add_column(sa.Column("document_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_knowledge_chunks_document_id", "knowledge_documents", ["document_id"], ["id"])
        if "section_title" not in chunk_columns:
            batch.add_column(sa.Column("section_title", sa.String(256), nullable=True))
        if "page_number" not in chunk_columns:
            batch.add_column(sa.Column("page_number", sa.Integer(), nullable=True))
        if "content_hash" not in chunk_columns:
            batch.add_column(sa.Column("content_hash", sa.String(64), nullable=False, server_default=""))
        if "ix_knowledge_chunks_document_id" not in chunk_indexes:
            batch.create_index("ix_knowledge_chunks_document_id", ["document_id"])
        if "ix_knowledge_chunks_content_hash" not in chunk_indexes:
            batch.create_index("ix_knowledge_chunks_content_hash", ["content_hash"])
        if not any(name in chunk_uniques for name in ("uq_knowledge_chunk_document_index", "uq_knowledge_chunk_document_source_index")):
            batch.create_unique_constraint("uq_knowledge_chunk_document_index", ["document_id", "source_index"])


def downgrade():
    with op.batch_alter_table("knowledge_chunks") as batch:
        batch.drop_constraint("uq_knowledge_chunk_document_index", type_="unique")
        batch.drop_index("ix_knowledge_chunks_content_hash")
        batch.drop_index("ix_knowledge_chunks_document_id")
        batch.drop_constraint("fk_knowledge_chunks_document_id", type_="foreignkey")
        batch.drop_column("content_hash")
        batch.drop_column("page_number")
        batch.drop_column("section_title")
        batch.drop_column("document_id")
    op.drop_table("knowledge_documents")
