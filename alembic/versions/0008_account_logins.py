"""Encrypted, opt-in login queue. No credentials in Job.payload or public state."""
from alembic import op
import sqlalchemy as sa

revision = "0008_account_logins"
down_revision = "0007_automation_plans"
branch_labels = depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("account_logins"):
        expected = {"id", "batch_id", "position", "display_name", "identity_key", "login_ciphertext", "password_ciphertext", "status", "message", "account_id", "created_at", "updated_at"}
        if not expected.issubset({c["name"] for c in inspector.get_columns("account_logins")}):
            raise RuntimeError("Existing login queue schema is incompatible")
        return
    op.create_table("account_logins",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("batch_id", sa.String(36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("login_ciphertext", sa.Text(), nullable=True),
        sa.Column("password_ciphertext", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("account_id", sa.String(36), sa.ForeignKey("accounts.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    for name in ("batch_id", "identity_key", "status"):
        op.create_index(f"ix_account_logins_{name}", "account_logins", [name])


def downgrade():
    raise RuntimeError("Preserve login history; restore a verified backup instead")
