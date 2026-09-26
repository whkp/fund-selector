"""Add compressed payload column to raw_data_snapshots.

全量基金目录快照（约 2 万只）的明文 JSON 有 ~15 MB：跨洋读一次要十几分钟，
「启动从数据库装载目录」根本不可用。JSON 结构高度重复，gzip 实测压缩 30 倍
以上（~15 MB → ~600 KB）。新快照写压缩列 `payload_gz`；`payload_text` 保留
给其他快照类型（单只基金历史净值等小数据）。

幂等：全新库由 0001 的 create_all 直接带上该列；这里只为旧库补列。
"""

import sqlalchemy as sa
from alembic import op

revision = "20260926_0003"
down_revision = "20260912_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "raw_data_snapshots" not in set(inspector.get_table_names()):
        return
    existing = {column["name"] for column in inspector.get_columns("raw_data_snapshots")}
    if "payload_gz" not in existing:
        op.add_column(
            "raw_data_snapshots",
            sa.Column("payload_gz", sa.LargeBinary(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "raw_data_snapshots" not in set(inspector.get_table_names()):
        return
    existing = {column["name"] for column in inspector.get_columns("raw_data_snapshots")}
    if "payload_gz" in existing:
        op.drop_column("raw_data_snapshots", "payload_gz")
