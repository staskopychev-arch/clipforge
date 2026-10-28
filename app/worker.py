import logging
import os
import signal
import socket
import uuid
import random
import re
from datetime import timedelta
from pathlib import Path
from threading import Event

from sqlalchemy import or_, select, update

from .config import settings
from .db import SessionLocal, init_db
from .models import Account, AutomationPlan, Clip, Job, JobType, RecordStatus, SourceVideo, SourceClipUse, YouTubePublication, TikTokWebPublication, utcnow
from .services.media import clip_start_times, probe_duration, render_vertical_clip, probe_video_stream
from .services.auto_montage import resolve_presentation
from .services.editing import EditingOptions, editing_snapshot, excerpt_plan, music_path, parse_editing, resolve_automatic, validate_automatic_source, capture_editing
from .services.tiktok import publish_video
from .services.youtube import SEARCH_WINDOW, relevance_score, search_youtube, source_attribution, SEARCH_RIGHTS_MODE, YouTubeNoResultsError
from .services.y2save import DownloadBlocked, Y2SaveDownloader
from .services.downloaders import FallbackDownloader, ProvidersUnavailable
from .services.scheduler import schedule_tick
from .services.publishing import enqueue_youtube, enqueue_tiktok_web
from .services.source_identity import source_key, existing_clip, require_active, FirstResultError, unavailable_source_keys
from .services.tiktok_web import TikTokWebPolicyError, TikTokWebSessionError, TikTokWebExpiredSessionError, TikTokWebPostRejected, upload_video as upload_tiktok_web
from .services.youtube_upload import YouTubeSessionError, check_session, connection_error, upload_video as upload_youtube


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("clipforge.worker")
# httpx logs full query strings at INFO level, which can expose API keys.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
STOP_EVENT = Event()
WORKER_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


class AutomationStoppedError(RuntimeError):
    """Stopping a plan is terminal for an already queued operation."""


