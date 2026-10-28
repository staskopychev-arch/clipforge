"""Durable receipts for explicitly requested TikTok browser uploads."""
from alembic import op
import sqlalchemy as sa

revision = "0009_tiktok_web_receipts"
down_revision = "0008_account_logins"
branch_labels = depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("tiktok_web_publications"):
        expected = {"id", "account_id", "job_id", "file_hash", "stage", "video_url", "created_at"}
        if not expected.issubset({c["name"] for c in inspector.get_columns("tiktok_web_publications")}):
            raise RuntimeError("Existing TikTok receipt schema is incompatible")
        return
    op.create_table("tiktok_web_publications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("video_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_id", "file_hash", name="uq_tiktok_web_account_file"))


def downgrade():
    raise RuntimeError("Preserve publication receipts; restore a verified backup instead")
