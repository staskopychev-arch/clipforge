import re
import time
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

import httpx
from playwright.sync_api import BrowserContext, Locator, Page, Playwright, TimeoutError, sync_playwright

from ..config import settings
from .browser_network import configure_secure_dns_profile

log = logging.getLogger("clipforge.y2save")


class Y2SaveError(RuntimeError):
    pass


class DownloadBlocked(Y2SaveError):
    """Human verification or explicit access denial: do not switch to evade it."""


class Y2SaveDownloader:
    """Maintains one browser profile and reuses the downloader tab between jobs."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self.progress_callback = None
        self._profile = None

    def _progress(self, stage: str, percent: int | None = None) -> None:
        if self.progress_callback:
            self.progress_callback(stage, percent)

    def start(self) -> None:
        if self._context:
            return
        # Reuse this worker's browser, but do not persist a hostname-bound Chrome
        # SingletonLock across container replacements. No account data lives here.
        self._profile = TemporaryDirectory(prefix="clipforge-downloader-", ignore_cleanup_errors=True)
        profile = Path(self._profile.name)
        if settings.y2save_doh_enabled:
            configure_secure_dns_profile(profile, settings.y2save_doh_template)
        self._playwright = sync_playwright().start()
        launch_options = {
            "headless": settings.y2save_headless,
            "accept_downloads": True,
            "viewport": {"width": 1440, "height": 1000},
            "args": ["--disable-quic"],
        }
        if settings.browser_executable_path:
            launch_options["executable_path"] = settings.browser_executable_path
        if settings.production_mode:
            launch_options["chromium_sandbox"] = True
        self._context = self._playwright.chromium.launch_persistent_context(
            str(profile),
            **launch_options,
        )
        self._page = self._find_existing_page() or self._context.new_page()
        self._ensure_site(force_navigation=True)

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            try:
                if self._playwright:
                    self._playwright.stop()
            finally:
                self._playwright = self._context = self._page = None
                if self._profile:
                    self._profile.cleanup()
                    self._profile = None

    def _find_existing_page(self) -> Page | None:
        assert self._context
        expected_host = urlparse(settings.y2save_url).netloc
        return next((page for page in self._context.pages if expected_host in page.url), None)

    def _ensure_site(self, force_navigation: bool = False) -> Page:
        if not self._context:
            self.start()
        assert self._context
        if not self._page or self._page.is_closed():
            self._page = self._find_existing_page() or self._context.new_page()
            force_navigation = True
        expected_host = urlparse(settings.y2save_url).netloc
        if force_navigation or expected_host not in self._page.url:
            self._page.goto(settings.y2save_url, wait_until="domcontentloaded", timeout=60_000)
        return self._page

    @staticmethod
    def _first_visible(candidates: list[Locator]) -> Locator:
        for candidate in candidates:
            try:
                if candidate.count() and candidate.first.is_visible():
                    return candidate.first
            except Exception:
                continue
        raise Y2SaveError("Y2Save page layout changed: required control was not found")

    def _input(self, page: Page) -> Locator:
        if settings.y2save_input_selector:
            return page.locator(settings.y2save_input_selector).first
        return self._first_visible(
            [
                page.locator('input[placeholder*="Paste" i]'),
                page.locator('input[placeholder*="YouTube" i]'),
                page.locator('input[placeholder*="link" i]'),
                page.locator('input[type="url"]'),
                page.locator('input[type="text"]'),
            ]
        )

    def _submit(self, page: Page) -> Locator:
        if settings.y2save_submit_selector:
            return page.locator(settings.y2save_submit_selector).first
        return self._first_visible(
            [
                page.get_by_role("button", name=re.compile(r"^(convert|start|download)$", re.I)),
                page.locator('button[type="submit"]'),
                page.locator('input[type="submit"]'),
                page.get_by_role("button", name=re.compile(r"mp4", re.I)),
            ]
        )

    def _click_submit(self, page: Page) -> None:
        button = self._submit(page)
        # The page enables the control asynchronously after validating the URL
        # and the rights checkbox. Waiting on its DOM state avoids a race with
        # the site's reactive re-render.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if button.is_visible() and button.is_enabled():
                    button.click()
                    return
            except Exception:
                button = self._submit(page)
            page.wait_for_timeout(250)
        raise Y2SaveError("Y2Save did not enable the Convert button")

    @staticmethod
    def _accept_disclaimer(page: Page) -> None:
        """Accept the site's explicit rights confirmation when it is present."""
        checkbox = page.locator('input[type="checkbox"]').first
        try:
            try:
                checkbox.wait_for(state="visible", timeout=5000)
            except TimeoutError:
                if not checkbox.count():
                    return
                raise
            if checkbox.count() and checkbox.is_visible() and not checkbox.is_checked():
                checkbox.check()
        except Exception as exc:
            raise Y2SaveError("Could not confirm the Y2Save copyright disclaimer") from exc

    def _download_control(self, page: Page) -> Locator:
        # This observed overlay offers an installer, not the converted video.
        advert = page.locator('.s4k-ov:visible')
        if advert.count() and advert.first.locator('.s4k-pri').count():
            close = advert.first.get_by_role('button', name='Close', exact=True)
            if close.count() == 1 and close.is_visible():
                close.click()
            else:
                raise Y2SaveError('Y2Save: рекламное окно закрывает результат; файл не выбран')
        if settings.y2save_download_selector:
            return page.locator(settings.y2save_download_selector).first
        # Quality buttons exist before conversion and must not be mistaken for
        # the final download action. Prefer actual links/download controls.
        candidates = [
            page.locator('a[download]'),
            page.get_by_role("link", name=re.compile(r"^\s*download\s*$", re.I)),
            page.get_by_role("button", name=re.compile(r"^\s*download\s*$", re.I)),
            page.locator('a[href^="http"]:has-text("1080P")'),
            page.locator('a[href^="http"]:has-text("720P")'),
        ]
        for candidate in candidates:
            for control in candidate.all():
                if control.is_visible() and not control.evaluate("el => !!el.closest('.s4k-ov')"):
                    return control
        raise Y2SaveError('Y2Save: готовая ссылка на видео пока не появилась')

    def _wait_download_control(self, page: Page, timeout_ms: int) -> Locator:
        deadline = time.monotonic() + timeout_ms / 1000
        last_error: Exception | None = None
        last_percent = None
        reported = False
        while time.monotonic() < deadline:
            self._raise_if_challenged(page)
            body = page.locator("body").inner_text(timeout=5000)
            lower = body.lower()
            if any(message in lower for message in (
                "conversion failed", "failed to convert", "video is unavailable", "video too long",
                "download failed", "download has failed", "failed to download",
                "unable to download", "could not download", "something went wrong", "ошибка конвертации",
            )):
                raise Y2SaveError("y2save отклонил конвертацию. Проверьте доступность и длину исходника")
            match = re.search(r"(?:processing|converting|обработка)[^\n%]{0,80}?(\d{1,3})\s*%", body, re.I)
            percent = min(100, int(match.group(1))) if match else None
            if percent != last_percent or not reported:
                self._progress("conversion", percent)
                last_percent = percent
                reported = True
            try:
                return self._download_control(page)
            except Exception as exc:
                last_error = exc
                page.wait_for_timeout(750)
        raise Y2SaveError(f"y2save не выдал MP4 за {timeout_ms // 1000} сек. Последний прогресс: {last_percent if last_percent is not None else 'не указан'}%") from last_error

    @staticmethod
    def _validate_youtube_url(value: str) -> None:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
            raise Y2SaveError("Only HTTPS YouTube links are accepted")
        if host == "youtu.be" and not parsed.path.strip("/"):
            raise Y2SaveError("YouTube link has no video id")
        if host != "youtu.be" and parsed.path not in {"/watch"} and not parsed.path.startswith(("/shorts/", "/live/")):
            raise Y2SaveError("Unsupported YouTube video URL")

    @staticmethod
    def _raise_if_challenged(page: Page) -> None:
        body = page.locator("body").inner_text(timeout=5_000).lower()
        markers = ("verify you are human", "unusual traffic", "проверка, что вы человек")
        if any(marker in body for marker in markers):
            raise DownloadBlocked("Downloader requested a human verification; complete it manually and retry")
        challenge = page.locator('iframe[src*="recaptcha"][title*="challenge"], iframe[src*="hcaptcha"]')
        if any(frame.is_visible() for frame in challenge.all()):
            raise DownloadBlocked("y2save требует ручного прохождения CAPTCHA")

    def _stream_direct_link(self, href: str, destination: Path, timeout_seconds: int) -> None:
        assert self._context
        cookie_jar = {item["name"]: item["value"] for item in self._context.cookies(href)}
        with httpx.stream(
            "GET",
            href,
            cookies=cookie_jar,
            follow_redirects=True,
            timeout=httpx.Timeout(timeout_seconds, read=timeout_seconds),
        ) as response:
            if response.status_code >= 400:
                if response.status_code in {401, 403, 429}:
                    raise DownloadBlocked(f"Сервер скачивания ограничил доступ: HTTP {response.status_code}")
                raise Y2SaveError(f"Сервер скачивания вернул HTTP {response.status_code}")
            with destination.open("wb") as target:
                for chunk in response.iter_bytes(1024 * 1024):
                    target.write(chunk)

    def download(self, youtube_url: str, destination: Path) -> Path:
        self._validate_youtube_url(youtube_url)
        page = None
        timeout_ms = settings.y2save_timeout_seconds * 1000
        temporary = destination.with_name(f".{destination.stem}.downloading{destination.suffix}")
        temporary.unlink(missing_ok=True)
        try:
            self._progress("connecting")
            self.start()
            page = self._ensure_site()
            self._raise_if_challenged(page)
            field = self._input(page)
            field.fill(youtube_url)
            self._accept_disclaimer(page)
            self._click_submit(page)
            control = self._wait_download_control(page, timeout_ms)
            self._raise_if_challenged(page)
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._progress("downloading")
            href = control.get_attribute("href", timeout=2_000)
            if href and href.startswith("http"):
                self._stream_direct_link(href, temporary, settings.y2save_timeout_seconds)
                if not temporary.exists() or temporary.stat().st_size < 1024:
                    raise Y2SaveError("Downloaded file is empty")
                temporary.replace(destination)
                return destination
            try:
                # Conversion can take minutes, but once the final control is visible
                # the browser should emit a download promptly.
                with page.expect_download(timeout=min(timeout_ms, 30_000)) as event:
                    control.click()
                event.value.save_as(str(temporary))
            except TimeoutError as exc:
                # Some versions open a direct media URL in a new tab instead.
                href = None
                if not href and self._context:
                    expected_host = urlparse(settings.y2save_url).netloc
                    media_page = next(
                        (
                            item
                            for item in reversed(self._context.pages)
                            if item is not page and item.url.startswith("http") and expected_host not in item.url
                        ),
                        None,
                    )
                    href = media_page.url if media_page else None
                if not href or not href.startswith("http"):
                    raise Y2SaveError("Y2Save did not start a file download") from exc
                self._stream_direct_link(href, temporary, settings.y2save_timeout_seconds)
            if not temporary.exists() or temporary.stat().st_size < 1024:
                raise Y2SaveError("Downloaded file is empty")
            temporary.replace(destination)
            return destination
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            # Keep the browser profile but discard only the broken tab.
            try:
                if page:
                    page.close()
            except Exception:
                pass
            # Reopen lazily on the next explicit attempt, retaining the original error.
            self._page = None
            if isinstance(exc, Y2SaveError):
                raise
            raise Y2SaveError(str(exc)) from exc

