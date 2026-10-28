import enum
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RecordStatus(str, enum.Enum):
    queued = "queued"
    processing = "processing"
    ready = "ready"
    error = "error"
    reauth_required = "reauth_required"


class JobType(str, enum.Enum):
    discovery = "discovery"
    download = "download"
    render = "render"
    publish = "publish"
    youtube_publish = "youtube_publish"
    tiktok_web_publish = "tiktok_web_publish"
    # Read-only compatibility for jobs produced by an earlier local build.
    automation_publish = "automation_publish"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    platform: Mapped[str] = mapped_column(String(32), default="tiktok")
    external_id: Mapped[str] = mapped_column(String(255), unique=True)
    display_name: Mapped[str] = mapped_column(String(255), default="TikTok account")
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[RecordStatus] = mapped_column(
        Enum(RecordStatus, native_enum=False, length=15), default=RecordStatus.ready
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceVideo(Base):
    __tablename__ = "source_videos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_url: Mapped[str] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default='0', index=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(String(255), default="Source video")
    rights_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[RecordStatus] = mapped_column(
        Enum(RecordStatus, native_enum=False, length=15), default=RecordStatus.queued
    )
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    clips: Mapped[list["Clip"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class Clip(Base):
    __tablename__ = "clips"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source_id: Mapped[str] = mapped_column(ForeignKey("source_videos.id"))
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_seconds: Mapped[float] = mapped_column(Float)
    duration_seconds: Mapped[float] = mapped_column(Float)
    caption: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[RecordStatus] = mapped_column(
        Enum(RecordStatus, native_enum=False, length=15), default=RecordStatus.queued
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[SourceVideo] = relationship(back_populates="clips")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    type: Mapped[JobType] = mapped_column(Enum(JobType, native_enum=False, length=32))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default='0', index=True)
    status: Mapped[RecordStatus] = mapped_column(
        Enum(RecordStatus, native_enum=False, length=15), default=RecordStatus.queued
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SourceClipUse(Base):
    __tablename__ = 'source_clip_uses'
    source_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey('source_videos.id'))
    job_id: Mapped[str | None] = mapped_column(ForeignKey('jobs.id'), nullable=True)
    clip_id: Mapped[str | None] = mapped_column(ForeignKey('clips.id'), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class YouTubePublication(Base):
    """A durable reservation prevents duplicate uploads, including uncertain saves."""

    __tablename__ = "youtube_publications"
    __table_args__ = (UniqueConstraint("account_id", "file_hash", name="uq_youtube_account_file"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    file_hash: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(32), default="reserved")
    video_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TikTokWebPublication(Base):
    """Reserve the file before browser transmission; uncertain posts never retry."""

    __tablename__ = "tiktok_web_publications"
    __table_args__ = (UniqueConstraint("account_id", "file_hash", name="uq_tiktok_web_account_file"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), unique=True)
    file_hash: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(32), default="reserved")
    video_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TestEnvironment(Base):
    """Encrypted configuration for an isolated, allowlisted E2E environment."""

    __tablename__ = "test_environments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    connection_hub_id: Mapped[str] = mapped_column(String(255), unique=True, default=lambda: str(uuid.uuid4()))
    # The column names intentionally match the external configuration contract,
    # but values are encrypted at rest by the helper methods below.
    storage_state: Mapped[str] = mapped_column(Text)
    proxy_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_domain: Mapped[str] = mapped_column(String(255), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    def set_storage_state(self, value: dict) -> None:
        from .security import encrypt

        self.storage_state = encrypt(json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def get_storage_state(self) -> dict:
        from .security import decrypt

        value = json.loads(decrypt(self.storage_state))
        if not isinstance(value, dict):
            raise ValueError("Playwright storage_state must be a JSON object")
        return value
    def set_proxy_config(self, value: dict | None) -> None:
        from .security import encrypt

        self.proxy_config = (
            encrypt(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) if value is not None else None
        )

    def get_proxy_config(self) -> dict | None:
        from .security import decrypt

        if not self.proxy_config:
            return None
        value = json.loads(decrypt(self.proxy_config))
        if not isinstance(value, dict):
            raise ValueError("Proxy configuration must be a JSON object")
        return value


class AutomationPlan(Base):
    """One explicitly enabled, durable publishing schedule for owned channels."""

    __tablename__ = "automation_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_pipeline_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="stopped")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AccountLogin(Base):
    """An explicit, single-attempt login queue; never a credential-checking job."""

    __tablename__ = "account_logins"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    position: Mapped[int] = mapped_column(Integer)
    display_name: Mapped[str] = mapped_column(String(120))
    identity_key: Mapped[str] = mapped_column(String(64), index=True)
    login_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    password_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    message: Mapped[str] = mapped_column(String(500), default="Ожидает запуска очереди")
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AccountViews(Base):
    """Persistent, independent hourly read-only Studio checks."""
    __tablename__ = 'account_views'
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'),primary_key=True)
    views: Mapped[int | None] = mapped_column(BigInteger,nullable=True)
    video_count: Mapped[int | None] = mapped_column(Integer,nullable=True)
    approximate: Mapped[bool] = mapped_column(Boolean,default=False)
    complete: Mapped[bool] = mapped_column(Boolean,default=False)
    profile_url: Mapped[str | None] = mapped_column(Text,nullable=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True,index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36),nullable=True)
    session_hash: Mapped[str | None] = mapped_column(String(64),nullable=True)
    status: Mapped[str] = mapped_column(String(32),default='pending')
    error: Mapped[str | None] = mapped_column(String(500),nullable=True)


class AccountViewSample(Base):
    __tablename__ = 'account_view_samples'
    id: Mapped[int] = mapped_column(Integer,primary_key=True,autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'),index=True)
    views: Mapped[int] = mapped_column(BigInteger)
    video_count: Mapped[int] = mapped_column(Integer)
    approximate: Mapped[bool] = mapped_column(Boolean)
    complete: Mapped[bool] = mapped_column(Boolean)
    profile_url: Mapped[str | None] = mapped_column(Text,nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),index=True)

