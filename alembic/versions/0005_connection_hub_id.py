"""Restore the historical revision referenced by existing local databases."""
from alembic import op
import sqlalchemy as sa

revision = "0005_connection_hub_id"
down_revision = "0004_test_environments"
branch_labels = depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "connection_hub_id" in {c["name"] for c in inspector.get_columns("test_environments")}:
        return
    op.add_column("test_environments", sa.Column("connection_hub_id", sa.String(255), nullable=True))
    op.execute(sa.text("UPDATE test_environments SET connection_hub_id = 'local-' || id"))
    with op.batch_alter_table("test_environments") as batch:
        batch.alter_column("connection_hub_id", existing_type=sa.String(255), nullable=False)
        batch.create_unique_constraint("uq_test_environment_hub", ["connection_hub_id"])


def downgrade():
    raise RuntimeError("Use a verified backup for rollback; identity data is not dropped automatically")
