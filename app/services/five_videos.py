"""Five shared clips, durable video-major rotation, and independent draft editing."""
import copy
from datetime import timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, update
from ..models import Account, AutomationPlan, Clip, Job, JobType, RecordStatus, SourceVideo, SourceClipUse, TikTokWebPublication, utcnow
from .publishing import enqueue_tiktok_web, file_hash
from .tiktok_web import canonical_video_url
from .source_identity import source_key, exhausted_download
from .editing import editing_snapshot, capture_editing, parse_editing, EditingSetupError

SEARCH_GENERATION_VERSION = 2


def save_draft(db, videos, account_ids, *, queries=None, topic=None):
    if len(videos) != 5 or len(account_ids) > 30 or len(set(account_ids)) != len(account_ids):
        raise ValueError('Нужно пять роликов и не больше 30 разных аккаунтов')
    for aid in account_ids:
        account = db.get(Account, aid)
        if not account or account.platform != 'tiktok_web':
            raise ValueError('Выберите существующие аккаунты TikTok')
    for _ in range(3):
        db.expire_all()
        plan = db.get(AutomationPlan, 1)
        if plan is None:
            plan = AutomationPlan(id=1, enabled=False, status='stopped', config={})
            db.add(plan); db.flush()
        previous = copy.deepcopy(plan.config)
        batch = previous.get('five_video_batch')
        if batch and batch.get('status') not in {'complete', 'rejected'}:
            descriptions = [v['description'].strip() for v in videos]
            if not all(descriptions) or len(set(descriptions)) != 5:
                raise ValueError('Для начатой партии нужны пять разных непустых описаний')
            if plan.enabled and account_ids != batch['account_ids']:
                raise ValueError('Чтобы выбрать другие аккаунты, остановите 24/7')
            if any(v.get('clip_id') != old.get('clip_id') for v, old in zip(videos, previous['five_video_draft']['videos'])):
                raise ValueError('Ролики уже начатой партии менять нельзя')
        draft = {**previous.get('five_video_draft', {}), 'videos': videos, 'account_ids': account_ids}
        if queries is not None:
            draft['queries'] = queries
        if topic is not None:
            draft['topic'] = topic
        changed = db.execute(update(AutomationPlan).where(AutomationPlan.id == 1, AutomationPlan.config == previous)
            .values(config={**previous, 'five_video_draft': draft}).execution_options(synchronize_session=False))
        if changed.rowcount == 1:
            db.commit(); db.expire_all()
            return
        db.rollback()
    raise ValueError('Очередь изменилась одновременно. Сохраните ещё раз.')


def retryable_search(db, batch):
    """Retry only before media ownership or a publication could have been created."""
    from .scheduler import pipeline_jobs
    if batch.get('pending_job_id') or not batch.get('prepare_pipeline_id'):
        return False
    index = batch.get('position', 0) // len(batch['account_ids'])
    if index >= 5 or batch['clip_ids'][index]:
        return False
    family = pipeline_jobs(db, batch['prepare_pipeline_id'])
    search_only = bool(len(family) == 1 and family[0].type == JobType.discovery
                and family[0].status == RecordStatus.error
                and not (family[0].result or {}).get('created_source_ids')
                and (family[0].result or {}).get('error_kind') not in {'manual_verification', 'platform_policy', 'session_expired'}
                and not db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id == family[0].id)))
    if search_only:
        return True
    root = next((j for j in family if j.id == batch['prepare_pipeline_id']), None)
    children = [j for j in family if j is not root]
    if not root or root.type != JobType.discovery or root.status != RecordStatus.ready or not children:
        return False
    for child in children:
        kind = (child.result or {}).get('error_kind')
        if kind in {'manual_verification', 'platform_policy', 'session_expired'}:
            return False
        cancelled = kind == 'automation_stopped' or child.error == 'Режим 24/7 остановлен; новая операция не запущена'
        changed_editing = (kind == 'first_result_unusable' and
            parse_editing(child.payload.get('auto_render', {}).get('editing')).model_dump() != editing_snapshot(db))
        if child.type != JobType.download or child.status != RecordStatus.error or not (cancelled or changed_editing or exhausted_download(child)):
            return False
    source_ids = {j.payload.get('source_id') for j in children} - {None}
    return not (db.scalar(select(Clip.id).where(Clip.source_id.in_(source_ids))) or
                db.scalar(select(SourceClipUse.source_key).where(SourceClipUse.source_id.in_(source_ids))) or
                db.scalar(select(TikTokWebPublication.id).where(TikTokWebPublication.job_id.in_([j.id for j in family]))))


