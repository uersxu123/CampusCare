"""Current project schema baseline before memory and knowledge metadata."""
from alembic import op
import sqlalchemy as sa


revision = "0001_current_schema_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("user_accounts", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("username", sa.String(64), nullable=False), sa.Column("display_name", sa.String(128), nullable=False), sa.Column("password_hash", sa.String(128), nullable=False), sa.Column("roles_csv", sa.String(256), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.UniqueConstraint("username"))
    op.create_index("ix_user_accounts_username", "user_accounts", ["username"], unique=True)
    op.create_table("chat_sessions", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("public_id", sa.String(64), nullable=False), sa.Column("title", sa.String(160), nullable=False), sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.UniqueConstraint("public_id"))
    op.create_index("ix_chat_sessions_public_id", "chat_sessions", ["public_id"], unique=True)
    op.create_table("chat_messages", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False), sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=False), sa.Column("role", sa.String(32), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_table("knowledge_chunks", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("source", sa.String(256), nullable=False), sa.Column("source_index", sa.Integer(), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("embedding_json", sa.Text(), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_knowledge_chunks_source", "knowledge_chunks", ["source"])
    op.create_table("psychological_reports", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False), sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("intent", sa.String(32), nullable=False), sa.Column("emotion", sa.String(32), nullable=False), sa.Column("emotion_score", sa.Float(), nullable=False), sa.Column("risk_level", sa.String(32), nullable=False), sa.Column("confidence", sa.Float(), nullable=False), sa.Column("summary", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_table("risk_cases", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("report_id", sa.Integer(), nullable=False), sa.Column("risk_level", sa.String(32), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("owner", sa.String(128), nullable=False), sa.Column("summary", sa.Text(), nullable=False), sa.Column("handoff_summary", sa.Text(), nullable=False), sa.Column("acknowledged_by", sa.String(128), nullable=True), sa.Column("acknowledged_at", sa.DateTime(), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.UniqueConstraint("report_id"))
    op.create_index("ix_risk_cases_report_id", "risk_cases", ["report_id"], unique=True)
    op.create_index("ix_risk_cases_risk_level", "risk_cases", ["risk_level"])
    op.create_index("ix_risk_cases_status", "risk_cases", ["status"])
    op.create_table("case_notes", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("case_id", sa.Integer(), nullable=False), sa.Column("actor", sa.String(128), nullable=False), sa.Column("note", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_case_notes_case_id", "case_notes", ["case_id"])
    op.create_table("alert_records", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("report_id", sa.Integer(), nullable=False), sa.Column("channel", sa.String(64), nullable=False), sa.Column("recipient", sa.String(256), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("message", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_alert_records_report_id", "alert_records", ["report_id"])
    op.create_table("excel_records", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("report_id", sa.Integer(), nullable=False), sa.Column("file_path", sa.String(512), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("message", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    op.create_index("ix_excel_records_report_id", "excel_records", ["report_id"])
    op.create_table("tool_jobs", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("report_id", sa.Integer(), nullable=False), sa.Column("kind", sa.String(64), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("max_attempts", sa.Integer(), nullable=False), sa.Column("depends_on_job_id", sa.Integer(), nullable=True), sa.Column("run_after", sa.DateTime(), nullable=False), sa.Column("last_error", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False))
    for name in ("report_id", "kind", "status", "depends_on_job_id", "run_after"):
        op.create_index(f"ix_tool_jobs_{name}", "tool_jobs", [name])
    op.create_table("dead_letter_records", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("job_id", sa.Integer(), nullable=True), sa.Column("report_id", sa.Integer(), nullable=False), sa.Column("kind", sa.String(64), nullable=False), sa.Column("reason", sa.Text(), nullable=False), sa.Column("payload", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    for name in ("job_id", "report_id", "kind"):
        op.create_index(f"ix_dead_letter_records_{name}", "dead_letter_records", [name])
    op.create_table("agent_run_traces", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("user_id", sa.Integer(), sa.ForeignKey("user_accounts.id"), nullable=False), sa.Column("session_id", sa.Integer(), sa.ForeignKey("chat_sessions.id"), nullable=False), sa.Column("report_id", sa.Integer(), nullable=True), sa.Column("intent", sa.String(32), nullable=False), sa.Column("risk_level", sa.String(32), nullable=False), sa.Column("original_input", sa.Text(), nullable=False), sa.Column("sanitized_input", sa.Text(), nullable=False), sa.Column("memory_brief", sa.Text(), nullable=False), sa.Column("agent_steps_json", sa.Text(), nullable=False), sa.Column("retrieved_knowledge_json", sa.Text(), nullable=False), sa.Column("response_messages_json", sa.Text(), nullable=False), sa.Column("assessment_json", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False))
    for name in ("user_id", "session_id", "report_id", "intent", "risk_level"):
        op.create_index(f"ix_agent_run_traces_{name}", "agent_run_traces", [name])
    op.create_table("tool_audit_records", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("job_id", sa.Integer(), nullable=True), sa.Column("report_id", sa.Integer(), nullable=True), sa.Column("tool_name", sa.String(64), nullable=False), sa.Column("policy", sa.String(128), nullable=False), sa.Column("allowed", sa.Boolean(), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("reason", sa.Text(), nullable=False), sa.Column("payload", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False))
    for name in ("job_id", "report_id", "tool_name", "status"):
        op.create_index(f"ix_tool_audit_records_{name}", "tool_audit_records", [name])


def downgrade():
    for table in ("tool_audit_records", "agent_run_traces", "dead_letter_records", "tool_jobs", "excel_records", "alert_records", "case_notes", "risk_cases", "psychological_reports", "knowledge_chunks", "chat_messages", "chat_sessions", "user_accounts"):
        op.drop_table(table)
