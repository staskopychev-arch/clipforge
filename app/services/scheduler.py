"""Durable, opt-in scheduling. Security failures pause the entire plan."""
from datetime import timedelta, timezone
import shutil
from pathlib import Path

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..models import Account, AutomationPlan, Clip, Job, JobType, RecordStatus, SourceVideo, YouTubePublication, TikTokWebPublication, utcnow
from ..config import settings
from .content_selection import cached_unique_excerpt
from .editing import editing_snapshot, capture_editing, EditingSetupError
from .youtube import source_attribution


def plan_state(db: Session) -> dict:
    plan = db.get(AutomationPlan, 1)
    if plan is None:
        return {"enabled": False, "status": "stopped", "config": {}, "next_run_at": None, "last_error": None}
    return {"enabled": plan.enabled, "status": plan.status, "config": plan.config,
            "next_run_at": plan.next_run_at.isoformat() if plan.next_run_at else None,
            "last_pipeline_id": plan.last_pipeline_id, "last_error": plan.last_error}


def pipeline_jobs(db: Session, pipeline_id: str) -> list[Job]:
    return db.scalars(select(Job).where(or_(
        Job.id == pipeline_id, Job.payload["pipeline_id"].as_string() == pipeline_id,
    ), Job.archived.is_(False))).all()


def pause(plan: AutomationPlan, reason: str) -> None:
    plan.enabled = False
    plan.status = "paused"
    plan.last_error = reason[:1000]


def start_plan(db: Session, config: dict) -> dict:
    plan = db.get(AutomationPlan, 1)
    if plan and plan.enabled:
        raise ValueError("Режим 24/7 уже включён; сначала остановите его")
    if plan and plan.last_pipeline_id and any(job.status in {RecordStatus.queued, RecordStatus.processing} for job in pipeline_jobs(db, plan.last_pipeline_id)):
        raise ValueError("Дождитесь завершения текущего цикла перед новым запуском")
    capture_editing(db)
    platform = config.get("platform", "youtube")
    targets = config.get("account_post_targets")
    if targets is not None and (platform != "tiktok_web" or not config["publish"]
            or set(targets) != set(config["account_ids"])
            or any(type(value) is not int or value < 1 for value in targets.values())):
        raise ValueError("Цель публикаций должна быть положительной и задана для каждого выбранного аккаунта TikTok")
    if config.get("source_ids") is not None and not config["source_ids"]:
        raise ValueError("Выберите хотя бы один разрешённый исходник")
    for source_id in config.get('source_ids') or []:
        source = db.get(SourceVideo, source_id)
        if not source or source.archived:
            raise ValueError('Выберите действующий исходник')
    if platform == "tiktok_web" and config["visibility"] == "unlisted":
        raise ValueError("У TikTok нет видимости «По ссылке»")
    if config["publish"]:
        for account_id in config["account_ids"]:
            account = db.get(Account, account_id)
            if not account or account.platform != platform or account.status != RecordStatus.ready:
                raise ValueError("Каждый выбранный аккаунт должен иметь действующую сессию выбранной платформы")
    if plan is None:
        plan = AutomationPlan(id=1)
        db.add(plan)
    retained_editing = {k: plan.config[k] for k in ("editing", "music_library") if plan.config and k in plan.config}
    retained_draft = {"five_video_draft": {**plan.config["five_video_draft"],
        "queries": config["queries"], "topic": config.get("topic", "any")}} if plan.config and plan.config.get("five_video_draft") else {}
    plan.config = {**retained_editing, **retained_draft, **config, "account_index": 0, "query_index": 0,
                   "clip_count": 1}
    plan.enabled = True
    plan.status = "scheduled"
    plan.last_error = None
    plan.last_pipeline_id = None
    plan.next_run_at = utcnow()
    db.commit()
    return plan_state(db)


