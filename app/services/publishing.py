"""Durable YouTube upload queue and duplicate-file reservations."""
import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Account, Clip, Job, JobType, RecordStatus, SourceVideo, YouTubePublication, TikTokWebPublication
from .media import validate_shorts_file


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def enqueue_youtube(db: Session, *, account_id: str, clip: Clip, visibility: str = "private",
                    made_for_kids: bool = False, title: str = "", pipeline_id: str | None = None) -> Job:
    account = db.get(Account, account_id)
    if not account or account.platform != "youtube" or account.status != RecordStatus.ready:
        raise ValueError("Выберите доступный YouTube-аккаунт; при необходимости импортируйте сессию заново")
    if clip.status != RecordStatus.ready or not clip.local_path:
        raise ValueError("Клип ещё не готов")
    if visibility not in {"private", "unlisted", "public"}:
        raise ValueError("Неверная видимость видео")
    source = db.get(SourceVideo, clip.source_id)
    if not source or source.archived or not source.rights_confirmed:
        raise ValueError("Подтвердите право на публикацию исходника")
    validate_shorts_file(Path(clip.local_path))
    digest = file_hash(Path(clip.local_path))
    existing = db.scalar(select(YouTubePublication).where(
        YouTubePublication.account_id == account_id, YouTubePublication.file_hash == digest))
    if existing:
        raise ValueError("Этот файл уже отправлялся в выбранный аккаунт. Проверьте предыдущую задачу в журнале")
    for receipt in db.scalars(select(YouTubePublication).where(YouTubePublication.account_id == account_id)).all():
        previous_job = db.get(Job, receipt.job_id)
        previous_clip = db.get(Clip, previous_job.payload.get("clip_id")) if previous_job else None
        if previous_clip and previous_clip.source_id == clip.source_id and abs(previous_clip.start_seconds - clip.start_seconds) < 0.05 and abs(previous_clip.duration_seconds - clip.duration_seconds) < 0.05:
            raise ValueError("Этот фрагмент исходника уже отправлялся в выбранный аккаунт")
    job = Job(type=JobType.youtube_publish, payload={
        "account_id": account_id, "clip_id": clip.id, "visibility": visibility,
        "made_for_kids": made_for_kids, "title": title[:100], "pipeline_id": pipeline_id,
    })
    db.add(job)
    db.flush()
    db.add(YouTubePublication(account_id=account_id, job_id=job.id, file_hash=digest))
    db.flush()
    return job


def enqueue_tiktok_web(db: Session, *, account_id: str, clip: Clip, description: str, visibility: str = "private", pipeline_id: str | None = None, unique_across_accounts: bool = False) -> Job:
    account = db.get(Account, account_id)
    if not account or account.platform != "tiktok_web" or account.status != RecordStatus.ready:
        raise ValueError("Выберите TikTok-аккаунт, подключённый через cookie, и проверьте вход")
    source = db.get(SourceVideo, clip.source_id)
    if clip.status != RecordStatus.ready or not clip.local_path or not source or source.archived or not source.rights_confirmed:
        raise ValueError("Нужен готовый клип с подтверждёнными правами на исходник")
    if visibility not in {"private", "public"} or not 1 <= len(description.strip()) <= 2200:
        raise ValueError("Укажите описание 1–2200 символов и видимость TikTok")
    validate_shorts_file(Path(clip.local_path))
    digest = file_hash(Path(clip.local_path))
    statement = select(TikTokWebPublication)
    if not unique_across_accounts:
        statement = statement.where(TikTokWebPublication.account_id == account_id)
    for receipt in db.scalars(statement).all():
        previous = db.get(Job, receipt.job_id)
        previous_clip = db.get(Clip, previous.payload.get("clip_id")) if previous else None
        previous_source = db.get(SourceVideo, previous_clip.source_id) if previous_clip else None
        same_source = previous_source and (previous_source.id == source.id or (source.source_url.startswith("https://") and previous_source.source_url == source.source_url))
        same_excerpt = previous_clip and same_source and abs(previous_clip.start_seconds - clip.start_seconds) < (3 if unique_across_accounts else 0.05) and (unique_across_accounts or abs(previous_clip.duration_seconds - clip.duration_seconds) < 0.05)
        if receipt.file_hash == digest or same_excerpt:
            raise ValueError("Этот файл или фрагмент уже ставился на отправку в аккаунт. Проверьте предыдущую задачу")
    job = Job(type=JobType.tiktok_web_publish, payload={"account_id": account_id, "clip_id": clip.id,
        "description": description.strip(), "visibility": visibility, "pipeline_id": pipeline_id,
        "unique_across_accounts": unique_across_accounts})
    db.add(job)
    db.flush()
    db.add(TikTokWebPublication(account_id=account_id, job_id=job.id, file_hash=digest))
    db.flush()
    return job
