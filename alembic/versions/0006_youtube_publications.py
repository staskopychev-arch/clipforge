"""Durable YouTube receipts, including previously auto-created SQLite tables."""
from alembic import op
import sqlalchemy as sa

revision = "0006_youtube_publications"
down_revision = "0005_connection_hub_id"
branch_labels = depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("youtube_publications"):
        columns = {c["name"] for c in inspector.get_columns("youtube_publications")}
        expected = {"id", "account_id", "job_id", "file_hash", "stage", "video_url", "created_at"}
        unique = {tuple(c["column_names"]) for c in inspector.get_unique_constraints("youtube_publications")}
        unique.update(tuple(c["column_names"]) for c in inspector.get_indexes("youtube_publications") if c["unique"])
        if not expected.issubset(columns) or ("account_id", "file_hash") not in unique or ("job_id",) not in unique:
            raise RuntimeError("Existing YouTube receipt schema is incompatible; preserve data and repair manually")
        return
    op.create_table("youtube_publications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("video_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id"),
        sa.UniqueConstraint("account_id", "file_hash", name="uq_youtube_account_file"))


def downgrade():
    raise RuntimeError("Publication receipts must not be deleted automatically; restore a verified backup")
