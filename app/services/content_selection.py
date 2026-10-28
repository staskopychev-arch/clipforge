"""Select one unused source; never create a second clip from the same video."""
from pathlib import Path
from sqlalchemy import select
from ..models import SourceVideo, RecordStatus
from .source_identity import existing_clip


def next_unique_start(db, source, duration):
    if source.archived or (source.duration_seconds or 0) < duration:
        return None
    return None if existing_clip(db, source.source_url, source.id) else 0


def cached_unique_excerpt(db, duration, topic='any', source_ids=None):
    sources = db.scalars(select(SourceVideo).where(SourceVideo.archived.is_(False),
        SourceVideo.status == RecordStatus.ready).order_by(SourceVideo.created_at)).all()
    for source in sources:
        if source_ids is not None and source.id not in source_ids:
            continue
        if not source.rights_confirmed or not source.local_path or not Path(source.local_path).is_file():
            continue
        if next_unique_start(db, source, duration) is not None:
            return source, 0
    return None
