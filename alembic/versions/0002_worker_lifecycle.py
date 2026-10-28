"""Add scheduling and ownership fields to background jobs.

Revision ID: 0002_worker_lifecycle
Revises: 0001_initial
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_worker_lifecycle"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("worker_id", sa.String(length=64), nullable=True))
    op.create_index("ix_jobs_next_attempt_at", "jobs", ["next_attempt_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jobs_next_attempt_at", table_name="jobs")
    op.drop_column("jobs", "worker_id")
    op.drop_column("jobs", "finished_at")
    op.drop_column("jobs", "started_at")
    op.drop_column("jobs", "next_attempt_at")
