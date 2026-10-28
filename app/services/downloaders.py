"""Bounded provider failover for permitted public videos, with media validation."""
import logging
import time
import asyncio
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.async_api import async_playwright

from ..config import settings
from .browser_network import configure_secure_dns_profile
from .media import probe_duration, probe_video_stream
from .y2save import DownloadBlocked, Y2SaveDownloader, Y2SaveError
from .direct_download import DirectDownloader

log = logging.getLogger(__name__)


class ProvidersUnavailable(Y2SaveError):
    def __init__(self, attempts):
        self.provider_attempts = [dict(item) for item in attempts]
        super().__init__('Ни один загрузчик не выдал корректное видео. Попытки: ' +
            ', '.join(f"{item['provider']}={item['status']}" for item in attempts))


def failure_reason(exc):
    """Store fixed descriptions, never raw URLs, cookies or provider responses."""
    text = str(exc).lower()
    if 'отклонил конвертацию' in text or 'сообщил об ошибке' in text:
        return 'Сервис отклонил конвертацию'
    if 'empty' in text:
        return 'Сервис выдал пустой файл'
    if 'layout changed' in text:
        return 'Изменился интерфейс сервиса'
    if 'timeout' in type(exc).__name__.lower() or any(s in text for s in ('отведённое время','не выдал mp4','did not start a file download')):
        return 'Сервис не начал скачивание вовремя'
    if type(exc).__name__ == 'MediaError':
        return 'Сервис выдал некорректное видео'
    return 'Ошибка сервиса скачивания'


class CnvMP3Downloader:
    """Use the ordinary MP4 UI. No private API, account cookies or CAPTCHA solver."""

    progress_callback = None

    def download(self, url: str, destination: Path) -> Path:
        Y2SaveDownloader._validate_youtube_url(url)
        try:
            return asyncio.run(asyncio.wait_for(self._download(url, destination), settings.download_provider_timeout_seconds))
        except asyncio.TimeoutError as exc:
            raise Y2SaveError("CnvMP3 не выдал видео за отведённое время") from exc

    async def _check(self, page):
        body = (await page.locator("body").inner_text()).lower()
        if any(marker in body for marker in ("verify you are human", "unusual traffic", "sign in to confirm", "too many requests")):
            raise DownloadBlocked("CnvMP3 требует ручной проверки или ограничил запросы")
        for frame in await page.locator('iframe[src*="recaptcha"][title*="challenge"],iframe[src*="hcaptcha"]').all():
            if await frame.is_visible():
                raise DownloadBlocked("CnvMP3 требует ручного прохождения CAPTCHA")
        return body

    async def _download(self, url: str, destination: Path) -> Path:
        partial = destination.with_name(f".{destination.stem}.cnvmp3.mp4")
        partial.unlink(missing_ok=True)
        try:
            with TemporaryDirectory(prefix="clipforge-cnvmp3-", ignore_cleanup_errors=True) as profile:
                if settings.y2save_doh_enabled:
                    configure_secure_dns_profile(Path(profile), settings.y2save_doh_template)
                options = {"headless": settings.y2save_headless, "accept_downloads": True, "args": ["--disable-quic"]}
                if settings.production_mode:
                    options["chromium_sandbox"] = True
                if settings.browser_executable_path:
                    options["executable_path"] = settings.browser_executable_path
                async with async_playwright() as driver:
                    context = await driver.chromium.launch_persistent_context(profile, **options)
                    try:
                        page = await context.new_page()
                        page.set_default_timeout(15000)
                        downloads, alerts = [], []
                        page.on("download", lambda download: downloads.append(download))

                        async def dialog_handler(dialog):
                            alerts.append(dialog.message[:300])
                            await dialog.dismiss()

                        page.on("dialog", dialog_handler)
                        response = await page.goto(settings.cnvmp3_url, wait_until="domcontentloaded", timeout=30000)
                        if response and response.status in {401, 403, 429}:
                            raise DownloadBlocked(f"CnvMP3 ограничил доступ: HTTP {response.status}")
                        await self._check(page)
                        field = page.locator("#video-url")
                        await field.wait_for(state="visible")
                        await page.locator("#format-select-display").click()
                        await page.locator("#format-select-list").get_by_text("MP4", exact=True).click()
                        await page.locator("#quality-video-select-display").click()
                        await page.locator("#quality-video-select-list-720").click()
                        await field.fill(url)
                        await page.locator('#converter-form input[type="submit"]').filter(visible=True).first.click()
                        if self.progress_callback:
                            self.progress_callback("conversion", None)
                        while True:
                            body = await self._check(page)
                            if alerts:
                                message = alerts[0]
                                if re.search(r"captcha|verify|not a bot|too many requests", message, re.I):
                                    raise DownloadBlocked("CnvMP3 требует ручной проверки")
                                raise Y2SaveError(f"CnvMP3 сообщил об ошибке: {message}")
                            if downloads:
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                if self.progress_callback:
                                    self.progress_callback("downloading", None)
                                await downloads[0].save_as(str(partial))
                                if partial.stat().st_size > settings.max_upload_bytes:
                                    raise Y2SaveError("CnvMP3: файл превышает установленный предел размера")
                                probe_duration(partial)
                                probe_video_stream(partial)
                                partial.replace(destination)
                                return destination
                            if any(marker in body for marker in ("conversion failed", "download failed", "video unavailable", "something went wrong")):
                                raise Y2SaveError("CnvMP3 не смог подготовить файл")
                            await page.wait_for_timeout(500)
                    finally:
                        await context.close()
        finally:
            partial.unlink(missing_ok=True)

    def close(self):
        pass