def configured_job_types() -> tuple[JobType, ...]:
    raw = os.environ.get("WORKER_JOB_TYPES", "").strip()
    if not raw:
        return tuple(item for item in JobType if item != JobType.automation_publish)
    try:
        selected = tuple(JobType(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise RuntimeError(f"Invalid WORKER_JOB_TYPES value: {raw}") from exc
    if not selected or JobType.automation_publish in selected:
        raise RuntimeError("WORKER_JOB_TYPES must contain supported active job types")
    return selected


ACTIVE_JOB_TYPES = configured_job_types()


def claim_job(worker_id: str = WORKER_ID) -> Job | None:
    with SessionLocal() as db:
        now = utcnow()
        statement = (
            select(Job)
            .where(Job.status == RecordStatus.queued)
            .where(Job.archived.is_(False))
            .where(Job.type.in_(ACTIVE_JOB_TYPES))
            .where(or_(Job.next_attempt_at.is_(None), Job.next_attempt_at <= now))
            .order_by(Job.next_attempt_at.asc().nulls_first(), Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = db.scalar(statement)
        if not job:
            return None
        # SQLite ignores FOR UPDATE: a compare-and-set claim also protects it.
        claimed = db.execute(update(Job).where(Job.id == job.id, Job.status == RecordStatus.queued).values(
            status=RecordStatus.processing, attempts=Job.attempts + 1, started_at=now,
            finished_at=None, worker_id=worker_id, next_attempt_at=None, error=None,
        ))
        if claimed.rowcount != 1:
            db.rollback()
            return None
        db.commit()
        db.refresh(job)
        return job


def process_download(job: Job, downloader: Y2SaveDownloader) -> dict:
    source_id = job.payload["source_id"]
    with SessionLocal() as db:
        source = db.get(SourceVideo, source_id)
        require_active(source)
        if not source or not source.rights_confirmed:
            raise RuntimeError("Source is missing or rights were not confirmed")
        destination = settings.sources_dir / f"{source.id}.mp4"
        try:
            def download_progress(stage: str, percent: int | None) -> None:
                with SessionLocal() as progress_db:
                    record = progress_db.get(Job, job.id)
                    if record:
                        record.result = {"stage": stage, "percent": percent,
                            "provider": getattr(downloader, "last_provider", None),
                            "attempts": list(getattr(downloader, "attempts", []))}
                        progress_db.commit()
            downloader.progress_callback = download_progress
            downloader.download(source.source_url, destination)
            source.local_path = str(destination)
            source.duration_seconds = probe_duration(destination)
            if job.payload.get("auto_render"):
                auto = job.payload['auto_render']
                try:
                    edit = parse_editing(auto.get('editing'))
                    if edit.automatic:
                        validate_automatic_source(source.duration_seconds)
                    else:
                        excerpt_plan(source.duration_seconds, float(auto.get('duration', 20)), edit)
                except ValueError as exc:
                    raise FirstResultError(str(exc)) from exc
            source.status = RecordStatus.ready
            source.error = None
            auto_render = job.payload.get("auto_render")
            if auto_render:
                attribution = source_attribution(source, auto_render.get("rights_basis", "creative_commons"))
                requested_caption = str(auto_render.get("caption", "")).strip()
                caption = "\n".join(part for part in (requested_caption, attribution) if part)
                db.add(
                    Job(
                        type=JobType.render,
                        payload={
                            "source_id": source.id,
                            "count": 1,
                            "duration": float(auto_render.get("duration", 20)),
                            "editing": auto_render.get("editing") or EditingOptions().model_dump(),
                            "caption": caption,
                            "hashtags": str(auto_render.get("hashtags", "")),
                            "youtube_account_id": auto_render.get("youtube_account_id"),
                            "tiktok_web_account_id": auto_render.get("tiktok_web_account_id"),
                            "unique_tiktok_excerpt": auto_render.get("unique_tiktok_excerpt", False),
                            "visibility": auto_render.get("visibility", "private"),
                            "made_for_kids": auto_render.get("made_for_kids", False),
                            "pipeline_id": job.payload.get("pipeline_id"),
                            "automation_plan_id": job.payload.get("automation_plan_id"),
                        },
                    )
                )
            db.commit()
            return {"source_id": source.id, "path": str(destination),
                "provider": getattr(downloader, "last_provider", None), "attempts": list(getattr(downloader, "attempts", []))}
        except Exception as exc:
            source.status = RecordStatus.error
            source.error = str(exc)[:2000]
            db.commit()
            raise


def process_render(job: Job) -> dict:
    source_id = job.payload["source_id"]
    if int(job.payload.get("count", 1)) != 1:
        raise FirstResultError('Из одного исходника допускается только один клип')
    requested_duration = float(job.payload["duration"])
    options = parse_editing(job.payload.get('editing'))
    created = []
    with SessionLocal() as db:
        source = db.get(SourceVideo, source_id)
        require_active(source)
        if not source.local_path or source.status != RecordStatus.ready:
            raise RuntimeError('Исходник ещё не готов')
        # Serialize source reservation and randomization before any media work.
        db.execute(update(SourceVideo).where(SourceVideo.id == source.id, SourceVideo.archived.is_(False)).values(status=source.status))
        record = db.get(Job,job.id)
        resolved = (record.payload or {}).get('resolved_editing')
        try:
            if options.automatic:
                if not resolved:
                    width,height,_ = probe_video_stream(Path(source.local_path))
                    resolved = resolve_automatic(source.duration_seconds or probe_duration(Path(source.local_path)),width,height,
                        job.payload.get('editing',{}).get('music_pool') or ([options.music_id] if options.music_id else []),
                        profile_version=job.payload.get('editing',{}).get('automatic_profile',1))
                    record.payload = {**record.payload,'resolved_editing':resolved}
                start,duration = resolved['start_seconds'],resolved['duration_seconds']
                options = EditingOptions(automatic=True,start_seconds=start,speed=resolved['speed'],quality=resolved['quality'],music_id=resolved['music_id'])
            else:
                start,duration = excerpt_plan(source.duration_seconds or probe_duration(Path(source.local_path)),requested_duration,options)
            track = music_path(options.music_id) if options.music_id else None
        except ValueError as exc:
            raise FirstResultError(str(exc)) from exc
        identity = source_key(source.source_url, source.id)
        use = db.get(SourceClipUse, identity)
        if use and use.job_id != job.id:
            raise FirstResultError('Из этого исходника уже создан или зарезервирован клип. Выполните поиск следующего видео.')
        if not use and existing_clip(db, source.source_url, source.id):
            raise FirstResultError('Из этого видео уже создан клип. Повторный монтаж отключён.')
        if not use:
            use = SourceClipUse(source_key=identity, source_id=source.id, job_id=job.id)
            db.add(use)
        clip = db.get(Clip, use.clip_id) if use.clip_id else None
        if not clip:
            clip = Clip(source_id=source.id, start_seconds=start, duration_seconds=duration,
                caption=str(job.payload.get('caption', '')), hashtags=str(job.payload.get('hashtags', '')),
                status=RecordStatus.processing)
            db.add(clip); db.flush(); use.clip_id = clip.id
        destination = settings.clips_dir / f'{clip.id}.mp4'
        ready = clip.status == RecordStatus.ready and clip.local_path and Path(clip.local_path).is_file()
        db.commit()
        if not ready:
            try:
                # OCR runs after the source reservation is committed, without
                # holding SQLite's write lock during frame decoding/recognition.
                if resolved and resolved.get('profile_version') == 2 and not resolved.get('presentation'):
                    presentation = resolve_presentation(Path(source.local_path),resolved,source.title)
                    resolved = {**resolved,'presentation':presentation}
                    record.payload = {**record.payload,'resolved_editing':resolved}
                    db.commit()
                kwargs = {}
                if options.speed != 1 or options.quality != '1080p' or track:
                    kwargs = {'speed': options.speed, 'quality': options.quality, 'music': track}
                if resolved and resolved.get('presentation'):
                    kwargs['presentation'] = resolved['presentation']
                render_vertical_clip(Path(source.local_path), destination, start, duration, **kwargs)
                clip.local_path = str(destination); clip.status = RecordStatus.ready; clip.error = None
                db.commit()
            except Exception as exc:
                clip.status = RecordStatus.error; clip.error = str(exc)[:2000]; db.commit()
                raise
        created = [clip.id]
        # Recover the same queued transmission after a render-job interruption.
        if job.payload.get('pipeline_id'):
            previous = db.scalar(select(Job).where(Job.type.in_([JobType.youtube_publish, JobType.tiktok_web_publish]),
                Job.payload['pipeline_id'].as_string() == job.payload['pipeline_id'],
                Job.payload['clip_id'].as_string() == clip.id))
            if previous:
                return {'clip_ids': created, 'publish_job_id': previous.id, 'recovered_existing': True}
        result = {"clip_ids": created}
        if resolved:
            result["editing"] = resolved
        if job.payload.get("youtube_account_id"):
            # One selected clip per explicit cycle, preserving the others for review.
            chosen = db.get(Clip, random.SystemRandom().choice(created))
            upload_job = enqueue_youtube(
                db, account_id=job.payload["youtube_account_id"], clip=chosen,
                visibility=job.payload.get("visibility", "private"),
                made_for_kids=bool(job.payload.get("made_for_kids", False)),
                title=job.payload.get("title") or source.title[:91] + " #Shorts",
                pipeline_id=job.payload.get("pipeline_id"),
            )
            upload_job.payload = {**upload_job.payload, "automation_plan_id": job.payload.get("automation_plan_id")}
            db.commit()
            result["publish_job_id"] = upload_job.id
        if job.payload.get("tiktok_web_account_id"):
            chosen = db.get(Clip, created[0])
            upload_job = enqueue_tiktok_web(db, account_id=job.payload["tiktok_web_account_id"], clip=chosen,
                description=job.payload.get("tiktok_description") or "\n".join(p for p in (chosen.caption.strip(), chosen.hashtags.strip()) if p),
                visibility=job.payload.get("visibility", "private"), pipeline_id=job.payload.get("pipeline_id"),
                unique_across_accounts=bool(job.payload.get("unique_tiktok_excerpt")))
            upload_job.payload = {**upload_job.payload, "automation_plan_id": job.payload.get("automation_plan_id")}
            db.commit()
            result["publish_job_id"] = upload_job.id
        return result


def process_discovery(job: Job) -> dict:
    payload = job.payload
    if payload.get("youtube_account_id"):
        with SessionLocal() as db:
            account = db.get(Account, payload["youtube_account_id"])
            if not account or account.platform != "youtube" or account.status != RecordStatus.ready:
                raise RuntimeError("Подключите доступный YouTube-аккаунт")
            encrypted_session = account.access_token
            db.get(Job, job.id).result = {"stage": "checking_youtube_session"}
            db.commit()
        try:
            check_session(encrypted_session)
        except YouTubeSessionError:
            with SessionLocal() as db:
                db.get(Account, payload["youtube_account_id"]).status = RecordStatus.reauth_required
                db.commit()
            raise
        except Exception as exc:
            raise RuntimeError(connection_error(exc)) from exc
    excluded = {source_key(url) for url in payload.get('exclude_source_urls', [])}
    with SessionLocal() as lookup:
        unavailable = unavailable_source_keys(lookup) | excluded
    queries = payload.get('queries') or [str(payload['query'])]
    attempts = []
    results = []
    selected_position = None
    selected_score = None
    for query in dict.fromkeys(queries):
        found = search_youtube(query, SEARCH_WINDOW, payload.get('duration_filter', 'any'))[:SEARCH_WINDOW]
        attempt = {'query': query, 'returned': len(found), 'accepted': 0, 'skipped': 0}
        attempts.append(attempt)
        candidates = []
        for position, item in enumerate(found, 1):
            if source_key(item['url']) in unavailable:
                attempt['skipped'] += 1
                continue
            candidates.append((relevance_score(query, item), -position, item))
        if candidates:
            selected_score, negative_position, item = max(candidates, key=lambda candidate: candidate[:2])
            results = [item]
            selected_position = -negative_position
            attempt['accepted'] = 1
            break
    if not results:
        error = YouTubeNoResultsError(attempts)
        if any(a['skipped'] for a in attempts):
            error.args = ('Все результаты поиска уже использованы, исключены, находятся в обработке или временно недоступны для скачивания. Укажите другой запрос.',)
        raise error
    created: list[str] = []
    with SessionLocal() as db:
        for item in results:
            source = db.scalar(select(SourceVideo).where(SourceVideo.source_url == item["url"], SourceVideo.archived.is_(False)))
            if not source:
                source = SourceVideo(source_url=item["url"], title=f"{item['title']} · {item['channel']}", rights_confirmed=True)
                db.add(source)
                db.flush()
            auto_render = {
                "rights_basis": item.get("rights_basis", SEARCH_RIGHTS_MODE),
                "editing": payload.get("editing") if payload.get("editing") is not None else capture_editing(db),
                "count": 1, "duration": payload["clip_duration"],
                "caption": payload.get("caption", ""), "hashtags": payload.get("hashtags", ""),
                "youtube_account_id": payload.get("youtube_account_id"),
                "tiktok_web_account_id": payload.get("tiktok_web_account_id"),
                "unique_tiktok_excerpt": payload.get("unique_tiktok_excerpt", False),
                "visibility": payload.get("visibility", "private"),
                "made_for_kids": payload.get("made_for_kids", False),
                "automation_plan_id": payload.get("automation_plan_id"),
            }
            if source.status == RecordStatus.ready and source.local_path and Path(source.local_path).is_file():
                if not source.rights_confirmed:
                    raise RuntimeError("Права на сохранённый исходник не подтверждены")
                auto_render["caption"] = "\n".join(part for part in (auto_render["caption"], source_attribution(source, auto_render["rights_basis"])) if part)
                db.add(Job(type=JobType.render, payload={"source_id": source.id, **auto_render, "pipeline_id": job.id}))
                created.append(source.id)
                continue
            active = db.scalars(select(Job).where(Job.type == JobType.download, Job.status.in_([RecordStatus.queued, RecordStatus.processing]))).all()
            if any(other.payload.get("source_id") == source.id for other in active):
                raise RuntimeError("Этот исходник уже скачивается. Дождитесь предыдущей задачи")
            source.status = RecordStatus.queued
            source.error = None
            db.add(
                Job(
                    type=JobType.download,
                    payload={
                        "source_id": source.id,
                        "auto_render": auto_render,
                        "pipeline_id": job.id,
                        
                        "automation_plan_id": payload.get("automation_plan_id"),
                    },
                )
            )
            created.append(source.id)
        db.commit()
    return {"found": len(results), "created_source_ids": created, "rights_basis": SEARCH_RIGHTS_MODE,
            "search_attempts": attempts, "selected_position": selected_position,
            "selection_policy": "best_of_20", "selected_score": selected_score}


def process_publish(job: Job) -> dict:
    with SessionLocal() as db:
        clip = db.get(Clip, job.payload["clip_id"])
        require_active(db.get(SourceVideo, clip.source_id) if clip else None)
        account = db.get(Account, job.payload["account_id"])
        if not clip or not clip.local_path or clip.status != RecordStatus.ready:
            raise RuntimeError("Clip is not ready")
        if not account or account.platform != "tiktok" or account.status not in (RecordStatus.ready, RecordStatus.reauth_required):
            raise RuntimeError("TikTok account is unavailable")
        caption = " ".join(part for part in (clip.caption.strip(), clip.hashtags.strip()) if part)
        try:
            result = publish_video(
                account=account,
                video_path=Path(clip.local_path),
                caption=caption,
                privacy_level=job.payload.get("privacy_level", "SELF_ONLY"),
                mode=job.payload.get("mode", "inbox"),
            )
            db.commit()  # Persist refreshed tokens when refresh_account changed them.
            return result
        except Exception:
            if account.status == RecordStatus.reauth_required:
                db.commit()
            raise


def process_youtube_publish(job: Job) -> dict:
    with SessionLocal() as db:
        account = db.get(Account, job.payload["account_id"])
        clip = db.get(Clip, job.payload["clip_id"])
        require_active(db.get(SourceVideo, clip.source_id) if clip else None)
        reservation = db.scalar(select(YouTubePublication).where(YouTubePublication.job_id == job.id))
        if not account or account.platform != "youtube" or account.status != RecordStatus.ready:
            raise YouTubeSessionError("YouTube-аккаунт требует повторного подключения")
        if not clip or not clip.local_path or clip.status != RecordStatus.ready or not reservation:
            raise RuntimeError("Клип или запись публикации не найдены")
        if reservation.stage == "published":
            return {"video_url": reservation.video_url, "confirmed": True, "duplicate_skipped": True}
        if reservation.stage == "submitting":
            raise RuntimeError("Результат предыдущего сохранения неизвестен; сначала проверьте YouTube Studio")
        if reservation.stage == "uploading":
            raise RuntimeError("Файл уже отправлен: завершите существующий черновик, не загружайте повторно")
        session = account.access_token
        path = Path(clip.local_path)
        description = "\n".join(item for item in (clip.caption.strip(), clip.hashtags.strip()) if item)
        title = job.payload.get("title") or (clip.caption.splitlines()[0][:91] if clip.caption else "Видео") + " #Shorts"

    def progress(stage: str, url: str | None) -> None:
        with SessionLocal() as db:
            publication = db.scalar(select(YouTubePublication).where(YouTubePublication.job_id == job.id))
            record = db.get(Job, job.id)
            publication.stage = stage
            if url:
                publication.video_url = url
            record.result = {"stage": stage, "video_url": publication.video_url}
            db.commit()
        log.info("YouTube job %s stage=%s", job.id, stage)

    try:
        result = upload_youtube(encrypted_session=session, video_path=path, title=title,
            description=description[:5000], visibility=job.payload.get("visibility", "private"),
            made_for_kids=bool(job.payload.get("made_for_kids", False)), on_progress=progress)
        if not result.get("confirmed"):
            raise RuntimeError("Save отправлен, но сохранение не подтверждено; проверьте существующее видео в Studio")
        return result
    except YouTubeSessionError:
        with SessionLocal() as db:
            db.get(Account, job.payload["account_id"]).status = RecordStatus.reauth_required
            db.commit()
        raise
    except Exception as exc:
        if "ERR_NAME_NOT_RESOLVED" in str(exc) or "ERR_NETWORK_ACCESS_DENIED" in str(exc):
            raise RuntimeError(connection_error(exc)) from exc
        raise


def process_tiktok_web_publish(job: Job) -> dict:
    with SessionLocal() as db:
        account = db.get(Account, job.payload["account_id"])
        clip = db.get(Clip, job.payload["clip_id"])
        require_active(db.get(SourceVideo, clip.source_id) if clip else None)
        receipt = db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id == job.id))
        if not account or account.platform != "tiktok_web" or account.status != RecordStatus.ready:
            raise TikTokWebSessionError("Обновите сессию своего TikTok-аккаунта")
        if not clip or clip.status != RecordStatus.ready or not clip.local_path or not receipt:
            raise RuntimeError("Нет готового клипа или записи TikTok-публикации")
        if receipt.stage == "published":
            return {"confirmed": True, "video_url": receipt.video_url, "duplicate_skipped": True}
        if receipt.stage in {"uploading", "submitting"}:
            raise RuntimeError("Файл уже передавался TikTok. Проверьте Studio; повторная отправка запрещена")
        session, path = account.access_token, Path(clip.local_path)

    def progress(stage, url):
        with SessionLocal() as db:
            receipt = db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id == job.id))
            receipt.stage = stage
            if url:
                receipt.video_url = url
            db.get(Job, job.id).result = {"stage": stage, "video_url": receipt.video_url, "platform": "tiktok_web"}
            db.commit()
        log.info("TikTok web job %s stage=%s", job.id, stage)

    try:
        result = upload_tiktok_web(encrypted_session=session, video_path=path,
            description=job.payload["description"], visibility=job.payload.get("visibility", "private"), on_progress=progress, diagnostic_id=job.id)
        if not result.get("confirmed"):
            raise RuntimeError("TikTok не подтвердил публикацию; проверьте Studio перед повтором")
        if job.payload.get("visibility") == "public" and result.get("public_visibility_confirmed") is False:
            with SessionLocal() as db:
                db.get(Job, job.id).result = result
                db.commit()
            raise RuntimeError("Видео создано, но TikTok не подтвердил «Для всех». Текущая видимость и ссылка сохранены; повторная загрузка запрещена")
        return result
    except TikTokWebPostRejected as exc:
        with SessionLocal() as db:
            row = db.get(Job, job.id)
            row.result = {**(row.result or {}), 'platform_post_code': exc.code,
                'requires_owner_review': exc.requires_owner_review}
            db.commit()
        raise
    except TikTokWebPolicyError:
        with SessionLocal() as db:
            db.get(Account, job.payload["account_id"]).status = RecordStatus.error
            row = db.get(Job, job.id)
            row.result = {**(row.result or {}), "platform_error_code": 30522, "requires_owner_review": True}
            db.commit()
        raise
    except TikTokWebSessionError:
        with SessionLocal() as db:
            db.get(Account, job.payload["account_id"]).status = RecordStatus.reauth_required
            db.commit()
        raise


