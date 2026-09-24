"""Bridge the legacy migration head to the current migration chain.

Existing deployments may already be stamped at ``20260727_07``. Fresh
databases reach this revision through the current schema baseline, while
legacy databases can continue directly with the additive migrations.
"""


revision = "20260727_07"
down_revision = "0001_current_schema_baseline"
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
