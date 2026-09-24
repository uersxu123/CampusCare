"""Map legacy chunks into target document entities idempotently."""
import hashlib
from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa


revision = "0004_migrate_legacy_knowledge"
down_revision = "0003_knowledge_metadata"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    meta = sa.MetaData()
    chunks = sa.Table("knowledge_chunks", meta, autoload_with=bind)
    documents = sa.Table("knowledge_documents", meta, autoload_with=bind)
    sources = bind.execute(sa.select(chunks.c.source).where(chunks.c.document_id.is_(None)).distinct()).scalars().all()
    for source in sources:
        rows = bind.execute(sa.select(chunks.c.id, chunks.c.content).where(chunks.c.source == source).order_by(chunks.c.source_index)).all()
        source_key = "legacy-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]
        content_hash = hashlib.sha256("\n".join(row.content for row in rows).encode("utf-8")).hexdigest()
        document_id = bind.execute(sa.select(documents.c.id).where(documents.c.source_key == source_key)).scalar_one_or_none()
        if document_id is None:
            result = bind.execute(
                documents.insert().values(
                    source_key=source_key,
                    title=source,
                    source_url=None,
                    source_type="INTERNAL_GUIDANCE",
                    domain="SAFETY" if source in {"risk-policy.md", "privacy-boundaries-and-ethics.md"} else "MENTAL_HEALTH",
                    tags_json="[]",
                    site="ALL",
                    status="ACTIVE",
                    verified_at=datetime.now(UTC).replace(tzinfo=None),
                    expires_at=None,
                    version="legacy-1",
                    content_hash=content_hash,
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                    updated_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            document_id = result.inserted_primary_key[0]
        for row in rows:
            bind.execute(
                chunks.update().where(chunks.c.id == row.id).values(
                    document_id=document_id,
                    content_hash=hashlib.sha256(row.content.encode("utf-8")).hexdigest(),
                )
            )


def downgrade():
    bind = op.get_bind()
    meta = sa.MetaData()
    chunks = sa.Table("knowledge_chunks", meta, autoload_with=bind)
    documents = sa.Table("knowledge_documents", meta, autoload_with=bind)
    legacy_ids = sa.select(documents.c.id).where(documents.c.source_key.like("legacy-%"))
    bind.execute(chunks.update().where(chunks.c.document_id.in_(legacy_ids)).values(document_id=None, content_hash=""))
    bind.execute(documents.delete().where(documents.c.source_key.like("legacy-%")))