def complete_job(job_id: str, result: dict) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if job:
            job.status = RecordStatus.ready
            job.result = result
            job.error = None
            job.next_attempt_at = None
            job.finished_at = utcnow()
            db.commit()


def fail_job(job_id: str, message: str, *, error_kind: str | None = None, search_attempts=None, provider_attempts=None) -> None:
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            return
        job.error = message[:4000]
        if error_kind == 'automation_stopped' and (job.result or {}).get('error_kind') in {'manual_verification', 'platform_policy', 'session_expired'}:
            job.result = {**job.result, 'cancelled_by_stop': True}
        elif error_kind:
            job.result = {**(job.result or {}), "error_kind": error_kind}
        if search_attempts is not None:
            job.result = {**(job.result or {}), "search_attempts": search_attempts}
        if provider_attempts is not None:
            job.result = {**(job.result or {}), 'attempts': provider_attempts, 'stage': 'download_failed'}
        if settings.youtube_api_key:
            job.error = job.error.replace(settings.youtube_api_key, "[redacted]")
        job.finished_at = utcnow()
        plan_id = job.payload.get('automation_plan_id')
        plan = db.get(AutomationPlan, plan_id) if plan_id else None
        stopped = bool(plan_id and (not plan or not plan.enabled))
        if error_kind in {'first_result_unusable', 'automation_stopped'}:
            job.status = RecordStatus.error
            job.next_attempt_at = None
        elif job.type in (JobType.youtube_publish, JobType.tiktok_web_publish):
            # A failed save may still have succeeded upstream. Never auto-resubmit.
            account = db.get(Account, job.payload.get("account_id"))
            job.status = RecordStatus.reauth_required if account and account.status == RecordStatus.reauth_required else RecordStatus.error
            job.next_attempt_at = None
        elif stopped:
            job.status = RecordStatus.error
            job.next_attempt_at = None
        elif job.type == JobType.download and error_kind not in {'manual_verification', 'first_result_unusable'} and job.attempts < settings.max_job_attempts:
            job.status = RecordStatus.queued
            job.next_attempt_at = utcnow() + timedelta(seconds=settings.download_failure_cooldown_seconds)
        elif job.type in (JobType.download, JobType.discovery):
            job.status = RecordStatus.error
            job.next_attempt_at = None
        elif job.attempts < settings.max_job_attempts:
            delay = settings.worker_retry_base_seconds * (2 ** max(0, job.attempts - 1))
            job.status = RecordStatus.queued
            job.next_attempt_at = utcnow() + timedelta(seconds=delay)
        else:
            job.status = RecordStatus.error
            job.next_attempt_at = None
        db.commit()


