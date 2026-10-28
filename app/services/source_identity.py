"""One durable clip per source video, including equivalent YouTube URLs."""
import hashlib
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from sqlalchemy import select
from ..models import Clip, SourceVideo, SourceClipUse, Job, JobType, RecordStatus, utcnow
from ..config import settings


def exhausted_download(job):
    return (job.type == JobType.download and job.status == RecordStatus.error and
        job.attempts >= settings.max_job_attempts and
        ((job.result or {}).get('error_kind') == 'download_failed' or
         (job.error or '').startswith('Ни один загрузчик не выдал корректное видео. Попытки: ')))


class FirstResultError(RuntimeError):
    pass


def source_key(url, source_id=''):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    parts = parsed.path.strip('/').split('/')
    video_id = None
    if host in {'youtu.be', 'www.youtu.be'}:
        video_id = parts[0]
    elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtube-nocookie.com', 'www.youtube-nocookie.com'}:
        video_id = parse_qs(parsed.query).get('v', [None])[0]
        if not video_id and len(parts) > 1 and parts[0] in {'shorts', 'embed', 'live'}:
            video_id = parts[1]
    if video_id:
        return 'youtube:' + video_id
    if url in {'local-upload', 'local-youtube-upload', 'local-tiktok-upload'}:
        return 'upload:' + source_id
    return 'url:' + hashlib.sha256(url.encode()).hexdigest()


def existing_clip(db, url, source_id=''):
    key = source_key(url, source_id)
    reservation = db.get(SourceClipUse, key)
    if reservation:
        return reservation.clip_id or 'reserved'
    for clip, source in db.execute(select(Clip, SourceVideo).join(SourceVideo, Clip.source_id == SourceVideo.id)):
        if source_key(source.source_url, source.id) == key:
            return clip.id
    return None


def unavailable_source_keys(db):
    """Include archived history and in-flight claims across URL variants."""
    keys = set(db.scalars(select(SourceClipUse.source_key)))
    for source in db.scalars(select(SourceVideo).join(Clip, Clip.source_id == SourceVideo.id)):
        keys.add(source_key(source.source_url, source.id))
    pending = db.scalars(select(Job).where(Job.archived.is_(False),
        Job.type.in_([JobType.download, JobType.render]),
        Job.status.in_([RecordStatus.queued, RecordStatus.processing])))
    for job in pending:
        sid = job.payload.get('source_id')
        source = db.get(SourceVideo, sid) if sid else None
        if source:
            keys.add(source_key(source.source_url, source.id))
    # A new explicit search should not immediately choose the same failed URL.
    cutoff = utcnow() - timedelta(minutes=30)
    for job in db.scalars(select(Job).where(Job.archived.is_(False), Job.type == JobType.download,
            Job.status == RecordStatus.error, Job.finished_at >= cutoff)):
        if exhausted_download(job):
            source = db.get(SourceVideo, job.payload.get('source_id'))
            if source:
                keys.add(source_key(source.source_url, source.id))
    return keys


def require_active(source):
    if not source or source.archived:
        raise FirstResultError('Исходник находится в архиве и недоступен для новой обработки')
