"""Human review of the actual rendered file, independent of its search title."""
import copy
from pathlib import Path
from sqlalchemy import select, update
from ..models import AutomationPlan, Clip, Job, RecordStatus, SourceVideo, utcnow
from .publishing import file_hash


def approved(batch, clip_id, digest):
    review = batch.get('clip_reviews', {}).get(clip_id, {})
    return review.get('decision') == 'approved' and review.get('sha256') == digest


def review_clip(db, *, batch_id, clip_id, decision, watched=False):
    if decision not in {'approved', 'rejected'} or (decision == 'approved' and not watched):
        raise ValueError('Просмотрите сам фрагмент и подтвердите его содержание')
    for _ in range(3):
        db.expire_all()
        plan = db.get(AutomationPlan, 1)
        previous = copy.deepcopy(plan.config) if plan else {}
        batch = copy.deepcopy(previous.get('five_video_batch', {}))
        if batch.get('id') != batch_id or clip_id not in batch.get('clip_ids', []):
            raise ValueError('Партия или выбранный ролик уже изменились. Обновите страницу.')
        pending = db.get(Job, batch['pending_job_id']) if batch.get('pending_job_id') else None
        if pending and pending.status in {RecordStatus.queued, RecordStatus.processing}:
            raise ValueError('Дождитесь результата начатой отправки; она не будет повторена')
        if batch.get('status') == 'rejected' and decision == 'approved':
            raise ValueError('Отклонённую партию нельзя возобновить; начните новую')
        clip = db.get(Clip, clip_id)
        if not clip or clip.status != RecordStatus.ready or not clip.local_path or not Path(clip.local_path).is_file():
            raise ValueError('Готовый файл ролика недоступен')
        review = {'decision': decision, 'sha256': file_hash(Path(clip.local_path)),
                  'reviewed_at': utcnow().isoformat(), 'watched': bool(watched)}
        batch['clip_reviews'] = {**batch.get('clip_reviews', {}), clip_id: review}
        values = {}
        config = {**previous, 'five_video_batch': batch}
        if decision == 'rejected':
            source = db.get(SourceVideo, clip.source_id)
            excluded = list(previous.get('rejected_source_urls', []))
            if source and source.source_url.startswith('https://') and source.source_url not in excluded:
                excluded.append(source.source_url)
            batch['status'] = 'rejected'
            config['rejected_source_urls'] = excluded
            values = {'enabled': False, 'status': 'stopped',
                      'last_error': 'Фрагмент отклонён владельцем. Исходник исключён; для другого ролика начните новую партию.'}
        changed = db.execute(update(AutomationPlan).where(AutomationPlan.id == 1,
            AutomationPlan.config == previous).values(config=config, **values)
            .execution_options(synchronize_session=False))
        if changed.rowcount == 1:
            db.commit(); db.expire_all()
            return
        db.rollback()
    raise ValueError('Очередь изменилась одновременно. Обновите страницу и повторите проверку.')
