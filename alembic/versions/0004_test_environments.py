"""Add encrypted E2E test environment storage.

Revision ID: 0004_test_environments
Revises: 0003_normalize_job_type
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_test_environments"
down_revision: Union[str, None] = "0003_normalize_job_type"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "test_environments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("storage_state", sa.Text(), nullable=False),
        sa.Column("proxy_config", sa.Text(), nullable=True),
        sa.Column("target_domain", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_test_environments_name", "test_environments", ["name"], unique=True)
    op.create_index("ix_test_environments_target_domain", "test_environments", ["target_domain"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_test_environments_target_domain", table_name="test_environments")
    op.drop_index("ix_test_environments_name", table_name="test_environments")
    op.drop_table("test_environments")