def start_batch(db, *, interval_minutes, daily_limit, clip_duration, queries, topic, visibility):
    from .scheduler import pipeline_jobs
    plan = db.get(AutomationPlan, 1)
    if not plan or plan.enabled:
        raise ValueError('Сначала сохраните пять описаний и остановите текущий режим')
    if plan.last_pipeline_id and any(j.status in {RecordStatus.queued, RecordStatus.processing} for j in pipeline_jobs(db, plan.last_pipeline_id)):
        raise ValueError('Дождитесь завершения текущей операции')
    draft = plan.config.get('five_video_draft', {})
    videos, ids = draft.get('videos', []), draft.get('account_ids', [])
    if any(not v.get('clip_id') for v in videos):
        capture_editing(db)
    descriptions = [v['description'].strip() for v in videos]
    if len(videos) != 5 or len(set(descriptions)) != 5 or not all(descriptions) or not ids:
        raise ValueError('Сохраните пять разных непустых описаний и выберите аккаунты')
    for aid in ids:
        a = db.get(Account, aid)
        if not a or a.platform != 'tiktok_web' or a.status != RecordStatus.ready:
            raise ValueError('Каждому выбранному аккаунту нужен подтверждённый вход')
    batch = copy.deepcopy(plan.config.get('five_video_batch'))
    history = copy.deepcopy(plan.config.get('five_video_batch_history', []))
    if batch and batch.get('status') == 'rejected':
        pending = db.get(Job, batch['pending_job_id']) if batch.get('pending_job_id') else None
        if batch.get('pending_job_id') and (not pending or not confirmed(db, pending)):
            raise ValueError('Сначала выясните результат последней отправки в журнале; новая партия не запущена')
        history.append({**batch, 'archived_at': utcnow().isoformat(),
            'archive_reason': 'Owner rejected the actual clip',
            'queries': copy.deepcopy(plan.config.get('queries', [])), 'topic': plan.config.get('topic', 'any')})
        batch = None
    retry_search = False
    if batch and batch['status'] != 'complete':
        if batch.get('pending_job_id'):
            job = db.get(Job, batch['pending_job_id'])
            if job and job.status in {RecordStatus.error, RecordStatus.reauth_required} and not confirmed(db, job):
                raise ValueError('Сначала выясните результат последней отправки в журнале; повторная загрузка отключена')
        legacy_automatic_media = (any(not v.get('clip_id') for v in videos)
                                  and batch.get('search_generation_version') != SEARCH_GENERATION_VERSION)
        selection_changed = (batch['account_ids'] != ids or plan.config.get('queries') != queries
                             or plan.config.get('topic', 'any') != topic or legacy_automatic_media)
        retry_search = retryable_search(db, batch)
        if selection_changed:
            untouched_search = (retry_search and batch.get('position') == 0 and not batch.get('completed')
                                and not batch.get('skipped') and not any(batch['clip_ids']))
            if plan.status != 'stopped' and not untouched_search:
                raise ValueError('Смена аккаунтов или поиска не снимает причину паузы предыдущей партии')
            history.append({**batch, 'archived_at': utcnow().isoformat(),
                'archive_reason': 'Legacy automatic media predates query-bound search' if legacy_automatic_media else 'New account or search selection',
                'queries': copy.deepcopy(plan.config.get('queries', [])),
                'topic': plan.config.get('topic', 'any')})
            batch = None
        elif retry_search:
            # Keep the failed preparation as evidence; the next tick creates a new discovery.
            batch['prepare_pipeline_id'] = None
        elif batch.get('prepare_pipeline_id') and plan.status == 'paused':
            raise ValueError('Сначала устраните причину остановки подготовки в журнале')
    if not batch or batch['status'] == 'complete':
        clips = [v.get('clip_id') for v in videos]
        chosen = [c for c in clips if c]
        if len(chosen) != len(set(chosen)):
            raise ValueError('На один аккаунт нельзя отправлять один и тот же ролик дважды')
        hashes = []
        selected_clips = []
        for cid in chosen:
            clip = db.get(Clip, cid)
            if not clip or clip.status != RecordStatus.ready or not clip.local_path or not Path(clip.local_path).is_file():
                raise ValueError('Выбранный ролик не готов')
            source = db.get(SourceVideo, clip.source_id)
            if not source or source.archived:
                raise ValueError('Архивный исходник нельзя использовать в новой партии')
            if source and source.source_url in plan.config.get('rejected_source_urls', []):
                raise ValueError('Выбранный ролик использует отклонённый исходник. Выберите другой файл.')
            hashes.append(file_hash(Path(clip.local_path)))
            for other in selected_clips:
                other_source = db.get(SourceVideo, other.source_id)
                if source_key(other_source.source_url, other_source.id) == source_key(source.source_url, source.id):
                    raise ValueError('Из одного исходника допускается только один клип')
            selected_clips.append(clip)
        if len(set(hashes)) != len(hashes):
            raise ValueError('Выбраны одинаковые видеофайлы')
        batch = {'id': str(uuid4()), 'status': 'running', 'account_ids': ids, 'clip_ids': clips,
                 'clip_hashes': [None] * 5,
                 'position': 0, 'completed': [], 'pending_job_id': None, 'prepare_pipeline_id': None,
                 'started_at': utcnow().isoformat(), 'search_generation_version': SEARCH_GENERATION_VERSION}
    batch['status'] = 'running'
    plan.config = {**plan.config, 'five_video_batch': batch,
        'five_video_draft': {**draft, 'queries': queries, 'topic': topic}, 'platform': 'tiktok_web', 'publish': True,
        'account_ids': ids, 'interval_minutes': interval_minutes, 'daily_limit': daily_limit,
        'clip_duration': clip_duration, 'queries': queries, 'topic': topic, 'visibility': visibility}
    if history:
        plan.config = {**plan.config, 'five_video_batch_history': history}
    plan.enabled = True; plan.status = 'scheduled'; plan.last_error = None
    plan.next_run_at = utcnow()
    db.commit()