def process_job(job: Job, downloader: Y2SaveDownloader) -> dict:
    if job.archived:
        raise FirstResultError('Архивное задание нельзя запускать повторно')
    if job.payload.get("automation_plan_id"):
        with SessionLocal() as db:
            plan = db.get(AutomationPlan, job.payload["automation_plan_id"])
            if not plan or not plan.enabled:
                raise AutomationStoppedError("Режим 24/7 остановлен; новая операция не запущена")
    handlers = {
        JobType.discovery: lambda: process_discovery(job),
        JobType.download: lambda: process_download(job, downloader),
        JobType.render: lambda: process_render(job),
        JobType.publish: lambda: process_publish(job),
        JobType.youtube_publish: lambda: process_youtube_publish(job),
        JobType.tiktok_web_publish: lambda: process_tiktok_web_publish(job),
    }
    try:
        handler = handlers[job.type]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported job type: {job.type}") from exc
    return handler()


def run_once(downloader: Y2SaveDownloader) -> bool:
    """Process one due job. Returns False when the queue currently has no due work."""
    if JobType.discovery in ACTIVE_JOB_TYPES:
        with SessionLocal() as db:
            schedule_tick(db)
    job = claim_job()
    if not job:
        return False
    log.info("Processing %s job %s", job.type.value, job.id)
    try:
        complete_job(job.id, process_job(job, downloader))
    except Exception as exc:
        log.exception("Job %s failed", job.id)
        error_kind = "automation_stopped" if isinstance(exc, AutomationStoppedError) else "download_failed" if isinstance(exc, ProvidersUnavailable) else "first_result_unusable" if isinstance(exc, FirstResultError) else "empty_search" if isinstance(exc, YouTubeNoResultsError) else "platform_policy" if isinstance(exc, TikTokWebPolicyError) else "session_expired" if isinstance(exc, TikTokWebExpiredSessionError) else "manual_verification" if isinstance(exc, (DownloadBlocked, YouTubeSessionError, TikTokWebSessionError)) else None
        fail_job(job.id, str(exc), error_kind=error_kind, search_attempts=getattr(exc, 'attempts', None), provider_attempts=getattr(exc, 'provider_attempts', None))
    return True


