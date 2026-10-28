import secrets
import shutil
import json
import hashlib
from pathlib import Path
from threading import Event, Thread, Lock

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, Field, PositiveInt, field_validator
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db, init_db
from .models import Account, Clip, Job, JobType, RecordStatus, SourceVideo, YouTubePublication, TikTokWebPublication
from .services.media import probe_duration, probe_video_stream
from .services.source_identity import existing_clip
from .services.editing import EditingOptions, editing_snapshot, editing_state, update_editing, music_path, has_edits, direct_duration, capture_editing, EditingSetupError
from .services.media import prepare_music, MediaError
from .services.publishing import enqueue_youtube, enqueue_tiktok_web
from .services.tiktok_web import TikTokWebError, TikTokWebSessionError, TikTokWebPolicyError, check_session as check_tiktok_web_session
from .services.scheduler import plan_state, start_plan, stop_plan, update_plan_content
from .services.account_login import LoginQueueError, import_credentials, login_queue, parse_credentials
from .services.cookie_import import parse_tiktok_cookie_text
from .services.account_views import views_monitor,views_state,request_refresh
from .services.youtube_upload import YouTubeSessionError, check_session, connection_error
from .services.tiktok import (
    TikTokError,
    apply_token_data,
    authorization_url,
    creator_info,
    exchange_code,
    refresh_account,
)
from .security import create_session, encrypt, verify_password, verify_session
from .deployment_security import LoginLimiter, same_origin


app = FastAPI(title="ClipForge", version="1.0.0")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
signer = URLSafeTimedSerializer(settings.app_secret, salt="tiktok-oauth")


PUBLIC_PATHS = {"/login", "/health", "/oauth/tiktok/callback"}
login_limiter = LoginLimiter()
session_check_lock = Lock()


@app.exception_handler(EditingSetupError)
async def editing_setup_error(request, exc):
    return JSONResponse({'detail':str(exc)},status_code=409)


@app.middleware("http")
async def production_guards(request: Request, call_next):
    if settings.production_mode:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not same_origin(request.headers.get("origin", ""), settings.public_base_url):
                return JSONResponse({"detail": "Same-origin request required"}, status_code=403)
        if request.url.path == "/login" and request.method == "POST":
            if not login_limiter.allow(request.client.host if request.client else "unknown"):
                return JSONResponse({"detail": "Try login again in one minute"}, status_code=429, headers={"Retry-After": "60"})
    response = await call_next(request)
    if settings.production_mode:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        if request.url.path.startswith("/api/") or request.url.path in {"/", "/login"}:
            response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def protect_admin_panel(request: Request, call_next):
    path = request.url.path
    is_public = path in PUBLIC_PATHS or path.startswith("/static/")
    if not is_public and not verify_session(request.cookies.get("clipforge_session", "")):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


class SourceBatch(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)
    rights_confirmed: bool


class DiscoveryRequest(BaseModel):
    query: str = Field(min_length=2, max_length=180)
    max_results: int = Field(default=10, ge=1, le=20)
    clip_count: int = Field(default=1, ge=1, le=1)
    clip_duration: float = Field(default=20, ge=5, le=180)
    caption: str = Field(default="", max_length=1200)
    hashtags: str = Field(default="", max_length=350)
    duration_filter: str = Field(default="any", pattern="^(any|long|medium|short)$")


class TikTokTokenRecord(BaseModel):
    open_id: str = Field(min_length=1, max_length=255)
    display_name: str = Field(default="TikTok account", max_length=255)
    access_token: str = Field(min_length=8)
    refresh_token: str = Field(min_length=8)
    expires_in: int = Field(default=86400, ge=1)
    refresh_expires_in: int = Field(default=31536000, ge=1)


class PipelineRequest(DiscoveryRequest):
    youtube_account_id: str | None = None
    tiktok_web_account_id: str | None = None
    fresh_source: bool = False
    topic: str = Field(default="any", pattern="^any$")
    visibility: str = Field(default="private", pattern="^(private|unlisted|public)$")
    made_for_kids: bool = False
    consent: bool = False


class AutomationRequest(BaseModel):
    platform: str = Field(default="youtube", pattern="^(youtube|tiktok_web)$")
    topic: str = Field(default="any", pattern="^any$")
    queries: list[str] = Field(min_length=1, max_length=10)
    account_ids: list[str] = Field(default_factory=list, max_length=20)
    source_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    account_post_targets: dict[str, PositiveInt] | None = None
    interval_minutes: int = Field(default=360, ge=1)
    daily_limit: int = Field(default=0, ge=0)
    clip_count: int = Field(default=1, ge=1, le=1)
    clip_duration: float = Field(default=20, ge=5, le=60)
    caption: str = Field(default="", max_length=1200)
    hashtags: str = Field(default="", max_length=350)
    visibility: str = Field(default="private", pattern="^(private|unlisted|public)$")
    publish: bool = False
    consent: bool = False
    owns_channels: bool = False

    @field_validator("queries")
    @classmethod
    def clean_queries(cls, values):
        result = list(dict.fromkeys(value.strip() for value in values))
        if any(not 2 <= len(value) <= 180 for value in result):
            raise ValueError("Каждый поисковый запрос должен содержать 2–180 символов")
        return result

    @field_validator("account_ids")
    @classmethod
    def unique_accounts(cls, values):
        if any(not value or len(value) > 36 for value in values):
            raise ValueError("Неверный идентификатор аккаунта")
        return list(dict.fromkeys(values))


class AutomationContentRequest(BaseModel):
    model_config = {"extra": "forbid"}
    caption: str = Field(max_length=1200)
    hashtags: str = Field(max_length=350)


class FiveVideoItem(BaseModel):
    model_config = {"extra": "forbid"}
    description: str = Field(default="", max_length=1600)
    clip_id: str | None = Field(default=None, min_length=1, max_length=36)


