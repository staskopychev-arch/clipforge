import os
import base64
import binascii
from pathlib import Path
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_secret: str = "change-me-before-production"
    admin_username: str = "admin"
    admin_password_hash: str = ""
    session_max_age_seconds: int = 43200
    database_url: str = "sqlite:///./data/clipforge.db"
    media_root: Path = Path("./data")
    public_base_url: str = "http://localhost:8000"
    youtube_api_key: str = ""

    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = "http://localhost:8000/oauth/tiktok/callback"
    tiktok_scopes: str = "user.info.basic,video.upload,video.publish"

    y2save_url: str = "https://y2save.com/en"
    y2save_headless: bool = True
    y2save_doh_enabled: bool = False
    y2save_doh_template: str = "https://dns.google/dns-query"
    y2save_timeout_seconds: int = 180
    y2save_input_selector: str = ""
    y2save_submit_selector: str = ""
    y2save_download_selector: str = ""
    download_provider_order: str = "direct,cnvmp3,y2save"
    download_provider_timeout_seconds: int = 120
    download_failure_cooldown_seconds: int = 60
    cnvmp3_url: str = "https://cnvmp3.com/"

    worker_poll_seconds: float = 2
    max_job_attempts: int = 3
    worker_retry_base_seconds: float = 15
    worker_stale_after_seconds: int = 3600
    tiktok_status_poll_seconds: float = 3
    tiktok_status_timeout_seconds: int = 90

    qa_allowed_domains: str = "example.com,localhost,127.0.0.1"
    qa_browser_headless: bool = True
    qa_navigation_timeout_ms: int = 30000
    playwright_browsers_path: Path = Path("./data/ms-playwright")
    browser_executable_path: str = ""
    ffmpeg_executable: str = "ffmpeg"
    ffprobe_executable: str = "ffprobe"
    embedded_worker: bool = True
    account_views_enabled: bool = True
    youtube_upload_headless: bool = False
    youtube_studio_timeout_seconds: int = 90
    youtube_upload_timeout_seconds: int = 600
    youtube_doh_enabled: bool = True
    youtube_doh_template: str = "https://dns.google/dns-query"
    tiktok_web_headless: bool = False
    tiktok_web_timeout_seconds: int = 600
    max_upload_bytes: int = 512 * 1024 * 1024
    production_mode: bool = False
    auto_create_schema: bool = True
    app_secret_file: str = ""
    admin_password_hash_file: str = ""
    youtube_api_key_file: str = ""

    def load_secret_files(self) -> None:
        for name in ("app_secret", "admin_password_hash", "youtube_api_key"):
            filename = getattr(self, name + "_file")
            if filename:
                value = Path(filename).read_text(encoding="utf-8").strip()
                if (not value and name != "youtube_api_key") or len(value) > 8192:
                    raise RuntimeError(f"Invalid secret file for {name}")
                setattr(self, name, value)

    def validate_runtime(self) -> None:
        if not self.production_mode:
            return
        if len(self.app_secret) < 32 or self.app_secret in {"change-me-before-production", "replace-with-a-long-random-secret"}:
            raise RuntimeError("Production requires a strong, persistent APP_SECRET")
        try:
            algorithm, salt, digest = self.admin_password_hash.split("$")
            valid_hash = (algorithm == "scrypt" and
                          len(base64.b64decode(salt, altchars=b"-_", validate=True)) == 16 and
                          len(base64.b64decode(digest, altchars=b"-_", validate=True)) == 32)
        except (ValueError, binascii.Error):
            valid_hash = False
        if not valid_hash:
            raise RuntimeError("Production requires a configured admin password hash")
        url = urlsplit(self.public_base_url)
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}):
            raise RuntimeError("Use HTTPS or a loopback URL with an SSH tunnel")
        if url.username or url.password or not url.hostname:
            raise RuntimeError("Invalid PUBLIC_BASE_URL")
        if self.embedded_worker or self.auto_create_schema:
            raise RuntimeError("Production requires a separate worker and explicit migrations")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def sources_dir(self) -> Path:
        return self.media_root / "sources"

    @property
    def clips_dir(self) -> Path:
        return self.media_root / "clips"

    @property
    def music_dir(self) -> Path:
        return self.media_root / 'music'

    @property
    def browser_dir(self) -> Path:
        return self.media_root / "browser"

    @property
    def allowed_test_domains(self) -> set[str]:
        return {item.strip().lower().rstrip(".") for item in self.qa_allowed_domains.split(",") if item.strip()}

    def ensure_directories(self) -> None:
        for path in (
            self.media_root,
            self.sources_dir,
            self.clips_dir,
            self.music_dir,
            self.browser_dir,
            self.playwright_browsers_path,
        ):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.load_secret_files()
settings.validate_runtime()
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(settings.playwright_browsers_path.resolve()))

