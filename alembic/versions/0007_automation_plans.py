"""Persistent opt-in worker schedule; disabled until explicitly configured."""
from alembic import op
import sqlalchemy as sa

revision = "0007_automation_plans"
down_revision = "0006_youtube_publications"
branch_labels = depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("automation_plans"):
        expected = {"id", "enabled", "config", "next_run_at", "last_pipeline_id", "status", "last_error", "updated_at"}
        if not expected.issubset({column["name"] for column in inspector.get_columns("automation_plans")}):
            raise RuntimeError("Existing automation plan schema is incompatible; preserve and inspect it")
        return
    op.create_table("automation_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_pipeline_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    raise RuntimeError("Preserve schedule history; restore a verified backup instead")