def stop_plan(db: Session) -> dict:
    plan = db.get(AutomationPlan, 1)
    if plan:
        plan.enabled = False
        plan.status = "stopped"
        if plan.last_pipeline_id:
            for job in pipeline_jobs(db, plan.last_pipeline_id):
                if job.status == RecordStatus.queued:
                    job.status = RecordStatus.error
                    job.error = "Режим 24/7 остановлен пользователем"
                    job.finished_at = utcnow()
                    job.next_attempt_at = None
                    job.result = {**(job.result or {}), 'cancelled_by_stop': True,
                                  'error_kind': (job.result or {}).get('error_kind') or 'automation_stopped'}
        db.commit()
    return plan_state(db)


def update_plan_content(db: Session, caption: str, hashtags: str) -> dict:
    """Change future descriptions without starting a plan or rewriting its cursor."""
    content = {"caption": caption.strip(), "hashtags": hashtags.strip()}
    for _ in range(3):
        db.expire_all()
        plan = db.get(AutomationPlan, 1)
        if plan is None:
            db.add(AutomationPlan(id=1, enabled=False, status="stopped", config=content))
            db.commit()
            return plan_state(db)
        previous = dict(plan.config)
        changed = db.execute(update(AutomationPlan).where(
            AutomationPlan.id == 1, AutomationPlan.config == previous,
        ).values(config={**previous, **content}).execution_options(synchronize_session=False))
        if changed.rowcount == 1:
            db.commit()
            db.expire_all()
            return plan_state(db)
        db.rollback()
    raise ValueError("Очередь обновилась одновременно. Сохраните описание ещё раз.")


def used_source_urls(db: Session, account_id: str, platform="youtube") -> set[str]:
    model = TikTokWebPublication if platform == "tiktok_web" else YouTubePublication
    statement = select(model)
    if platform != "tiktok_web":
        statement = statement.where(model.account_id == account_id)
    receipts = db.scalars(statement).all()
    urls = set()
    for receipt in receipts:
        job = db.get(Job, receipt.job_id)
        clip = db.get(Clip, job.payload.get("clip_id")) if job else None
        source = db.get(SourceVideo, clip.source_id) if clip else None
        if source:
            urls.add(source.source_url)
    return urls


def confirmed_tiktok_count(db: Session, account_id: str) -> int:
    """Count unique, receipt-matched created videos across the entire history."""
    from .tiktok_web import canonical_video_url
    urls = set()
    for receipt in db.scalars(select(TikTokWebPublication).where(
            TikTokWebPublication.account_id == account_id,
            TikTokWebPublication.stage == "published")).all():
        job = db.get(Job, receipt.job_id)
        result = (job.result or {}) if job else {}
        url = canonical_video_url(receipt.video_url or "")
        if result.get("confirmed") is True and url and canonical_video_url(result.get("video_url") or "") == url:
            urls.add(url)
    return len(urls)


def expired_session_before_post(db: Session, job: Job) -> bool:
    result = job.result or {}
    if (job.type != JobType.tiktok_web_publish or job.status != RecordStatus.reauth_required
            or result.get('error_kind') != 'session_expired' or result.get('confirmed') or result.get('video_url')
            or result.get('requires_owner_review') or result.get('platform_error_code') or result.get('platform_post_code')):
        return False
    receipt = db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id == job.id))
    return bool(receipt and receipt.account_id == job.payload.get('account_id')
                and receipt.stage in {'reserved', 'authenticated', 'uploading'} and not receipt.video_url)


