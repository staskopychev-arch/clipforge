"""Independent hourly account view counters and thirty-day history."""
from alembic import op
import sqlalchemy as sa
revision='0011_account_views'
down_revision='0010_neutral_sources'
branch_labels=depends_on=None

def upgrade():
    tables=set(sa.inspect(op.get_bind()).get_table_names())
    if 'account_views' not in tables:create_views()
    if 'account_view_samples' not in tables:create_samples()

def create_views():
    op.create_table('account_views',
        sa.Column('account_id',sa.String(36),sa.ForeignKey('accounts.id'),primary_key=True),
        sa.Column('views',sa.BigInteger(),nullable=True),sa.Column('video_count',sa.Integer(),nullable=True),
        sa.Column('approximate',sa.Boolean(),nullable=False),sa.Column('complete',sa.Boolean(),nullable=False),
        sa.Column('profile_url',sa.Text(),nullable=True),
        *[sa.Column(n,sa.DateTime(timezone=True),nullable=True) for n in ('checked_at','attempted_at','next_check_at','lease_until')],
        sa.Column('lease_token',sa.String(36),nullable=True),sa.Column('session_hash',sa.String(64),nullable=True),
        sa.Column('status',sa.String(32),nullable=False),sa.Column('error',sa.String(500),nullable=True))
    op.create_index('ix_account_views_next_check_at','account_views',['next_check_at'])

def create_samples():
    op.create_table('account_view_samples',
        sa.Column('id',sa.Integer(),primary_key=True,autoincrement=True),
        sa.Column('account_id',sa.String(36),sa.ForeignKey('accounts.id'),nullable=False),
        sa.Column('views',sa.BigInteger(),nullable=False),sa.Column('video_count',sa.Integer(),nullable=False),
        sa.Column('approximate',sa.Boolean(),nullable=False),sa.Column('complete',sa.Boolean(),nullable=False),
        sa.Column('profile_url',sa.Text(),nullable=True),
        sa.Column('checked_at',sa.DateTime(timezone=True),nullable=False))
    op.create_index('ix_account_view_samples_account_id','account_view_samples',['account_id'])
    op.create_index('ix_account_view_samples_checked_at','account_view_samples',['checked_at'])

def downgrade():
    op.drop_table('account_view_samples')
    op.drop_table('account_views')
