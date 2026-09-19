"""Create the initial persistent Fund Compass schema.

⚠️ 已知缺陷：本迁移用 `Base.metadata.create_all` 而不是显式 DDL，所以它的内容会
**随模型代码一起变化** —— 同一个 revision 在今天和明年跑出来的表结构可能不同，
`downgrade` 里的 `drop_all` 还会连带影响后续迁移建的表。迁移脚本本该是冻结的历史
快照，这一版不是。

改写成冻结 DDL 需要把 15 张表逐列手写一遍，收益（新环境初始化可复现）远小于
风险（写错就建不出库）；而且所有现有环境的库都已经跑过这一版，改不改都不影响它们。
记在这里，是为了避免以后有人当它可靠。
"""

from alembic import op

revision = "20260909_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    from app.db.base import Base

    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    from app.db.base import Base

    Base.metadata.drop_all(bind=op.get_bind())