def confirmed(db, job):
    result = job.result or {}
    receipt = db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id == job.id))
    url = canonical_video_url(result.get('video_url') or '')
    return bool(receipt and receipt.account_id == job.payload.get('account_id') and receipt.stage == 'published'
                and result.get('confirmed') is True and url and canonical_video_url(receipt.video_url or '') == url)


def tick(db, plan):
    """Called instead of the open-ended scheduler only for an explicit five-clip run."""
    from .scheduler import pause, pipeline_jobs, expired_session_before_post
    from ..config import settings
    import shutil
    previous = copy.deepcopy(plan.config)
    # Obtain the same SQLite write lock/CAS used by the normal scheduler.
    lock = db.execute(update(AutomationPlan).where(AutomationPlan.id == 1,
        AutomationPlan.enabled.is_(True), AutomationPlan.config == previous)
        .values(config=previous).execution_options(synchronize_session=False))
    if lock.rowcount != 1:
        db.rollback(); return False
    batch = copy.deepcopy(previous['five_video_batch'])
    config = previous

    def persist():
        plan.config = {**config, 'five_video_batch': batch}
        db.commit()

    def stop_error(reason):
        pause(plan, reason); batch['status'] = 'paused'; persist(); return False

    if batch.get('status') == 'rejected':
        pause(plan, 'Фрагмент отклонён владельцем; продолжение этой партии отключено')
        persist(); return False

    pending = db.get(Job, batch.get('pending_job_id')) if batch.get('pending_job_id') else None
    if pending:
        if pending.status in {RecordStatus.queued, RecordStatus.processing}:
            db.rollback(); return False
        created = confirmed(db, pending)
        if not created:
            expired_session = expired_session_before_post(db, pending)
            result = pending.result or {}
            account = db.get(Account, pending.payload.get('account_id'))
            receipt = db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id == pending.id))
            can_skip = (batch.get('skip_post_error_21') is True and result.get('platform_post_code') == 21
                and not result.get('requires_owner_review') and not result.get('platform_error_code')
                and result.get('error_kind') != 'manual_verification' and pending.status == RecordStatus.error
                and account and account.status == RecordStatus.ready and receipt and receipt.stage == 'submitting'
                and not receipt.video_url and not result.get('confirmed'))
            if not can_skip and not expired_session:
                return stop_error('Партия остановлена: ' + (pending.error or 'результат отправки не подтверждён'))
            skipped = batch.setdefault('skipped', [])
            if not any(s['job_id'] == pending.id for s in skipped):
                skipped.append({'job_id': pending.id, 'position': batch['position'],
                    'account_id': pending.payload['account_id'], 'round': pending.payload['five_video_round'],
                    'clip_id': pending.payload['clip_id'], 'skipped_at': utcnow().isoformat(),
                    'reason': 'Session expired before Post' if expired_session else 'Owner-enabled skip of post error 21; no repeated upload',
                    'platform_post_code': None if expired_session else 21, 'result_unknown': not expired_session, 'upload_must_not_repeat': True})
                if expired_session:
                    batch.setdefault('expired_session_accounts', {})[pending.payload['account_id']] = pending.id
                batch['position'] += 1
            plan.last_error = None
        elif pending.id not in batch['completed']:
            url = canonical_video_url((pending.result or {}).get('video_url') or '')
            if any(canonical_video_url(((db.get(Job, jid).result or {}).get('video_url') or '')) == url for jid in batch['completed']):
                return stop_error('TikTok вернул уже учтённую ссылку. Счётчик не увеличен; проверьте журнал.')
            batch['completed'].append(pending.id)
            batch['position'] += 1
        batch['pending_job_id'] = None
        if (pending.result or {}).get('public_visibility_confirmed') is False:
            plan.last_error = 'Ролик создан; видимость «Для всех» пока не подтверждена. Повторно файл не отправляется.'
    total = len(batch['account_ids']) * 5
    if batch['position'] >= total:
        batch['status'] = 'complete'; plan.enabled = False; plan.status = 'stopped'
        batch['finished_at'] = utcnow().isoformat(); persist(); return False
    round_index, account_index = divmod(batch['position'], len(batch['account_ids']))
    aid = batch['account_ids'][account_index]
    if aid in batch.get('expired_session_accounts', {}):
        batch.setdefault('skipped', []).append({'job_id': None, 'position': batch['position'],
            'account_id': aid, 'round': round_index + 1, 'reason': 'Session expired earlier in this batch',
            'expired_session_job_id': batch['expired_session_accounts'][aid], 'skipped_at': utcnow().isoformat(),
            'result_unknown': False, 'upload_must_not_repeat': True})
        batch['position'] += 1; persist(); return False
    preparation = batch.get('prepare_pipeline_id')
    if preparation:
        family = pipeline_jobs(db, preparation)
        if any(j.status in {RecordStatus.queued, RecordStatus.processing} for j in family):
            persist(); return False
        by_id = {j.id: j for j in family}
        failure = next((j for j in family if j.status in {RecordStatus.error, RecordStatus.reauth_required}
            and (j.result or {}).get('fallback_job_id') not in by_id), None)
        if failure:
            return stop_error('Подготовка ролика остановлена: ' + (failure.error or 'ошибка поиска/монтажа'))
        clips = [cid for j in family for cid in (j.result or {}).get('clip_ids', [])]
        if not clips:
            return stop_error('Монтаж завершился без готового ролика')
        batch['clip_ids'][round_index] = clips[0]
        batch['prepare_pipeline_id'] = None
    cid = batch['clip_ids'][round_index]
    if not cid:
        storage = settings.media_root if settings.media_root.exists() else settings.media_root.parent
        if shutil.disk_usage(storage).free < 3 * 1024**3:
            return stop_error('Свободно меньше 3 ГБ; освободите место')
        offset = round_index % len(config['queries'])
        queries = config['queries'][offset:] + config['queries'][:offset]
        try:
            editing = capture_editing(db)
        except EditingSetupError as exc:
            return stop_error(str(exc))
        payload = {'query': queries[0], 'queries': queries, 'max_results': 1, 'editing': editing,
            'clip_count': 1, 'clip_duration': config['clip_duration'], 'duration_filter': 'any',
            'unique_tiktok_excerpt': True, 'single_source': True, 'topic': config['topic'], 'automation_plan_id': 1,
            'caption': '', 'hashtags': '', 'exclude_source_urls': list(config.get('rejected_source_urls', [])), 'visibility': config['visibility']}
        # Only discovery can establish that a saved source matches this query.
        job = Job(type=JobType.discovery, payload=payload)
        db.add(job); db.flush(); job.payload = {**job.payload, 'pipeline_id': job.id}
        batch['prepare_pipeline_id'] = job.id; plan.last_pipeline_id = job.id
        plan.status = 'running'; persist(); return True
    clip = db.get(Clip, cid)
    if not clip or clip.status != RecordStatus.ready or not clip.local_path or not Path(clip.local_path).is_file():
        return stop_error('Файл ролика недоступен; повторная генерация уже отправленного ролика отключена')
    # Check the five actual files, including explicitly chosen clips, before reuse.
    digest = file_hash(Path(clip.local_path))
    hashes = batch.setdefault('clip_hashes', [None] * 5)
    if hashes[round_index] and hashes[round_index] != digest:
        return stop_error('Файл ролика изменился после начала круга. Отправка остановлена.')
    hashes[round_index] = digest
    for i, other_id in enumerate(batch['clip_ids']):
        if i == round_index or not other_id:
            continue
        other = db.get(Clip, other_id)
        if other and other.source_id == clip.source_id:
            return stop_error('Два ролика используют одинаковый фрагмент исходника. Отправка остановлена.')
        if other and other.local_path and file_hash(Path(other.local_path)) == digest:
            return stop_error('Два ролика партии совпадают. Отправка остановлена.')
    from .clip_review import approved
    if not approved(batch, cid, digest):
        plan.enabled = False; plan.status = 'review_required'; batch['status'] = 'awaiting_review'
        plan.last_error = f'Просмотрите видео {round_index + 1} и подтвердите содержание кадра перед отправкой. Название исходника не подтверждает содержание.'
        persist(); return False
    now = utcnow()
    if plan.next_run_at and plan.next_run_at.replace(tzinfo=timezone.utc) > now:
        plan.status = 'scheduled'; persist(); return False
    aid = batch['account_ids'][account_index]
    account = db.get(Account, aid)
    if not account or account.status != RecordStatus.ready:
        return stop_error('Аккаунту требуется вход. Порядок аккаунтов сохранён, замена отключена.')
    if config.get('daily_limit'):
        receipts = db.scalars(select(TikTokWebPublication).where(TikTokWebPublication.account_id == aid,
            TikTokWebPublication.created_at >= now - timedelta(hours=24))).all()
        if len(receipts) >= config['daily_limit']:
            plan.status = 'quota_wait'; plan.next_run_at = now + timedelta(hours=1); persist(); return False
    description = config['five_video_draft']['videos'][round_index]['description'].strip()
    source = db.get(SourceVideo, clip.source_id)
    if source and source.source_url.startswith('https://'):
        from .youtube import source_attribution
        basis = 'creative_commons' if 'Creative Commons Attribution;' in (clip.caption or '') else 'author_permission'
        description += '\n' + source_attribution(source, basis, title_limit=150)
    try:
        job = enqueue_tiktok_web(db, account_id=aid, clip=clip, description=description,
            visibility=config['visibility'], unique_across_accounts=False)
    except ValueError as exc:
        return stop_error(str(exc))
    job.payload = {**job.payload, 'automation_plan_id': 1, 'five_video_batch_id': batch['id'],
        'five_video_round': round_index + 1, 'five_video_account_position': account_index + 1}
    batch['pending_job_id'] = job.id; plan.last_pipeline_id = job.id; plan.status = 'running'
    plan.next_run_at = now + timedelta(minutes=config['interval_minutes'])
    persist(); return True