class FiveVideoDraftRequest(BaseModel):
    model_config = {"extra": "forbid"}
    videos: list[FiveVideoItem] = Field(min_length=5, max_length=5)
    account_ids: list[str] = Field(default_factory=list, max_length=30)
    queries: list[str] | None = Field(default=None, max_length=10)
    topic: str | None = Field(default=None, pattern="^any$")

    @field_validator('queries')
    @classmethod
    def valid_queries(cls, values):
        return AutomationRequest.clean_queries(values) if values is not None else None


class FiveVideoStartRequest(BaseModel):
    model_config = {"extra": "forbid"}
    consent: bool
    interval_minutes: int = Field(default=3, ge=1)
    daily_limit: int = Field(default=10, ge=0)
    clip_duration: float = Field(default=15, ge=5, le=60)
    queries: list[str] = Field(min_length=1, max_length=10)
    topic: str = Field(default="any", pattern="^any$")
    visibility: str = Field(default="public", pattern="^(private|public)$")

    @field_validator('queries')
    @classmethod
    def valid_queries(cls, values):
        return AutomationRequest.clean_queries(values)


class ClipReviewRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    batch_id: str = Field(min_length=1, max_length=36)
    clip_id: str = Field(min_length=1, max_length=36)
    decision: str = Field(pattern='^(approved|rejected)$')
    watched: bool = False


class CookiePasteRequest(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(min_length=1, max_length=2*1024*1024)
    account_id: str | None = Field(default=None, max_length=36)
    display_name: str = Field(default="", max_length=255)
    owns_account: bool = False


class YouTubePublishRequest(BaseModel):
    clip_id: str
    account_id: str
    visibility: str = Field(default="private", pattern="^(private|unlisted|public)$")
    made_for_kids: bool = False
    title: str = Field(default="", max_length=100)
    consent: bool


class RenderRequest(BaseModel):
    source_id: str
    count: int = Field(default=1, ge=1, le=1)
    duration: float = Field(default=20, ge=5, le=180)
    caption: str = Field(default="", max_length=1800)
    hashtags: str = Field(default="", max_length=350)


class TikTokWebPublishRequest(BaseModel):
    clip_id: str
    account_id: str
    description: str = Field(min_length=1, max_length=2200)
    visibility: str = Field(default="private", pattern="^(private|public)$")
    consent: bool = False


class PublishRequest(BaseModel):
    clip_id: str
    account_id: str
    privacy_level: str = "SELF_ONLY"
    mode: str = Field(default="inbox", pattern="^(inbox|direct)$")
    consent: bool


def _normalize_youtube_cookies(value: object, platform: str = "youtube") -> list[dict]:
    """Validate an imported browser-cookie list without exposing its secrets."""
    if not isinstance(value, list) or not value:
        raise ValueError("поле cookies должно быть непустым массивом")

    normalized: list[dict] = []
    allowed_domains = ("tiktok.com",) if platform == "tiktok_web" else ("youtube.com", "google.com", "googlevideo.com", "ytimg.com")
    for position, raw_cookie in enumerate(value, start=1):
        if not isinstance(raw_cookie, dict):
            raise ValueError(f"cookie #{position} должен быть объектом")

        name = str(raw_cookie.get("name", "")).strip()
        cookie_value = str(raw_cookie.get("value", ""))
        domain = str(raw_cookie.get("domain", "")).strip().lower()
        clean_domain = domain.lstrip(".")
        if not name or not cookie_value or not domain:
            raise ValueError(f"cookie #{position}: обязательны name, value и domain")
        if not any(clean_domain == item or clean_domain.endswith(f".{item}") for item in allowed_domains):
            raise ValueError(f"cookie #{position}: домен не относится к выбранной платформе")

        same_site = str(raw_cookie.get("sameSite", "Lax")).lower()
        same_site = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}.get(same_site, "Lax")
        if same_site not in {"Strict", "Lax", "None"}:
            same_site = "Lax"
        cookie = {
            "name": name,
            "value": cookie_value,
            "domain": domain,
            "path": str(raw_cookie.get("path", "/")) or "/",
            "secure": bool(raw_cookie.get("secure", True)),
            "httpOnly": bool(raw_cookie.get("httpOnly", False)),
            "sameSite": same_site,
        }
        expires = raw_cookie.get("expires", raw_cookie.get("expirationDate"))
        if expires not in (None, ""):
            try:
                cookie["expires"] = float(expires)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"cookie #{position}: неверный срок действия") from exc
        normalized.append(cookie)
    return normalized


