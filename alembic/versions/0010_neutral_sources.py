"""Archive legacy content and reserve each source for at most one clip."""
from alembic import op
import sqlalchemy as sa
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs
import hashlib

revision = '0010_neutral_sources'
down_revision = '0009_tiktok_web_receipts'
branch_labels = depends_on = None


def key(url, sid):
    p = urlsplit(url); host = (p.hostname or '').lower(); parts = p.path.strip('/').split('/')
    vid = None
    if host in {'youtu.be', 'www.youtu.be'}:
        vid = parts[0]
    elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtube-nocookie.com', 'www.youtube-nocookie.com'}:
        vid = parse_qs(p.query).get('v', [None])[0]
        if not vid and len(parts) > 1 and parts[0] in {'shorts', 'embed', 'live'}:
            vid = parts[1]
    return 'youtube:' + vid if vid else 'upload:' + sid if url in {'local-upload', 'local-youtube-upload', 'local-tiktok-upload'} else 'url:' + hashlib.sha256(url.encode()).hexdigest()


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name in ('source_videos', 'jobs'):
        if 'archived' not in {c['name'] for c in inspector.get_columns(name)}:
            op.add_column(name, sa.Column('archived', sa.Boolean(), nullable=False, server_default=sa.false()))
        if 'ix_' + name + '_archived' not in {i['name'] for i in inspector.get_indexes(name)}:
            op.create_index('ix_' + name + '_archived', name, ['archived'])
    if inspector.has_table('source_clip_uses'):
        table = sa.Table('source_clip_uses', sa.MetaData(), autoload_with=bind)
    else:
        table = create_uses()
    seen = set(bind.execute(sa.text('SELECT source_key FROM source_clip_uses')).scalars())
    rows = bind.execute(sa.text('SELECT c.id, c.source_id, s.source_url FROM clips c JOIN source_videos s ON s.id=c.source_id ORDER BY c.created_at, c.id')).fetchall()
    for cid, sid, url in rows:
        identity = key(url, sid)
        if identity not in seen:
            bind.execute(table.insert().values(source_key=identity, source_id=sid, job_id=None, clip_id=cid, created_at=datetime.now(timezone.utc)))
            seen.add(identity)


def create_uses():
    return op.create_table('source_clip_uses',
        sa.Column('source_key', sa.String(128), primary_key=True),
        sa.Column('source_id', sa.String(36), sa.ForeignKey('source_videos.id'), nullable=False),
        sa.Column('job_id', sa.String(36), sa.ForeignKey('jobs.id'), nullable=True),
        sa.Column('clip_id', sa.String(36), sa.ForeignKey('clips.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    raise RuntimeError('Preserve source reservations and history; restore a verified backup instead')