class FallbackDownloader:
    """Try each provider once; never use failover to bypass an access challenge."""

    def __init__(self, providers=None):
        registry = {"direct": DirectDownloader, "cnvmp3": CnvMP3Downloader, "y2save": Y2SaveDownloader}
        order = [name.strip() for name in settings.download_provider_order.split(",") if name.strip()]
        if providers is None and (not order or any(name not in registry for name in order)):
            raise ValueError("DOWNLOAD_PROVIDER_ORDER must contain direct, cnvmp3 and/or y2save")
        self.providers = providers if providers is not None else [(name, registry[name]()) for name in dict.fromkeys(order)]
        self.progress_callback = None
        self.last_provider = None
        self.attempts = []
        self._cooldowns = {}

    def download(self, url: str, destination: Path) -> Path:
        self.attempts = []
        for name, provider in self.providers:
            if self._cooldowns.get(name, 0) > time.monotonic():
                self.attempts.append({"provider": name, "status": "cooldown"})
                continue
            self.last_provider = name
            provider.progress_callback = self.progress_callback
            temporary = destination.with_name(f".{destination.stem}.{name}.candidate.mp4")
            try:
                if self.progress_callback:
                    self.progress_callback("provider_start", None)
                provider.download(url, temporary)
                if temporary.stat().st_size > settings.max_upload_bytes:
                    raise Y2SaveError("Downloaded source exceeds the configured file size limit")
                probe_duration(temporary)
                probe_video_stream(temporary)
                temporary.replace(destination)
                self.attempts.append({"provider": name, "status": "ready"})
                return destination
            except DownloadBlocked:
                self.attempts.append({"provider": name, "status": "manual_verification_required"})
                raise
            except Exception as exc:
                # Signed media URLs and cookies must never be stored in logs.
                self.attempts.append({"provider": name, "status": "error", "error_type": type(exc).__name__, "reason": failure_reason(exc)})
                if self.progress_callback:
                    self.progress_callback('provider_failed', None)
                self._cooldowns[name] = time.monotonic() + settings.download_failure_cooldown_seconds
                log.warning("Download provider %s failed (%s); trying the next provider", name, type(exc).__name__)
                try:
                    provider.close()
                except Exception:
                    log.warning("Provider %s context was already closed", name)
            finally:
                temporary.unlink(missing_ok=True)
        raise ProvidersUnavailable(self.attempts)

    def next_available_delay(self) -> float:
        return max(0, min((self._cooldowns.get(name, 0) for name, _ in self.providers), default=0) - time.monotonic())

    def close(self):
        for _, provider in self.providers:
            try:
                provider.close()
            except Exception:
                log.warning("Could not close a downloader context", exc_info=False)
