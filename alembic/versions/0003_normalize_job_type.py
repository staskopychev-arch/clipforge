"""Normalize the job type column created by early local SQLite builds.

Revision ID: 0003_normalize_job_type
Revises: 0002_worker_lifecycle
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_normalize_job_type"
down_revision: Union[str, None] = "0002_worker_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.alter_column(
            "type",
            existing_type=sa.String(length=8),
            type_=sa.String(length=32),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.alter_column(
            "type",
            existing_type=sa.String(length=32),
            type_=sa.String(length=8),
            existing_nullable=False,
        )
