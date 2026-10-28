"""Saved editing preferences; every render job receives an immutable snapshot."""
import math
import random
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import update
from ..config import settings
from ..models import AutomationPlan


class EditingOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    automatic: bool = False
    start_seconds: float = Field(default=0, ge=0, le=86400)
    end_seconds: float | None = Field(default=None, gt=0, le=86400)
    speed: float = Field(default=1, ge=0.5, le=2)
    quality: Literal['1080p','720p','480p'] = '1080p'
    music_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')

    @model_validator(mode='after')
    def ordered_range(self):
        if not self.automatic and self.end_seconds is not None and self.end_seconds <= self.start_seconds:
            raise ValueError('Конец фрагмента должен быть позже начала')
        return self


def editing_snapshot(db):
    plan = db.get(AutomationPlan, 1)
    return EditingOptions.model_validate((plan.config or {}).get('editing', {}) if plan else {}).model_dump()


class EditingSetupError(ValueError):
    pass


def parse_editing(data):
    return EditingOptions.model_validate({k:v for k,v in (data or {}).items() if k not in ('music_pool','automatic_profile')})


def capture_editing(db):
    snapshot = editing_snapshot(db)
    if snapshot['automatic']:
        snapshot['automatic_profile'] = 2
        plan = db.get(AutomationPlan,1)
        pool = []
        for track in (plan.config or {}).get('music_library', []) if plan else []:
            try:
                music_path(track['id'])
                pool.append(track['id'])
            except ValueError:
                continue
        snapshot['music_pool'] = pool
    return snapshot


def validate_automatic_source(total):
    if not math.isfinite(total) or total < 10:
        raise ValueError('Для автоматического фрагмента 10–15 секунд нужен исходник не короче 10 секунд.')


def resolve_automatic(total, width, height, pool, *, rng=None, profile_version=1):
    """Both the source excerpt and the final duration stay within 10–15 seconds."""
    validate_automatic_source(total)
    if profile_version == 2:
        rng = rng or random.SystemRandom()
        # Reserve 1–2 s at either end when length permits. A 10 s input cannot
        # lose 2 s while still producing the required 10–15 s excerpt.
        margin = min(1, max(0,(total-10)/2))
        edit = resolve_automatic(total-2*margin,width,height,pool,rng=rng)
        usable = total-2*margin
        speeds = [n/1000 for n in range(950,1051) if abs(n-1000)>=10 and usable>=max(10,10*n/1000)]
        speed = rng.choice(speeds)
        low = math.ceil(max(10,10/speed)*1000 - 1e-8)
        high = math.floor(min(15,15/speed,usable/speed)*1000 + 1e-8)
        duration = rng.randint(low,high)/1000 if low<=high else (max(10,10/speed)+min(15,15/speed,usable/speed))/2
        edit.update(speed=speed,duration_seconds=duration)
        excerpt = edit['duration_seconds']*edit['speed']
        room = max(0,total-excerpt)
        head = rng.uniform(margin,min(2,room/2))
        tail = rng.uniform(margin,min(2,room-head))
        start = rng.uniform(head,max(head,total-tail-excerpt))
        edit.update(start_seconds=round(start,6),quality='1080p',profile_version=2,
            trim_head_seconds=round(head,6),trim_tail_seconds=round(tail,6))
        return edit
    available = []
    for identity in pool:
        try:
            music_path(identity)
            available.append(identity)
        except ValueError:
            continue
    rng = rng or random.SystemRandom()
    speeds = [n/1000 for n in range(980,1031) if n!=1000 and total >= max(10,10*n/1000)]
    speed = rng.choice(speeds)
    low = math.ceil(max(10,10/speed)*1000 - 1e-8)
    high = math.floor(min(15,15/speed,total/speed)*1000 + 1e-8)
    duration = rng.randint(low,high)/1000 if low<=high else (max(10,10/speed)+min(15,15/speed,total/speed))/2
    source_duration = duration*speed
    start = math.floor(rng.uniform(0,max(0,total-source_duration))*1000)/1000
    source_quality = min(width,height)
    choices = [q for q,size in [('1080p',1080),('720p',720),('480p',480)] if size!=source_quality]
    return {'start_seconds':start,'duration_seconds':duration,'speed':speed,
        'quality':rng.choice(choices),'music_id':rng.choice(available) if available else None,'automatic':True}


def music_path(music_id):
    # Validate before constructing any path, including old or manually retried jobs.
    EditingOptions(music_id=music_id)
    root = settings.music_dir.resolve()
    path = root / (music_id + '.m4a')
    if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
        raise ValueError('Музыка недоступна. Загрузите трек и сохраните настройки монтажа.')
    return path


def editing_state(db):
    plan = db.get(AutomationPlan, 1)
    library = (plan.config or {}).get('music_library', []) if plan else []
    return {'settings': editing_snapshot(db), 'tracks': [
        {'id':t['id'],'name':t['name'],'duration_seconds':t['duration_seconds'],
         'preview_url':f"/api/editing/music/{t['id']}"} for t in library]}


def update_editing(db, options, *, track=None):
    if options.music_id:
        music_path(options.music_id)
    for _ in range(3):
        db.expire_all()
        plan = db.get(AutomationPlan, 1)
        if plan is None:
            plan = AutomationPlan(id=1,enabled=False,status='stopped',config={})
            db.add(plan);db.flush()
        previous = dict(plan.config or {})
        library = list(previous.get('music_library', []))
        if track:
            library.append(track)
        if options.music_id and options.music_id not in {t['id'] for t in library}:
            raise ValueError('Выберите музыку из загруженных треков')
        changed = db.execute(update(AutomationPlan).where(AutomationPlan.id==1,AutomationPlan.config==previous)
            .values(config={**previous,'editing':options.model_dump(),'music_library':library})
            .execution_options(synchronize_session=False))
        if changed.rowcount == 1:
            db.commit();db.expire_all();return editing_state(db)
        db.rollback()
    raise ValueError('Настройки изменились одновременно. Сохраните ещё раз.')


def excerpt_plan(total, requested_duration, options):
    start = options.start_seconds
    stop = options.end_seconds if options.end_seconds is not None else total
    if not all(math.isfinite(v) for v in (total, requested_duration)) or requested_duration <= 0:
        raise ValueError('Некорректная длина исходника или клипа')
    if start >= total or stop > total + 0.25:
        raise ValueError('Выбранный фрагмент выходит за длину исходника')
    needed = requested_duration * options.speed
    if options.end_seconds is None and start + needed > total + 0.25:
        raise ValueError('Исходник слишком короткий для выбранного начала, скорости и длины клипа')
    duration = min(needed, stop - start) / options.speed
    if duration <= 0 or duration > 180:
        raise ValueError('Итоговый клип должен быть длиннее нуля и не длиннее 180 секунд')
    return start, duration


def has_edits(options):
    return options != EditingOptions().model_dump()


def direct_duration(total, options):
    edit = parse_editing(options)
    if edit.automatic:
        validate_automatic_source(total)
        return min(15,total)
    stop = edit.end_seconds if edit.end_seconds is not None else total
    requested = min(180, (stop - edit.start_seconds) / edit.speed)
    return excerpt_plan(total, requested, edit)[1]
