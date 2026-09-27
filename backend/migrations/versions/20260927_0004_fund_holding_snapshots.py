"""Add compressed fund holding snapshots for portfolio overlap checks."""

import sqlalchemy as sa
from alembic import op

revision = "20260927_0004"
down_revision = "20260926_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "fund_holding_snapshots" in set(inspector.get_table_names()):
        return
    op.create_table(
        "fund_holding_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("fund_code", sa.String(length=16), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=True),
        sa.Column("payload_gz", sa.LargeBinary(), nullable=False),
        sa.Column("content_hash", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=80), nullable=False),
        sa.Column("trust_level", sa.String(length=30), nullable=False),
        sa.Column("parser_version", sa.String(length=80), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_fund_holding_snapshots_fund_code", "fund_holding_snapshots", ["fund_code"])
    op.create_index("ix_fund_holding_snapshots_fetched_at", "fund_holding_snapshots", ["fetched_at"])
    op.create_index("ix_fund_holding_snapshots_content_hash", "fund_holding_snapshots", ["content_hash"])


def downgrade() -> None:
    bind = op.get_bind()
    if "fund_holding_snapshots" not in set(sa.inspect(bind).get_table_names()):
        return
    op.drop_index("ix_fund_holding_snapshots_content_hash", table_name="fund_holding_snapshots")
    op.drop_index("ix_fund_holding_snapshots_fetched_at", table_name="fund_holding_snapshots")
    op.drop_index("ix_fund_holding_snapshots_fund_code", table_name="fund_holding_snapshots")
    op.drop_table("fund_holding_snapshots")
