"""Add user credentials and conversation tables.

`20260909_0001` 用 `Base.metadata.create_all` 建表，因此全新数据库在跑完
0001 之后就已经带有新版的 `users` 表和对话表 —— 本迁移对那种情况是幂等的。
它真正要处理的是**已经用旧 schema 建过 `users` 表**的库：那里只有
`subject`/`role`/`created_at`，需要补上登录相关列。
"""

from contextlib import suppress

import sqlalchemy as sa
from alembic import op

revision = "20260912_0002"
down_revision = "20260909_0001"
branch_labels = None
depends_on = None

USER_COLUMNS: dict[str, sa.types.TypeEngine] = {
    "email": sa.String(254),
    "display_name": sa.String(80),
    "password_hash": sa.String(255),
    "is_active": sa.Boolean(),
    "last_login_at": sa.DateTime(timezone=True),
    "updated_at": sa.DateTime(timezone=True),
}


def upgrade() -> None:
    from app.db.base import Base

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "users" in set(inspector.get_table_names()):
        existing = {column["name"] for column in inspector.get_columns("users")}
        for name, column_type in USER_COLUMNS.items():
            if name not in existing:
                op.add_column("users", sa.Column(name, column_type, nullable=True))
        op.execute("UPDATE users SET email = subject WHERE email IS NULL OR email = ''")
        op.execute("UPDATE users SET display_name = email WHERE display_name IS NULL")
        # 不能写 `= 1`：Postgres 的 boolean 严格区分类型，直接报
        # DatatypeMismatchError。TRUE 关键字两边方言都认（SQLite 3.23+，2018 年）。
        op.execute("UPDATE users SET is_active = TRUE WHERE is_active IS NULL")

    # 建出对话表（以及任何尚不存在的表）。已存在的表会被跳过。
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table in ("conversation_messages", "conversations"):
        if table in tables:
            op.drop_table(table)
    if "users" in tables:
        existing = {column["name"] for column in inspector.get_columns("users")}
        for name in USER_COLUMNS:
            if name in existing:
                with suppress(Exception):
                    # 老 SQLite 不支持 DROP COLUMN；能删就删，删不掉也不影响新代码读取。
                    op.drop_column("users", name)
