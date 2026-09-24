"""Add Knowledge Ingestion V2 governance and traceability structures."""

from alembic import op
import sqlalchemy as sa


revision = "0012_knowledge_ingestion_v2"
down_revision = "0011_clarification_retry"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("knowledge_documents") as batch:
        batch.add_column(sa.Column("ingestion_status", sa.String(length=32), nullable=False, server_default="READY"))
        batch.add_column(sa.Column("parser_profile", sa.String(length=64), nullable=False, server_default="legacy"))
        batch.add_column(sa.Column("chunking_profile", sa.String(length=64), nullable=False, server_default="legacy_char_v1"))
        batch.add_column(sa.Column("parser_version", sa.String(length=64), nullable=False, server_default="legacy"))
        batch.add_column(sa.Column("last_ingestion_error", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("ingestion_warnings_json", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("ingestion_quality_json", sa.Text(), nullable=False, server_default="{}"))
        batch.add_column(sa.Column("active_revision", sa.Integer(), nullable=False, server_default="1"))
        batch.create_index("ix_knowledge_documents_ingestion_status", ["ingestion_status"])

    op.create_table(
        "knowledge_artifacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("knowledge_documents.id"), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("original_filename", sa.String(length=256), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_artifacts_document_id", "knowledge_artifacts", ["document_id"])
    op.create_index("ix_knowledge_artifacts_storage_key", "knowledge_artifacts", ["storage_key"])
    op.create_index("ix_knowledge_artifacts_sha256", "knowledge_artifacts", ["sha256"])

    op.create_table(
        "knowledge_elements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("knowledge_documents.id"), nullable=False),
        sa.Column("artifact_id", sa.Integer(), sa.ForeignKey("knowledge_artifacts.id"), nullable=True),
        sa.Column("element_index", sa.Integer(), nullable=False),
        sa.Column("element_type", sa.String(length=32), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("bbox_json", sa.Text(), nullable=False, server_default="null"),
        sa.Column("heading_path_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("parent_element_id", sa.Integer(), sa.ForeignKey("knowledge_elements.id"), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("parser_name", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.UniqueConstraint("document_id", "element_index", name="uq_knowledge_element_document_index"),
    )
    op.create_index("ix_knowledge_elements_document_id", "knowledge_elements", ["document_id"])
    op.create_index("ix_knowledge_elements_artifact_id", "knowledge_elements", ["artifact_id"])
    op.create_index("ix_knowledge_elements_element_type", "knowledge_elements", ["element_type"])
    op.create_index("ix_knowledge_elements_content_hash", "knowledge_elements", ["content_hash"])

    op.create_table(
        "knowledge_tables",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("knowledge_documents.id"), nullable=False),
        sa.Column("element_id", sa.Integer(), sa.ForeignKey("knowledge_elements.id"), nullable=False, unique=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("caption", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("headers_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("rows_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("table_html", sa.Text(), nullable=False, server_default=""),
        sa.Column("schema_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
    )
    op.create_index("ix_knowledge_tables_document_id", "knowledge_tables", ["document_id"])
    op.create_index("ix_knowledge_tables_element_id", "knowledge_tables", ["element_id"], unique=True)
    op.create_index("ix_knowledge_tables_content_hash", "knowledge_tables", ["content_hash"])

    op.create_table(
        "knowledge_ingestion_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("knowledge_documents.id"), nullable=True),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.Column("request_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("error_code", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("error_message", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("run_after", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_knowledge_ingestion_jobs_document_id", "knowledge_ingestion_jobs", ["document_id"])
    op.create_index("ix_knowledge_ingestion_jobs_job_type", "knowledge_ingestion_jobs", ["job_type"])
    op.create_index("ix_knowledge_ingestion_jobs_status", "knowledge_ingestion_jobs", ["status"])
    op.create_index("ix_knowledge_ingestion_jobs_run_after", "knowledge_ingestion_jobs", ["run_after"])

    with op.batch_alter_table("knowledge_chunks") as batch:
        batch.add_column(sa.Column("chunk_kind", sa.String(length=32), nullable=False, server_default="TEXT_CHILD"))
        batch.add_column(sa.Column("parent_chunk_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("heading_path_json", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("element_ids_json", sa.Text(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("token_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("chunking_profile", sa.String(length=64), nullable=False, server_default="legacy_char_v1"))
        batch.create_foreign_key("fk_knowledge_chunks_parent", "knowledge_chunks", ["parent_chunk_id"], ["id"])
        batch.create_index("ix_knowledge_chunks_chunk_kind", ["chunk_kind"])
        batch.create_index("ix_knowledge_chunks_parent_chunk_id", ["parent_chunk_id"])


def downgrade():
    op.drop_table("knowledge_ingestion_jobs")

    with op.batch_alter_table("knowledge_chunks") as batch:
        batch.drop_index("ix_knowledge_chunks_parent_chunk_id")
        batch.drop_index("ix_knowledge_chunks_chunk_kind")
        batch.drop_constraint("fk_knowledge_chunks_parent", type_="foreignkey")
        batch.drop_column("chunking_profile")
        batch.drop_column("token_count")
        batch.drop_column("element_ids_json")
        batch.drop_column("heading_path_json")
        batch.drop_column("parent_chunk_id")
        batch.drop_column("chunk_kind")

    op.drop_table("knowledge_tables")
    op.drop_table("knowledge_elements")
    op.drop_table("knowledge_artifacts")

    with op.batch_alter_table("knowledge_documents") as batch:
        batch.drop_index("ix_knowledge_documents_ingestion_status")
        batch.drop_column("active_revision")
        batch.drop_column("last_ingestion_error")
        batch.drop_column("ingestion_quality_json")
        batch.drop_column("ingestion_warnings_json")
        batch.drop_column("parser_version")
        batch.drop_column("chunking_profile")
        batch.drop_column("parser_profile")
        batch.drop_column("ingestion_status")