def schedule_tick(db: Session) -> bool:
    plan = db.get(AutomationPlan, 1)
    if not plan or not plan.enabled:
        return False
    if plan.config.get("five_video_batch"):
        from .five_videos import tick
        return tick(db, plan)
    if plan.last_pipeline_id:
        family = pipeline_jobs(db, plan.last_pipeline_id)
        # A failed download may already have queued its fallback. Wait for the
        # complete family before deciding whether a cycle actually failed.
        if any(job.status in {RecordStatus.queued, RecordStatus.processing} for job in family):
            return False
        by_id = {job.id: job for job in family}
        failures = [job for job in family if job.status in {RecordStatus.error, RecordStatus.reauth_required}
                    and not ((job.result or {}).get("fallback_job_id") in by_id)
                    and not (job.type == JobType.tiktok_web_publish and (job.result or {}).get("confirmed")
                             and (job.result or {}).get("video_url"))]
        failed = failures[0] if failures else None
        if failed:
            if expired_session_before_post(db, failed):
                previous = dict(plan.config)
                aid = failed.payload['account_id']
                expired = {**previous.get('expired_session_accounts', {}), aid: {
                    'job_id': failed.id, 'skipped_at': utcnow().isoformat()}}
                changed = db.execute(update(AutomationPlan).where(
                    AutomationPlan.id == 1, AutomationPlan.enabled.is_(True),
                    AutomationPlan.config == previous, AutomationPlan.last_pipeline_id == plan.last_pipeline_id,
                ).values(config={**previous, 'expired_session_accounts': expired,
                    'account_index': (previous['account_ids'].index(aid) + 1) % len(previous['account_ids'])},
                    last_pipeline_id=None, status='scheduled', next_run_at=utcnow(),
                    last_error='Аккаунт пропущен: сессия истекла до отправки.'))
                if changed.rowcount != 1:
                    db.rollback(); return False
                db.commit(); return False
            pause(plan, f"Цикл остановлен: {failed.error or failed.status.value}")
            db.commit()
            return False
        if plan.config.get("platform") == "tiktok_web":
            parent = by_id.get(plan.last_pipeline_id)
            uploaded = next((job for job in family if job.type == JobType.tiktok_web_publish and (job.result or {}).get("confirmed")), None)
            if uploaded and parent and not (parent.result or {}).get("rotation_advanced"):
                previous = dict(plan.config)
                advanced = db.execute(update(AutomationPlan).where(
                    AutomationPlan.id == 1, AutomationPlan.enabled.is_(True), AutomationPlan.config == previous,
                ).values(config={**previous, "account_index": (int(previous.get("account_index", 0)) + 1) % len(previous["account_ids"])}))
                if advanced.rowcount != 1:
                    db.rollback()
                    return False
                parent.result = {**(parent.result or {}), "rotation_advanced": True}
                if (uploaded.result or {}).get("public_visibility_confirmed") is False:
                    plan.last_error = "Видео создано, но TikTok пока не подтвердил «Для всех»; ссылка и фактическая видимость есть в журнале"
                plan.status = "scheduled"
                db.commit()
    now = utcnow()
    due = plan.next_run_at
    if due and due.replace(tzinfo=timezone.utc) > now:
        return False
    storage = settings.media_root if settings.media_root.exists() else settings.media_root.parent
    if shutil.disk_usage(storage).free < 3 * 1024**3:
        pause(plan, "Свободно меньше 3 ГБ: освободите место или увеличьте диск; файлы автоматически не удаляются")
        db.commit()
        return False
    config = plan.config
    platform = config.get("platform", "youtube")
    account_id = None
    if config["publish"]:
        candidates = []
        ordered = config["account_ids"]
        if platform == "tiktok_web":
            position = int(config.get("account_index", 0)) % len(ordered)
            ordered = ordered[position:] + ordered[:position]
        for chosen_id in ordered:
            if chosen_id in config.get('expired_session_accounts', {}):
                continue
            if config.get("account_post_targets") is not None:
                if confirmed_tiktok_count(db, chosen_id) >= config["account_post_targets"][chosen_id]:
                    continue
            account = db.get(Account, chosen_id)
            if not account or account.status != RecordStatus.ready or account.platform != platform:
                pause(plan, "Аккаунту требуется повторный вход; автоматическая замена аккаунта отключена")
                db.commit()
                return False
            model = TikTokWebPublication if platform == "tiktok_web" else YouTubePublication
            receipts = db.scalars(select(model).where(
                model.account_id == chosen_id,
                model.created_at >= now - timedelta(hours=24))).all()
            if platform == "tiktok_web":
                receipts = [receipt for receipt in receipts if not (
                    receipt.stage in {"reserved", "authenticated", "uploading"}
                    and (previous := db.get(Job, receipt.job_id))
                    and (previous.result or {}).get("verified_no_post") is True)]
            # Count reservations and uncertain uploads as well, not only success.
            if not config.get("daily_limit") or len(receipts) < config["daily_limit"]:
                candidates.append((len(receipts), chosen_id))
        if not candidates:
            if config.get("account_post_targets") is not None:
                # Daily quota is a separate constraint. It must not be mistaken
                # for having reached the lifetime publication target.
                remaining = []
                for aid, target in config["account_post_targets"].items():
                    if aid in config.get('expired_session_accounts', {}):
                        continue
                    remaining.append(confirmed_tiktok_count(db, aid) < target)
                if not any(remaining):
                    plan.enabled = False
                    plan.status = "stopped"
                    plan.last_error = 'Завершено с пропуском аккаунтов с истёкшей сессией' if config.get('expired_session_accounts') else None
                    db.commit()
                    return False
            if all(aid in config.get('expired_session_accounts', {}) for aid in config['account_ids']):
                plan.enabled = False; plan.status = 'stopped'
                plan.last_error = 'Нет аккаунтов с действующей сессией; пропуски сохранены'
                db.commit(); return False
            plan.status = "quota_wait"
            plan.next_run_at = now + timedelta(hours=1)
            db.commit()
            return False
        account_id = candidates[0][1] if platform == "tiktok_web" else min(candidates)[1]
        if platform == "tiktok_web":
            # If an account is at its daily limit, resume rotation after the
            # account actually chosen, not after the skipped cursor position.
            config = {**config, "account_index": config["account_ids"].index(account_id)}
    query = config["queries"][int(config.get("query_index", 0)) % len(config["queries"])]
    # Compare-and-set also protects SQLite, which ignores FOR UPDATE.
    expected_pipeline = plan.last_pipeline_id
    claimed = db.execute(update(AutomationPlan).where(
        AutomationPlan.id == 1, AutomationPlan.enabled.is_(True),
        AutomationPlan.next_run_at == due, AutomationPlan.last_pipeline_id == expected_pipeline,
        AutomationPlan.config == plan.config,
    ).values(next_run_at=now + timedelta(minutes=config["interval_minutes"])))
    if claimed.rowcount != 1:
        db.rollback()
        return False
    try:
        editing = capture_editing(db)
    except EditingSetupError as exc:
        pause(plan,str(exc));db.commit();return False
    payload = {"query": query, "max_results": 1, "editing": editing,
        "clip_count": config["clip_count"], "clip_duration": config["clip_duration"],
        "duration_filter": "any", "caption": config.get("caption", ""), "hashtags": config.get("hashtags", ""),
        "youtube_account_id": account_id if platform == "youtube" else None,
        "tiktok_web_account_id": account_id if platform == "tiktok_web" else None,
        "unique_tiktok_excerpt": platform == "tiktok_web" and bool(account_id),
        "topic": config.get("topic", "any"),
        "visibility": config["visibility"], "made_for_kids": False,
        "automation_plan_id": 1, "exclude_source_urls": sorted(used_source_urls(db, account_id, platform)) if account_id else []}
    job = Job(type=JobType.discovery, payload=payload)
    # Search mode must reach discovery even when unrelated cached media exist.
    # Cached-only selection is reserved for explicitly pinned source IDs.
    if platform == "tiktok_web" and account_id and config.get("source_ids") is not None:
        cached = cached_unique_excerpt(db, config["clip_duration"], config.get("topic", "any"), config.get("source_ids"))
        if not cached and config.get("source_ids") is not None:
            pause(plan, "В выбранных исходниках нет доступного нового фрагмента; автоматическая подмена отключена")
            db.commit()
            return False
        if cached:
            source, _ = cached
            caption = "\n".join(p for p in (config.get("caption", "").strip(), source_attribution(source)) if p)
            job = Job(type=JobType.render, payload={**payload, "source_id": source.id, "count": 1,
                "duration": config["clip_duration"], "caption": caption})
    db.add(job)
    db.flush()
    if platform == "tiktok_web" and account_id and job.type == JobType.render:
        job.payload = {**job.payload, "pipeline_id": job.id}
    plan.last_pipeline_id = job.id
    plan.status = "running"
    plan.last_error = None
    plan.config = {**config, "query_index": int(config.get("query_index", 0)) + 1}
    db.commit()
    return True
