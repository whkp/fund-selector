"""Create the initial persistent Fund Compass schema."""

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