@app.on_event("startup")
async def startup() -> None:
    settings.ensure_directories()
    init_db()
    login_queue.recover()
    if settings.production_mode and settings.account_views_enabled:
        views_monitor.start()
    if settings.embedded_worker:
        from .worker import run_embedded
        app.state.worker_stop = Event()
        app.state.worker_thread = Thread(target=run_embedded, args=(app.state.worker_stop,), daemon=True, name="clipforge-worker")
        app.state.worker_thread.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    views_monitor.stop()
    await login_queue.stop()
    if hasattr(app.state, "worker_stop"):
        app.state.worker_stop.set()
        app.state.worker_thread.join(timeout=3)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "tiktok_configured": bool(settings.tiktok_client_key and settings.tiktok_client_secret),
            "youtube_configured": bool(settings.youtube_api_key),
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    if verify_session(request.cookies.get("clipforge_session", "")):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": error, "configured": bool(settings.admin_password_hash)},
    )


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    valid = (
        bool(settings.admin_password_hash)
        and secrets.compare_digest(username, settings.admin_username)
        and verify_password(password, settings.admin_password_hash)
    )
    if not valid:
        return RedirectResponse("/login?error=Неверный+логин+или+пароль", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "clipforge_session",
        create_session(username),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.public_base_url.startswith("https://"),
        samesite="strict",
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("clipforge_session")
    return response


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/state")
def state(db: Session = Depends(get_db)) -> dict:
    from .services.youtube import SEARCH_RIGHTS_MODE, SEARCH_RIGHTS_LABEL
    accounts = db.scalars(select(Account).order_by(Account.created_at.desc())).all()
    sources = db.scalars(select(SourceVideo).where(SourceVideo.archived.is_(False)).order_by(SourceVideo.created_at.desc())).all()
    clips = db.scalars(select(Clip).join(SourceVideo).where(SourceVideo.archived.is_(False)).order_by(Clip.created_at.desc()).limit(100)).all()
    jobs = db.scalars(select(Job).where(Job.archived.is_(False)).order_by(Job.created_at.desc()).limit(100)).all()
    return {
        "automation": plan_state(db),
        "editing": editing_state(db),
        "search_rights": {"mode": SEARCH_RIGHTS_MODE, "label": SEARCH_RIGHTS_LABEL},
        "download_providers": settings.download_provider_order.split(","),
        "accounts": [
            {"id": a.id, "display_name": a.display_name, "platform": a.platform, "status": a.status.value}
            for a in accounts
        ],
        "sources": [
            {
                "id": s.id,
                "title": s.title,
                "source_url": s.source_url,
                "status": s.status.value,
                "duration_seconds": s.duration_seconds,
                "error": s.error,
            }
            for s in sources
        ],
        "clips": [
            {
                "id": c.id,
                "source_id": c.source_id,
                "status": c.status.value,
                "start_seconds": c.start_seconds,
                "duration_seconds": c.duration_seconds,
                "preview_url": f"/api/clips/{c.id}/preview" if c.status == RecordStatus.ready and c.local_path else None,
                "caption": c.caption,
                "hashtags": c.hashtags,
                "error": c.error,
            }
            for c in clips
        ],
        "jobs": [
            {
                "id": j.id,
                "type": j.type.value,
                "status": j.status.value,
                "attempts": j.attempts,
                "error": j.error,
                "result": j.result,
                "next_attempt_at": j.next_attempt_at.isoformat() if j.next_attempt_at else None,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                "pipeline_id": j.payload.get("pipeline_id"),
                "account_id": j.payload.get("account_id") or j.payload.get("tiktok_web_account_id") or j.payload.get("youtube_account_id"),
                "account_name": next((a.display_name for a in accounts if a.id == (j.payload.get("account_id") or j.payload.get("tiktok_web_account_id") or j.payload.get("youtube_account_id"))), None),
            }
            for j in jobs
        ],
    }


@app.get("/api/automation")
def automation_state(db: Session = Depends(get_db)) -> dict:
    return plan_state(db)


@app.get('/api/editing')
def get_editing(db: Session = Depends(get_db)):
    return editing_state(db)


@app.patch('/api/editing')
def save_editing(payload: EditingOptions, db: Session = Depends(get_db)):
    try:
        return update_editing(db, payload)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post('/api/editing/music', status_code=201)
def upload_music(file: UploadFile = File(...), db: Session = Depends(get_db)):
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in {'.mp3','.wav','.m4a','.aac','.ogg','.flac'}:
        file.file.close()
        raise HTTPException(400, 'Загрузите MP3, WAV, M4A, AAC, OGG или FLAC')
    settings.music_dir.mkdir(parents=True, exist_ok=True)
    identity = secrets.token_hex(16)
    temporary = settings.music_dir / ('.' + identity + suffix)
    destination = settings.music_dir / (identity + '.m4a')
    try:
        size = 0
        with temporary.open('xb') as target:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > 50 * 1024 * 1024:
                    raise HTTPException(413, 'Музыка должна быть не больше 50 МБ')
                target.write(chunk)
        duration = prepare_music(temporary, destination)
        options = EditingOptions.model_validate({**editing_snapshot(db),'music_id':identity})
        track = {'id':identity,'name':Path(file.filename).name[:180],'duration_seconds':round(duration,3)}
        return update_editing(db, options, track=track)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except (ValueError, MediaError) as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from None
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        file.file.close()


@app.get('/api/editing/music/{music_id}')
def preview_music(music_id: str, db: Session = Depends(get_db)):
    try:
        if music_id not in {t['id'] for t in editing_state(db)['tracks']}:
            raise ValueError('Неизвестный трек')
        path = music_path(music_id)
    except ValueError:
        raise HTTPException(404, 'Трек не найден') from None
    return FileResponse(path, media_type='audio/mp4', headers={'Cache-Control':'no-store'})


@app.post("/api/automation/start")
def automation_start(payload: AutomationRequest, db: Session = Depends(get_db)) -> dict:
    if not settings.youtube_api_key:
        raise HTTPException(503, "Для автоматического поиска нужен YOUTUBE_API_KEY")
    if payload.publish and (not payload.consent or not payload.owns_channels or not payload.account_ids):
        raise HTTPException(400, "Выберите свои каналы и подтвердите автоматические публикации")
    if payload.publish and payload.platform == 'tiktok_web':
        plan=plan_state(db)
        if plan['enabled']:
            raise HTTPException(409,'Режим 24/7 уже включён; сначала остановите его')
        for account_id in payload.account_ids:
            account=db.get(Account,account_id)
            if not account or account.platform!='tiktok_web':
                raise HTTPException(409,'Выберите свой TikTok-аккаунт')
            if account.status!=RecordStatus.ready:
                try:
                    youtube_check_session(account_id,db)
                except HTTPException as exc:
                    raise HTTPException(exc.status_code,f'{account.display_name}: {exc.detail}') from None
    try:
        db.expire_all()
        return start_plan(db, payload.model_dump(exclude={"consent", "owns_channels"}))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/automation/stop")
def automation_stop(db: Session = Depends(get_db)) -> dict:
    return stop_plan(db)


@app.patch("/api/automation/content")
def automation_content(payload: AutomationContentRequest, db: Session = Depends(get_db)) -> dict:
    try:
        return update_plan_content(db, payload.caption, payload.hashtags)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.patch('/api/automation/five-videos')
def five_video_draft(payload: FiveVideoDraftRequest, db: Session = Depends(get_db)):
    from .services.five_videos import save_draft
    try:
        save_draft(db, [v.model_dump() for v in payload.videos], payload.account_ids,
                   queries=payload.queries, topic=payload.topic)
        return plan_state(db)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.get('/api/clips/{clip_id}/preview')
def clip_preview(clip_id: str, db: Session = Depends(get_db)):
    clip = db.get(Clip, clip_id)
    if not clip or clip.status != RecordStatus.ready or not clip.local_path or db.get(SourceVideo, clip.source_id).archived:
        raise HTTPException(404, 'Фрагмент недоступен')
    try:
        path = Path(clip.local_path).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(settings.clips_dir.resolve()):
            raise FileNotFoundError
    except (OSError, RuntimeError):
        raise HTTPException(404, 'Фрагмент недоступен') from None
    return FileResponse(path, media_type='video/mp4', headers={'Cache-Control': 'no-store'})


@app.post('/api/automation/five-videos/review')
def five_video_review(payload: ClipReviewRequest, db: Session = Depends(get_db)):
    from .services.clip_review import review_clip
    try:
        review_clip(db, **payload.model_dump())
        return plan_state(db)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post('/api/automation/five-videos/start')
def five_video_start(payload: FiveVideoStartRequest, db: Session = Depends(get_db)):
    from .services.five_videos import start_batch
    if not payload.consent:
        raise HTTPException(400, 'Подтвердите публикацию пяти роликов в выбранные аккаунты')
    if not settings.youtube_api_key:
        raise HTTPException(503, 'Для поиска нужен YOUTUBE_API_KEY')
    try:
        start_batch(db, **payload.model_dump(exclude={'consent'}))
        return plan_state(db)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.get('/api/account-views')
def account_views_state(db: Session = Depends(get_db)):
    return views_state(db)


@app.post('/api/account-views/refresh')
def refresh_account_views(db: Session = Depends(get_db)):
    return request_refresh(db)


@app.post("/api/sources")
def add_sources(payload: SourceBatch, db: Session = Depends(get_db)) -> dict:
    if not payload.rights_confirmed:
        raise HTTPException(400, "You must confirm that you may download and reuse these videos")
    created: list[str] = []
    for raw_url in payload.urls:
        url = raw_url.strip()
        if not url.startswith(("https://www.youtube.com/", "https://youtu.be/", "https://youtube.com/")):
            raise HTTPException(400, f"Unsupported YouTube URL: {url}")
        source = SourceVideo(source_url=url, title=url, rights_confirmed=True)
        db.add(source)
        db.flush()
        db.add(Job(type=JobType.download, payload={"source_id": source.id}))
        created.append(source.id)
    db.commit()
    return {"created": created}


@app.post("/api/discover")
def discover(payload: DiscoveryRequest, db: Session = Depends(get_db)) -> dict:
    if not settings.youtube_api_key:
        raise HTTPException(503, "Add YOUTUBE_API_KEY to .env before automatic discovery")
    job = Job(type=JobType.discovery, payload={**payload.model_dump(), "editing": capture_editing(db)})
    db.add(job)
    db.commit()
    return {"job_id": job.id}


@app.post("/api/pipeline/start", status_code=status.HTTP_202_ACCEPTED)
def start_pipeline(payload: PipelineRequest, db: Session = Depends(get_db)) -> dict:
    """Queue discovery, download, rendering and one consented platform upload."""
    if not settings.youtube_api_key:
        raise HTTPException(503, "Добавьте YOUTUBE_API_KEY в .env перед запуском автопоиска")
    if payload.youtube_account_id:
        if not payload.consent:
            raise HTTPException(400, "Подтвердите загрузку одного клипа после монтажа")
        account = db.get(Account, payload.youtube_account_id)
        if not account or account.platform != "youtube" or account.status != RecordStatus.ready:
            raise HTTPException(409, "Подключите доступный YouTube-аккаунт")
    if payload.tiktok_web_account_id:
        if payload.youtube_account_id or payload.visibility == "unlisted":
            raise HTTPException(400, "Выберите одну платформу и видимость TikTok")
        if not payload.consent:
            raise HTTPException(400, "Подтвердите загрузку одного клипа после монтажа")
        account = db.get(Account, payload.tiktok_web_account_id)
        if not account or account.platform != "tiktok_web" or account.status != RecordStatus.ready:
            raise HTTPException(409, "Подключите доступный TikTok-аккаунт")
    options = payload.model_dump()
    if payload.fresh_source:
        options["exclude_source_urls"] = list(db.scalars(select(SourceVideo.source_url)).all())
    job = Job(type=JobType.discovery, payload={**options, "editing": capture_editing(db)})
    db.add(job)
    db.commit()
    db.refresh(job)
    return {
        "job_id": job.id,
        "status": job.status.value,
        "message": "Поиск → скачивание → монтаж → TikTok" if payload.tiktok_web_account_id else ("Поиск → скачивание → монтаж → YouTube" if payload.youtube_account_id else "Поиск и монтаж добавлены в очередь"),
    }


@app.post("/api/sources/upload")
def upload_source(
    file: UploadFile = File(...),
    rights_confirmed: bool = Form(...),
    db: Session = Depends(get_db),
) -> dict:
    if not rights_confirmed:
        raise HTTPException(400, "Rights confirmation is required")
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm", ".mkv"}:
        raise HTTPException(400, "Unsupported video format")
    source = SourceVideo(source_url="local-upload", title=file.filename or "Uploaded video", rights_confirmed=True)
    db.add(source)
    db.flush()
    destination = settings.sources_dir / f"{source.id}{suffix}"
    with destination.open("wb") as target:
        shutil.copyfileobj(file.file, target)
    try:
        source.duration_seconds = probe_duration(destination)
        source.local_path = str(destination)
        source.status = RecordStatus.ready
    except Exception as exc:
        destination.unlink(missing_ok=True)
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    return {"id": source.id}


@app.post("/api/upload-video")
def upload_video(file: UploadFile = File(...), db: Session = Depends(get_db)) -> dict:
    """Handle the user's quick-upload button and register the file as a source."""
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if not (file.content_type or "").startswith("video/") or suffix not in {".mp4", ".mov", ".webm", ".mkv"}:
        raise HTTPException(400, "Разрешены видеофайлы MP4, MOV, WEBM и MKV")

    source = SourceVideo(
        source_url="local-quick-upload",
        title=file.filename or "Uploaded video",
        rights_confirmed=True,
        status=RecordStatus.processing,
    )
    db.add(source)
    db.flush()
    destination = settings.sources_dir / f"{source.id}{suffix}"
    try:
        with destination.open("wb") as target:
            shutil.copyfileobj(file.file, target)
        source.duration_seconds = probe_duration(destination)
        source.local_path = str(destination)
        source.status = RecordStatus.ready
        db.commit()
    except Exception as exc:
        destination.unlink(missing_ok=True)
        db.rollback()
        raise HTTPException(400, f"Не удалось обработать видео: {exc}") from exc
    finally:
        file.file.close()

    return {
        "status": "success",
        "message": "Видео загружено в ClipForge",
        "source_id": source.id,
        "filename": source.title,
    }


@app.post("/api/render")
def render(payload: RenderRequest, db: Session = Depends(get_db)) -> dict:
    source = db.get(SourceVideo, payload.source_id)
    if not source or source.archived:
        raise HTTPException(404, 'Исходник недоступен')
    if source.status != RecordStatus.ready:
        raise HTTPException(409, "Source video is not ready")
    if existing_clip(db, source.source_url, source.id):
        raise HTTPException(409, 'Из этого исходника уже создан или зарезервирован один клип')
    job = Job(type=JobType.render, payload={**payload.model_dump(), "editing": capture_editing(db)})
    db.add(job)
    db.commit()
    return {"job_id": job.id}


@app.post("/api/publish")
def publish(payload: PublishRequest, db: Session = Depends(get_db)) -> dict:
    if not payload.consent:
        raise HTTPException(400, "Explicit publication consent is required")
    clip = db.get(Clip, payload.clip_id)
    if not clip or db.get(SourceVideo, clip.source_id).archived or not db.get(Account, payload.account_id):
        raise HTTPException(404, "Clip or account not found")
    job = Job(type=JobType.publish, payload=payload.model_dump(exclude={"consent"}))
    db.add(job)
    db.commit()
    return {"job_id": job.id}


@app.get("/api/accounts/{account_id}/creator-info")
def account_creator_info(account_id: str, db: Session = Depends(get_db)) -> dict:
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    if account.platform != "tiktok":
        raise HTTPException(400, "Эта проверка предназначена для TikTok")
    try:
        token = refresh_account(account)
        info = creator_info(token)
        if info.get("creator_nickname"):
            account.display_name = info["creator_nickname"]
        db.commit()
        return info
    except TikTokError as exc:
        db.commit()
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/accounts/import")
def import_accounts(file: UploadFile = File(...), db: Session = Depends(get_db)) -> dict:
    raw = file.file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "OAuth JSON must be smaller than 2 MB")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "Invalid JSON file") from exc
    # Accept account files, Playwright storage_state, or a direct cookie export.
    if isinstance(payload, list) and payload and all(isinstance(item, dict) and {"name", "value", "domain"} <= item.keys() for item in payload):
        records = [{"display_name": "YouTube account", "cookies": payload}]
    elif isinstance(payload, dict) and "cookies" in payload:
        records = [payload]
    else:
        records = payload.get("accounts") if isinstance(payload, dict) else payload
    if not isinstance(records, list) or not records or len(records) > 200:
        raise HTTPException(400, "JSON должен содержать массив от 1 до 200 аккаунтов")
    imported = 0
    tiktok_account_ids = []
    for raw_record in records:
        if isinstance(raw_record, dict) and "cookies" in raw_record:
            try:
                raw_cookies = raw_record.get("cookies")
                is_tiktok = isinstance(raw_cookies, list) and bool(raw_cookies) and all(isinstance(c, dict) and (str(c.get("domain", "")).lstrip(".").lower() == "tiktok.com" or str(c.get("domain", "")).lstrip(".").lower().endswith(".tiktok.com")) for c in raw_cookies)
                platform = "tiktok_web" if is_tiktok else "youtube"
                declared = raw_record.get("platform")
                if declared and declared not in ({"tiktok", "tiktok_web"} if is_tiktok else {"youtube"}):
                    raise ValueError("platform не соответствует доменам cookie")
                cookies = _normalize_youtube_cookies(raw_cookies, platform)
            except ValueError as exc:
                raise HTTPException(400, f"Неверный аккаунт #{imported + 1}: {exc}") from None

            display_name = str(raw_record.get("display_name", f"{'TikTok' if is_tiktok else 'YouTube'} account {imported + 1}")).strip()
            if is_tiktok and display_name == "YouTube account":
                display_name = "TikTok account"
            display_name = display_name[:255] or f"{'TikTok' if is_tiktok else 'YouTube'} account {imported + 1}"
            fingerprint_source = json.dumps(cookies, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            external_id = f"{platform}:{hashlib.sha256(fingerprint_source.encode('utf-8')).hexdigest()[:32]}"
            account = db.scalar(select(Account).where(Account.external_id == external_id))
            if not account:
                account = Account(
                    platform=platform,
                    external_id=external_id,
                    display_name=display_name,
                    access_token="",
                    refresh_token="",
                )
                db.add(account)
            account.platform = platform
            account.display_name = display_name
            account.access_token = encrypt(json.dumps({"cookies": cookies, "origins": []}, ensure_ascii=False))
            account.refresh_token = encrypt("")
            account.status = RecordStatus.reauth_required if is_tiktok else RecordStatus.ready
            if is_tiktok:
                db.flush()
                tiktok_account_ids.append(account.id)
            imported += 1
            continue

        try:
            record = TikTokTokenRecord.model_validate(raw_record)
        except Exception as exc:
            # Pydantic's full validation text can contain submitted secret values.
            raise HTTPException(400, f"Аккаунт #{imported + 1}: ожидаются cookies YouTube/TikTok или OAuth-токены TikTok") from None
        account = db.scalar(select(Account).where(Account.external_id == record.open_id))
        if not account:
            account = Account(
                external_id=record.open_id,
                display_name=record.display_name,
                access_token="",
                refresh_token="",
            )
            db.add(account)
        else:
            account.display_name = record.display_name
        apply_token_data(account, record.model_dump())
        imported += 1
    db.commit()
    return {"imported": imported, "tiktok_account_ids": list(dict.fromkeys(tiktok_account_ids))}


@app.post("/api/tiktok-web/cookies/paste")
async def paste_tiktok_cookies(request: Request, db: Session = Depends(get_db)) -> dict:
    raw=await request.body()
    if len(raw)>3*1024*1024:
        raise HTTPException(413,"Cookie должны быть меньше 2 МБ")
    try:
        payload=CookiePasteRequest.model_validate_json(raw)
    except ValueError:
        # Validation errors must not echo the submitted text or credentials.
        raise HTTPException(400,"Проверьте поля: cookie до 2 МБ, один аккаунт и название до 255 символов") from None
    if not payload.owns_account:
        raise HTTPException(400,"Подтвердите, что это ваш аккаунт")
    try:
        cookies=_normalize_youtube_cookies(parse_tiktok_cookie_text(payload.text),"tiktok_web")
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from None
    fingerprint=json.dumps(cookies,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    external_id='tiktok_web:'+hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:32]
    matching=db.scalar(select(Account).where(Account.external_id==external_id))
    account=db.get(Account,payload.account_id) if payload.account_id else matching
    if payload.account_id and (not account or account.platform!='tiktok_web'):
        raise HTTPException(404,"Выберите существующий TikTok-аккаунт")
    if matching and account and matching.id!=account.id:
        raise HTTPException(409,"Эти cookie уже сохранены в другом аккаунте. Выберите его для обновления")
    if account:
        active=db.scalar(select(Job).where(Job.status.in_([RecordStatus.queued,RecordStatus.processing]),
            (Job.payload['account_id'].as_string()==account.id) | (Job.payload['tiktok_web_account_id'].as_string()==account.id)))
        if active:
            raise HTTPException(409,"Дождитесь завершения текущей задачи этого аккаунта, затем сохраните cookie")
    created=account is None
    if created:
        account=Account(platform='tiktok_web',external_id=external_id,display_name=payload.display_name.strip() or 'TikTok · новый аккаунт',
                        access_token='',refresh_token='')
        db.add(account)
    elif payload.display_name.strip():
        account.display_name=payload.display_name.strip()
    account.external_id=external_id
    account.access_token=encrypt(json.dumps({'cookies':cookies,'origins':[]},ensure_ascii=False))
    account.refresh_token=encrypt('')
    account.status=RecordStatus.reauth_required
    db.commit()
    return {'account_id':account.id,'display_name':account.display_name,'created':created,'cookie_count':len(cookies),
            'status':account.status.value,'message':'Cookie сохранены. Проверяю вход для режима 24/7…'}


@app.get("/api/account-logins")
async def account_login_state():
    return login_queue.state()


@app.post("/api/account-logins/import", status_code=201)
async def account_login_import(file: UploadFile = File(...), owns_accounts: bool = Form(False), db: Session = Depends(get_db)):
    try:
        if not owns_accounts:
            raise HTTPException(400, "Подтвердите, что это ваши аккаунты и вы вправе входить в них")
        raw = await file.read(1024 * 1024 + 1)
        return import_credentials(db, parse_credentials(raw, file.filename or "accounts.json"))
    except LoginQueueError as exc:
        raise HTTPException(400, str(exc)) from None
    finally:
        await file.close()


@app.post("/api/account-logins/start")
async def account_login_start():
    try:
        login_queue.start()
        return {"started": True}
    except LoginQueueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/account-logins/stop")
async def account_login_stop():
    await login_queue.stop()
    return {"stopped": True}


@app.post("/api/account-logins/clear")
async def account_login_clear():
    try:
        login_queue.clear_pending()
        return {"cleared": True}
    except LoginQueueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.get("/api/account-logins/{task_id}/screen")
async def account_login_screen(task_id: str):
    try:
        image = await login_queue.screenshot(task_id)
        return Response(image, media_type="image/jpeg", headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    except Exception:
        raise HTTPException(409, "Окно ручной проверки сейчас недоступно") from None


@app.post("/api/account-logins/{task_id}/action")
async def account_login_action(task_id: str, request: Request):
    try:
        raw = await request.body()
        if len(raw) > 4096:
            raise LoginQueueError("Слишком большой запрос управления")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise LoginQueueError("Неверный запрос управления")
        await login_queue.action(task_id, payload)
        return {"ok": True}
    except LoginQueueError as exc:
        raise HTTPException(409, str(exc)) from None
    except Exception:
        raise HTTPException(409, "Не удалось выполнить действие в окне браузера") from None


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if not job or job.archived:
        raise HTTPException(404, "Job not found")
    if job.type == JobType.automation_publish:
        raise HTTPException(409, "Legacy automation jobs are history-only and cannot be retried")
    if job.status in (RecordStatus.queued, RecordStatus.processing, RecordStatus.ready):
        raise HTTPException(409, "Нельзя повторить выполняющуюся или уже завершённую задачу")
    if job.type in (JobType.youtube_publish, JobType.tiktok_web_publish):
        receipt_model = YouTubePublication if job.type == JobType.youtube_publish else TikTokWebPublication
        publication = db.scalar(select(receipt_model).where(receipt_model.job_id == job.id))
        if publication and publication.stage in {"uploading", "submitting", "published"}:
            raise HTTPException(409, "Файл уже отправлен; проверьте существующее видео в Studio выбранной платформы, повторная загрузка запрещена")
        account = db.get(Account, job.payload.get("account_id"))
        if not account or account.status != RecordStatus.ready:
            raise HTTPException(409, "Сначала восстановите сессию аккаунта")
    job.status = RecordStatus.queued
    job.error = None
    job.attempts = 0
    job.next_attempt_at = None
    job.started_at = None
    job.finished_at = None
    job.worker_id = None
    db.commit()
    return {"status": "queued"}


@app.get("/oauth/tiktok/start")
def tiktok_start() -> RedirectResponse:
    if not settings.tiktok_client_key or not settings.tiktok_client_secret:
        raise HTTPException(503, "TikTok application credentials are not configured")
    state = secrets.token_urlsafe(32)
    signed_state = signer.dumps(state)
    response = RedirectResponse(authorization_url(state))
    response.set_cookie(
        "tiktok_oauth_state",
        signed_state,
        max_age=600,
        httponly=True,
        secure=settings.tiktok_redirect_uri.startswith("https://"),
        samesite="lax",
    )
    return response


@app.get("/oauth/tiktok/callback")
def tiktok_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if error:
        raise HTTPException(400, error_description or error)
    signed_state = request.cookies.get("tiktok_oauth_state", "")
    try:
        expected_state = signer.loads(signed_state, max_age=600)
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(400, "OAuth state is invalid or expired") from exc
    if not secrets.compare_digest(expected_state, state):
        raise HTTPException(400, "OAuth state mismatch")
    try:
        token_data = exchange_code(code)
    except TikTokError as exc:
        raise HTTPException(400, str(exc)) from exc
    account = db.scalar(select(Account).where(Account.external_id == token_data["open_id"]))
    if not account:
        account = Account(
            external_id=token_data["open_id"],
            display_name=f"TikTok {token_data['open_id'][-6:]}",
            access_token="",
            refresh_token="",
        )
        db.add(account)
    apply_token_data(account, token_data)
    db.commit()
    response = RedirectResponse("/?connected=1")
    response.delete_cookie("tiktok_oauth_state")
    return response


@app.post("/api/accounts/{account_id}/check-session")
def youtube_check_session(account_id: str, db: Session = Depends(get_db)) -> dict:
    # Account checks run sequentially and never upload media or start a plan.
    with session_check_lock:
        db.expire_all()
        account = db.get(Account, account_id)
        if not account or account.platform not in {"youtube", "tiktok_web"}:
            raise HTTPException(404, "Браузерный аккаунт не найден")
        checked_token=account.access_token
        def save_status(status):
            changed=db.execute(update(Account).where(Account.id==account_id,Account.access_token==checked_token).values(status=status))
            if changed.rowcount!=1:
                db.rollback()
                raise HTTPException(409,'Cookie обновились во время проверки. Сохраните и проверьте новый набор.')
            db.commit()
            db.refresh(account)
        try:
            result = check_tiktok_web_session(checked_token) if account.platform == "tiktok_web" else check_session(checked_token)
        except TikTokWebPolicyError as exc:
            save_status(RecordStatus.error)
            raise HTTPException(409, str(exc)) from None
        except (YouTubeSessionError, TikTokWebSessionError) as exc:
            save_status(RecordStatus.reauth_required)
            raise HTTPException(409, str(exc)) from None
        except TikTokWebError as exc:
            raise HTTPException(502, str(exc)) from None
        except Exception as exc:
            raise HTTPException(502, connection_error(exc)) from None
        save_status(RecordStatus.ready)
        return result


@app.post("/api/youtube/publish", status_code=status.HTTP_202_ACCEPTED)
def publish_youtube(payload: YouTubePublishRequest, db: Session = Depends(get_db)) -> dict:
    if not payload.consent:
        raise HTTPException(400, "Подтвердите загрузку видео")
    clip = db.get(Clip, payload.clip_id)
    if not clip:
        raise HTTPException(404, "Клип не найден")
    try:
        job = enqueue_youtube(db, account_id=payload.account_id, clip=clip, visibility=payload.visibility,
            made_for_kids=payload.made_for_kids, title=payload.title)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    return {"job_id": job.id, "status": "queued", "message": "Загрузка на YouTube добавлена в очередь"}


@app.post("/api/tiktok-web/publish", status_code=202)
def publish_tiktok_web(payload: TikTokWebPublishRequest, db: Session = Depends(get_db)):
    if not payload.consent:
        raise HTTPException(400, "Подтвердите публикацию и право использовать видео")
    clip = db.get(Clip, payload.clip_id)
    if not clip:
        raise HTTPException(404, "Клип не найден")
    try:
        job = enqueue_tiktok_web(db, account_id=payload.account_id, clip=clip,
            description=payload.description, visibility=payload.visibility)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None
    return {"job_id": job.id, "status": "queued", "message": "Загрузка через сайт TikTok добавлена в очередь"}


@app.post("/api/tiktok-web/direct-upload", status_code=202)
def direct_tiktok_web_upload(account_id: str = Form(...), description: str = Form(...),
                            visibility: str = Form("private"), consent: bool = Form(False),
                            file: UploadFile = File(...), db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if not consent or not account or account.platform != "tiktok_web" or account.status != RecordStatus.ready:
        raise HTTPException(409, "Подтвердите отправку и выберите готовый TikTok-аккаунт с cookie")
    if visibility not in {"private", "public"} or not 1 <= len(description.strip()) <= 2200:
        raise HTTPException(400, "Проверьте описание и видимость")
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm", ".mkv"}:
        raise HTTPException(400, "Разрешены MP4, MOV, WEBM и MKV")
    folder = settings.sources_dir / "tiktok_direct_uploads"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{secrets.token_hex(16)}{suffix}"
    try:
        with path.open("xb") as stream:
            size = 0
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, "Файл превышает допустимый размер")
                stream.write(chunk)
        duration = probe_duration(path)
        if not 5 <= duration <= 180:
            raise HTTPException(400, "В этом режиме сервиса нужны клипы длиной 5–180 секунд")
        width, height, _ = probe_video_stream(path)
        source = SourceVideo(source_url="local-tiktok-upload", title=Path(file.filename or "Видео").name[:255],
            local_path=str(path), duration_seconds=duration, rights_confirmed=True, status=RecordStatus.ready)
        db.add(source)
        db.flush()
        edit = capture_editing(db)
        if width > height or has_edits(edit):
            render_duration = direct_duration(duration, edit)
            job = Job(type=JobType.render, payload={"source_id": source.id, "count": 1, "duration": render_duration, "editing": edit,
                "caption": "", "hashtags": "", "tiktok_web_account_id": account_id,
                "tiktok_description": description.strip(), "visibility": visibility})
            db.add(job)
        else:
            clip = Clip(source_id=source.id, local_path=str(path), start_seconds=0,
                duration_seconds=duration, caption="", hashtags="", status=RecordStatus.ready)
            db.add(clip)
            db.flush()
            job = enqueue_tiktok_web(db, account_id=account_id, clip=clip, description=description, visibility=visibility)
        db.commit()
    except HTTPException:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise HTTPException(409, "Не удалось подготовить TikTok-загрузку: проверьте файл и предыдущие отправки") from None
    finally:
        file.file.close()
    return {"job_id": job.id, "status": "queued", "message": "Файл поставлен на загрузку через сайт TikTok"}


@app.post("/api/youtube/direct-upload", status_code=status.HTTP_202_ACCEPTED)
def direct_youtube_upload(
    file: UploadFile = File(...),
    account_id: str = Form(...),
    title: str = Form(""),
    visibility: str = Form("private"),
    made_for_kids: bool = Form(False),
    consent: bool = Form(False),
    db: Session = Depends(get_db),
) -> dict:
    """Validate and persist a media file, then queue the actual browser upload."""
    if not consent:
        raise HTTPException(400, "Подтвердите загрузку видео")
    account = db.get(Account, account_id)
    if not account or account.platform != "youtube":
        raise HTTPException(404, "Выберите импортированный YouTube-аккаунт")
    if account.status != RecordStatus.ready:
        raise HTTPException(409, "Сначала восстановите сессию выбранного YouTube-аккаунта")
    if visibility not in {"private", "unlisted", "public"}:
        raise HTTPException(400, "Неверная видимость видео")
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm", ".mkv"}:
        raise HTTPException(400, "Разрешены видеофайлы MP4, MOV, WEBM и MKV")

    # Сохраняем во временную директорию
    temp_dir = settings.sources_dir / "youtube_direct_uploads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_path = temp_dir / f"{secrets.token_hex(16)}{suffix}"

    try:
        with file_path.open("wb") as target:
            size = 0
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(413, "Файл превышает допустимый размер")
                target.write(chunk)
        duration = probe_duration(file_path)
        if duration > 180:
            raise HTTPException(400, "Для Shorts нужен ролик до 180 секунд; сначала нарежьте исходник")
        width, height, _ = probe_video_stream(file_path)
        source = SourceVideo(source_url="local-youtube-upload", title=Path(file.filename or "Видео").name[:255],
            local_path=str(file_path), rights_confirmed=True, duration_seconds=duration, status=RecordStatus.ready)
        db.add(source)
        db.flush()
        edit = capture_editing(db)
        if width > height or has_edits(edit):
            render_duration = direct_duration(duration, edit)
            job = Job(type=JobType.render, payload={"source_id": source.id, "count": 1, "duration": render_duration, "editing": edit,
                "caption": "", "hashtags": "", "youtube_account_id": account_id, "visibility": visibility,
                "made_for_kids": made_for_kids, "title": (title.strip() or source.title)[:100]})
            db.add(job)
        else:
            clip = Clip(source_id=source.id, local_path=str(file_path), start_seconds=0,
                        duration_seconds=duration, caption="", hashtags="", status=RecordStatus.ready)
            db.add(clip)
            db.flush()
            job = enqueue_youtube(db, account_id=account_id, clip=clip, visibility=visibility,
                made_for_kids=made_for_kids, title=(title.strip() or source.title)[:100])
        db.commit()
    except HTTPException:
        db.rollback()
        file_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        db.rollback()
        file_path.unlink(missing_ok=True)
        raise HTTPException(409, f"Не удалось подготовить загрузку: {exc}") from exc
    finally:
        file.file.close()

    return {
        "status": "queued", "job_id": job.id,
        "message": "Загрузка на YouTube добавлена в очередь; результат появится в журнале",
        "filename": source.title,
    }


