"""Cut pending clarifications over to five-intent route V3."""

import json

import sqlalchemy as sa
from alembic import op


revision = "0014_five_intent_route_v3"
down_revision = "0013_specialist_trace_contract"
branch_labels = None
depends_on = None

OLD_TO_INTENT = {
    # 最早部署版本使用小写任务名，升级时一并兼容。
    "study_plan": "ACADEMIC",
    "knowledge_scope": "CAMPUS",
    "CHAT:GENERAL_CHAT": "CHAT",
    "ACADEMIC:STUDY_PLAN": "ACADEMIC",
    "ACADEMIC:CAREER_DECISION": "ACADEMIC",
    "CAMPUS:INSTITUTIONAL_FACT": "CAMPUS",
    "MENTAL:EMOTIONAL_SUPPORT": "MENTAL",
    "RISK:HIGH_RISK_SUPPORT": "RISK",
}
INTENT_TO_OLD = {
    "CHAT": "CHAT:GENERAL_CHAT",
    "ACADEMIC": "ACADEMIC:STUDY_PLAN",
    "CAMPUS": "CAMPUS:INSTITUTIONAL_FACT",
    "MENTAL": "MENTAL:EMOTIONAL_SUPPORT",
    "RISK": "RISK:HIGH_RISK_SUPPORT",
}


def upgrade():
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, task_kind FROM pending_clarifications ORDER BY id")).fetchall()
    invalid = [(row[0], row[1]) for row in rows if row[1] not in OLD_TO_INTENT]
    if invalid:
        raise RuntimeError(f"pending_clarifications 包含非法 task_kind: {invalid}")
    with op.batch_alter_table("pending_clarifications") as batch:
        batch.add_column(sa.Column("intent", sa.String(length=16), nullable=True))
    for old, intent in OLD_TO_INTENT.items():
        connection.execute(sa.text("UPDATE pending_clarifications SET intent=:intent WHERE task_kind=:old"), {"intent": intent, "old": old})
    bad = connection.execute(sa.text("SELECT id, intent FROM pending_clarifications WHERE intent IS NULL OR intent NOT IN ('CHAT','ACADEMIC','CAMPUS','MENTAL','RISK') ORDER BY id")).fetchall()
    if bad:
        raise RuntimeError(f"pending_clarifications intent 回填失败: {bad}")
    tombstone = json.dumps({"schemaVersion": 3, "cutoverReason": "ROUTE_SCHEMA_V3_CUTOVER"}, ensure_ascii=False, separators=(",", ":"))
    connection.execute(sa.text(
        "UPDATE pending_clarifications SET status='INTERRUPTED', finish_reason='ROUTE_SCHEMA_V3_CUTOVER', "
        "resume_context_json=:resume, known_arguments_json='{}', missing_arguments_json='[]', original_message='', approved_question='' "
        "WHERE status='WAITING_USER'"
    ), {"resume": tombstone})
    inspector = sa.inspect(connection)
    indexes = {item["name"] for item in inspector.get_indexes("pending_clarifications")}
    with op.batch_alter_table("pending_clarifications") as batch:
        if "ix_pending_clarifications_task_kind" in indexes:
            batch.drop_index("ix_pending_clarifications_task_kind")
        batch.alter_column("intent", existing_type=sa.String(length=16), nullable=False)
        batch.create_index("ix_pending_clarifications_intent", ["intent"], unique=False)
        batch.drop_column("task_kind")


def downgrade():
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, intent FROM pending_clarifications ORDER BY id")).fetchall()
    invalid = [(row[0], row[1]) for row in rows if row[1] not in INTENT_TO_OLD]
    if invalid:
        raise RuntimeError(f"pending_clarifications 包含非法 intent: {invalid}")
    with op.batch_alter_table("pending_clarifications") as batch:
        batch.add_column(sa.Column("task_kind", sa.String(length=64), nullable=True))
    for intent, old in INTENT_TO_OLD.items():
        connection.execute(sa.text("UPDATE pending_clarifications SET task_kind=:old WHERE intent=:intent"), {"old": old, "intent": intent})
    tombstone = json.dumps({"schemaVersion": 2, "cutoverReason": "ROUTE_SCHEMA_V3_DOWNGRADE"}, ensure_ascii=False, separators=(",", ":"))
    connection.execute(sa.text(
        "UPDATE pending_clarifications SET status='INTERRUPTED', finish_reason='ROUTE_SCHEMA_V3_DOWNGRADE', "
        "resume_context_json=:resume, known_arguments_json='{}', missing_arguments_json='[]', original_message='', approved_question='' "
        "WHERE status='WAITING_USER'"
    ), {"resume": tombstone})
    with op.batch_alter_table("pending_clarifications") as batch:
        batch.drop_index("ix_pending_clarifications_intent")
        batch.alter_column("task_kind", existing_type=sa.String(length=64), nullable=False)
        batch.create_index("ix_pending_clarifications_task_kind", ["task_kind"], unique=False)
        batch.drop_column("intent")
