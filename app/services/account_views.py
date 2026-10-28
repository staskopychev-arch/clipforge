"""Durable hourly statistics, independent from the publishing plan and queue."""
import hashlib
import logging
import uuid
from datetime import timedelta,timezone
from threading import Event,Thread

from sqlalchemy import delete,or_,select,update
from sqlalchemy.exc import IntegrityError
from ..db import SessionLocal
from ..config import settings
from ..models import Account,AccountViews,AccountViewSample,Job,RecordStatus,utcnow
from .tiktok_web import TikTokWebSessionError,TikTokWebPolicyError

log=logging.getLogger(__name__)
INTERVAL=timedelta(hours=1)
PAUSED={'needs_login','manual_check'}

def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

def stamp(value):
    return aware(value).isoformat() if value else None

def fingerprint(account):
    return hashlib.sha256(account.access_token.encode()).hexdigest()

def ensure_rows(db):
    accounts=db.scalars(select(Account).where(Account.platform=='tiktok_web')).all()
    known={r.account_id:r for r in db.scalars(select(AccountViews)).all()}
    for account in accounts:
        row=known.get(account.id)
        if row is None:
            db.add(AccountViews(account_id=account.id,approximate=False,complete=False,status='pending'))
        elif row.status in PAUSED and row.session_hash!=fingerprint(account):
            row.status='pending';row.error=None;row.next_check_at=None
    try:db.commit()
    except IntegrityError:db.rollback() # another process may have inserted the row

def views_state(db):
    rows={r.account_id:r for r in db.scalars(select(AccountViews)).all()}
    output=[];now=utcnow()
    for account in db.scalars(select(Account).where(Account.platform=='tiktok_web').order_by(Account.created_at.desc())).all():
        r=rows.get(account.id)
        status=r.status if r else 'pending'
        if account.status!=RecordStatus.ready:status='needs_login' if account.status==RecordStatus.reauth_required else 'unavailable'
        if status=='checking' and (not r.lease_until or aware(r.lease_until)<now):status='pending'
        previous=db.scalar(select(AccountViewSample).where(AccountViewSample.account_id==account.id,
            AccountViewSample.checked_at<r.checked_at).order_by(AccountViewSample.checked_at.desc()).limit(1)) if r and r.checked_at else None
        delta=r.views-previous.views if r and r.views is not None and previous and r.profile_url and r.profile_url==previous.profile_url and r.complete and previous.complete and not r.approximate and not previous.approximate else None
        output.append({'account_id':account.id,'display_name':account.display_name,'views':r.views if r else None,
            'video_count':r.video_count if r else None,'approximate':r.approximate if r else False,
            'complete':r.complete if r else False,'profile_url':r.profile_url if r else None,'delta':delta,
            'checked_at':stamp(r.checked_at) if r else None,'attempted_at':stamp(r.attempted_at) if r else None,
            'next_check_at':stamp(r.next_check_at) if r else None,'status':status,'error':r.error if r else None})
    return {'interval_seconds':3600,'enabled':settings.account_views_enabled,'accounts':output,'server_time':stamp(now),
        'checked':sum(r['checked_at'] is not None for r in output),'checking':sum(r['status']=='checking' for r in output)}

def request_refresh(db):
    ensure_rows(db);now=utcnow()
    # Explicit refresh is coalesced; it never starts another concurrent browser
    # or repeats a security challenge without a new saved session.
    for row in db.scalars(select(AccountViews)).all():
        if row.status not in PAUSED and (not row.attempted_at or aware(row.attempted_at)<now-timedelta(minutes=5)):
            if not row.lease_until or aware(row.lease_until)<now:row.next_check_at=None
    db.commit()
    return views_state(db)