def recover_stale_jobs() -> int:
    cutoff = utcnow() - timedelta(seconds=settings.worker_stale_after_seconds)
    recovered = 0
    with SessionLocal() as db:
        statement = select(Job).where(
            Job.status == RecordStatus.processing, Job.archived.is_(False),
            or_(Job.started_at.is_(None), Job.started_at < cutoff),
        )
        for stale_job in db.scalars(statement).all():
            if stale_job.type == JobType.automation_publish:
                continue
            if stale_job.type in (JobType.youtube_publish, JobType.tiktok_web_publish):
                stale_job.status = RecordStatus.error
                stale_job.error = "Загрузка была прервана; проверьте Studio выбранной платформы перед повтором"
                stale_job.finished_at = utcnow()
                recovered += 1
                continue
            stale_job.status = RecordStatus.queued
            stale_job.worker_id = None
            stale_job.next_attempt_at = utcnow()
            stale_job.error = "Recovered after an interrupted worker"
            recovered += 1
        db.commit()
    return recovered


def request_stop(signum=None, frame=None) -> None:
    del frame
    log.info("Stop requested%s", f" by signal {signum}" if signum else "")
    STOP_EVENT.set()


def run_embedded(stop: Event) -> None:
    """Runs in a dedicated thread, keeping Playwright out of the ASGI event loop."""
    downloader = FallbackDownloader()
    recover_stale_jobs()
    log.info("Background worker started")
    try:
        while not stop.is_set():
            try:
                if not run_once(downloader):
                    stop.wait(settings.worker_poll_seconds)
            except Exception:
                log.exception("Queue polling failed")
                stop.wait(settings.worker_poll_seconds)
    finally:
        downloader.close()
        log.info("Background worker stopped")


def main() -> None:
    settings.ensure_directories()
    init_db()
    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), request_stop)
    recovered = recover_stale_jobs()
    if recovered:
        log.warning("Recovered %s stale jobs", recovered)
    downloader = FallbackDownloader()
    log.info("Worker %s started for job types: %s", WORKER_ID, ",".join(item.value for item in ACTIVE_JOB_TYPES))
    try:
        while not STOP_EVENT.is_set():
            if not run_once(downloader):
                STOP_EVENT.wait(settings.worker_poll_seconds)
    finally:
        downloader.close()
        log.info("Worker %s stopped", WORKER_ID)


if __name__ == "__main__":
    main()

