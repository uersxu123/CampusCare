"""Allow complete large tool results in MySQL without TEXT's 64KB limit."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import LONGTEXT

revision = "0017_tool_result_longtext"
down_revision = "0016_context_workitems_v7"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "mysql":
        for field in ("wire_result_json", "normalized_result_json"):
            op.alter_column("tool_execution_records", field, existing_type=sa.Text(),
                            type_=LONGTEXT(), existing_nullable=False)


def downgrade():
    # 保留宽字段，避免回滚迁移截断已保存的原文。
    pass