def run_due_once(session_factory=SessionLocal,collector=None,now=None):
    now=now or utcnow();lease=str(uuid.uuid4())
    with session_factory() as db:
        ensure_rows(db)
        busy={}
        for job in db.scalars(select(Job).where(Job.archived.is_(False),Job.status.in_([RecordStatus.queued,RecordStatus.processing]))).all():
            for key in ('account_id','tiktok_web_account_id','youtube_account_id'):
                if job.payload.get(key):busy[job.payload[key]]=True
        candidates=db.execute(select(AccountViews,Account).join(Account,Account.id==AccountViews.account_id).where(
            Account.platform=='tiktok_web',Account.status==RecordStatus.ready,AccountViews.status.not_in(PAUSED),
            or_(AccountViews.next_check_at.is_(None),AccountViews.next_check_at<=now,AccountViews.status=='checking'),
            or_(AccountViews.lease_until.is_(None),AccountViews.lease_until<=now)
            ).order_by(AccountViews.next_check_at,Account.created_at.desc())).all()
        chosen=None
        for row,account in candidates:
            if account.id in busy:continue
            changed=db.execute(update(AccountViews).where(AccountViews.account_id==account.id,
                or_(AccountViews.lease_until.is_(None),AccountViews.lease_until<=now),
                or_(AccountViews.next_check_at.is_(None),AccountViews.next_check_at<=now,AccountViews.status=='checking')).values(
                    lease_token=lease,lease_until=now+timedelta(minutes=5),status='checking',attempted_at=now,
                    next_check_at=now+INTERVAL,session_hash=fingerprint(account)).execution_options(synchronize_session=False))
            if changed.rowcount:
                chosen=(account.id,account.access_token,fingerprint(account));break
        db.commit()
    if not chosen:return False
    aid,token,session_hash=chosen
    try:
        if collector is None:
            from .tiktok_views import collect_account_views
            collector=collect_account_views
        result=collector(token)
        if not isinstance(result.get('views'),int) or isinstance(result['views'],bool) or not 0<=result['views']<=9223372036854775807:
            raise ValueError('invalid statistics')
        if not isinstance(result.get('video_count'),int) or result['video_count']<0:raise ValueError('invalid statistics')
        error=None;status='ready' if result['complete'] else 'partial'
    except TikTokWebPolicyError:
        result=None;error='TikTok требует ручной проверки доступа. Обновите вход после проверки в Studio.';status='manual_check'
    except TikTokWebSessionError:
        result=None;error='TikTok требует входа или ручной проверки. Сохраните обновлённые cookie.';status='needs_login'
    except Exception:
        result=None;error='Не удалось прочитать просмотры из Studio. Последний успешный результат сохранён; повтор через час.';status='error'
    with session_factory() as db:
        row=db.get(AccountViews,aid);account=db.get(Account,aid)
        if not row or row.lease_token!=lease:return True
        if not account or fingerprint(account)!=session_hash:
            row.status='pending';row.next_check_at=None;row.error=None
        else:
            row.status=status;row.error=error
            if result is not None:
                finished=utcnow()
                row.views=result['views'];row.video_count=result['video_count'];row.approximate=result.get('approximate',False)
                row.complete=result['complete'];row.profile_url=result.get('profile_url');row.checked_at=finished
                db.add(AccountViewSample(account_id=aid,views=row.views,video_count=row.video_count,approximate=row.approximate,
                    complete=row.complete,profile_url=row.profile_url,checked_at=finished))
                db.execute(delete(AccountViewSample).where(AccountViewSample.account_id==aid,AccountViewSample.checked_at<finished-timedelta(days=30)))
        row.lease_token=None;row.lease_until=None;db.commit()
    return True

class ViewsMonitor:
    def __init__(self):self.stop_event=Event();self.thread=None
    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop_event.clear();self.thread=Thread(target=self.run,name='account-views',daemon=True);self.thread.start()
    def run(self):
        while not self.stop_event.is_set():
            try:worked=run_due_once()
            except Exception:
                log.warning('Account view scheduler unavailable; retrying shortly');worked=False
            self.stop_event.wait(1 if worked else 15)
    def stop(self):self.stop_event.set()

views_monitor=ViewsMonitor()
