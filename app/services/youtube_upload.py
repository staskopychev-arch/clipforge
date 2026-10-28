"""Ordinary YouTube Studio upload using a user-authorized browser session.

No fingerprint modification, CAPTCHA solving, or automated account creation.
The receipt is recorded before Save, so a lost response cannot duplicate a video.
"""
import json
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, parse_qs

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from ..config import settings
from ..security import decrypt
from .browser_network import isolated_youtube_browser


class YouTubeUploadError(RuntimeError):
    pass


class YouTubeSessionError(YouTubeUploadError):
    pass


@contextmanager
def youtube_page(encrypted_session: str):
    try:
        state = json.loads(decrypt(encrypted_session))
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not state["cookies"]:
            raise ValueError("No cookies")
    except Exception as exc:
        raise YouTubeSessionError("Не удалось прочитать сессию; импортируйте JSON заново") from exc
    with sync_playwright() as driver:
        with isolated_youtube_browser(driver, headless=settings.youtube_upload_headless) as browser:
            context = browser.new_context(storage_state=state, locale="en-US")
            try:
                page = context.new_page()
                page.set_default_timeout(30_000)
                page.set_default_navigation_timeout(settings.youtube_studio_timeout_seconds * 1000)
                yield page
            finally:
                context.close()


def check_challenge(page: Page) -> None:
    if "accounts.google.com/" in page.url:
        raise YouTubeSessionError("YouTube запросил вход: импортированная сессия не авторизована")
    body = page.locator("body").inner_text(timeout=5000).lower()
    if any(marker in body for marker in ("verify it's you", "verify it’s you", "unusual traffic", "verify you are human", "подтвердите, что это вы")):
        raise YouTubeSessionError("YouTube требует ручной проверки входа")
    challenge = page.locator('iframe[src*="recaptcha"][title*="challenge"], iframe[src*="hcaptcha"]')
    if any(item.is_visible() for item in challenge.all()):
        raise YouTubeSessionError("YouTube запросил CAPTCHA; завершите проверку вручную")
    if "create a channel" in body or "создайте канал" in body:
        raise YouTubeSessionError("Для этого аккаунта нужно вручную создать YouTube-канал")


def dismiss_studio_welcome(page: Page) -> None:
    """Dismiss only Studio's informational welcome, never a security prompt."""
    check_challenge(page)
    welcome = page.get_by_text("Welcome to YouTube Studio", exact=True)
    if not welcome.count() or not welcome.first.is_visible():
        return
    button = page.get_by_role("button", name="Continue", exact=True)
    if button.count() != 1 or not button.is_visible():
        raise YouTubeUploadError("Не удалось найти кнопку стартового окна YouTube Studio")
    button.click()
    welcome.first.wait_for(state="hidden", timeout=15_000)
    check_challenge(page)


def studio_ready(page: Page) -> bool:
    """Custom-element hosts can be zero-sized while their shadow UI is visible."""
    url = urlsplit(page.url)
    if url.hostname != "studio.youtube.com" or not re.match(r"^/channel/UC[A-Za-z0-9_-]+(?:/|$)", url.path):
        return False
    if not page.locator("ytcp-app").count():
        return False
    create = page.get_by_role("button", name="Create", exact=True)
    return bool(create.count() and create.first.is_visible())


def open_studio(page: Page) -> None:
    page.goto("https://studio.youtube.com", wait_until="domcontentloaded")
    deadline = time.monotonic() + settings.youtube_studio_timeout_seconds
    while time.monotonic() < deadline:
        try:
            check_challenge(page)
            body = page.locator("body").inner_text(timeout=5000).lower()
        except PlaywrightTimeoutError:
            # Studio may redirect again after DOMContentLoaded; an absent body
            # during that transition is not a failed login. Never act until ready.
            page.wait_for_timeout(500)
            continue
        if "you are using an unsupported browser" in body:
            raise YouTubeUploadError("YouTube не поддерживает текущий режим браузера; используйте обычный Chrome (YOUTUBE_UPLOAD_HEADLESS=false)")
        dismiss_studio_welcome(page)
        if studio_ready(page):
            return
        page.wait_for_timeout(500)
    # A missing UI is not evidence that the account cookies are invalid.
    raise YouTubeUploadError("Интерфейс YouTube Studio не загрузился за отведённое время; сессия не признана недействительной")


def open_upload_form(page: Page) -> None:
    """Open the upload dialog without transmitting a media file."""
    page.goto("https://www.youtube.com/upload", wait_until="domcontentloaded")
    deadline = time.monotonic() + settings.youtube_studio_timeout_seconds
    while time.monotonic() < deadline:
        check_challenge(page)
        # File inputs are intentionally hidden; only attachment is required.
        # The upload modal may cover a welcome dialog left in the background.
        # Never click through an overlay or force-click its obscured controls.
        select_files = page.get_by_role("button", name="Select files", exact=True)
        if (page.locator('input[type="file"]').count()
                and select_files.count() and select_files.first.is_visible()):
            return
        dismiss_studio_welcome(page)
        page.wait_for_timeout(500)
    raise YouTubeUploadError("YouTube не показал форму загрузки видео")


def check_session(encrypted_session: str) -> dict:
    with youtube_page(encrypted_session) as page:
        open_studio(page)
        return {"authenticated": True, "url": page.url}


def connection_error(exc: Exception) -> str:
    text = str(exc)
    if "ERR_NAME_NOT_RESOLVED" in text:
        return "Не удаётся разрешить адрес YouTube (DNS). Проверьте доступ к studio.youtube.com с сервера"
    if "ERR_NETWORK_ACCESS_DENIED" in text or "10013" in text:
        return "Сеть блокирует соединение с YouTube Studio"
    return "Не удалось подключиться к YouTube Studio; проверьте соединение"


def _wait_visible(page: Page, selector: str, timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_challenge(page)
        locator = page.locator(selector)
        if locator.count() and locator.first.is_visible():
            return
        page.wait_for_timeout(500)
    raise YouTubeUploadError(f"YouTube не показал ожидаемый элемент: {selector}")


def canonical_video_url(href: str) -> str | None:
    parsed = urlsplit(href)
    if parsed.hostname not in {"youtube.com", "www.youtube.com", "youtu.be"}:
        return None
    video_id = (parsed.path.strip("/") if parsed.hostname == "youtu.be" else
                parsed.path.removeprefix("/shorts/") if parsed.path.startswith("/shorts/") else
                parse_qs(parsed.query).get("v", [""])[0])
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return f"https://www.youtube.com/watch?v={video_id}"
    return None


def _video_url(page: Page) -> str | None:
    for href in page.locator("ytcp-uploads-dialog a[href], ytcp-video-upload-dialog a[href]").evaluate_all("items => items.map(item => item.href)"):
        url = canonical_video_url(href)
        if url:
            return url
    return None


def _confirm_saved(page: Page, *, content_url: str, url: str, title: str, visibility: str) -> None:
    """Read back the saved video from Studio; never clicks Save or uploads."""
    video_id = parse_qs(urlsplit(url).query)["v"][0]
    page.goto(content_url, wait_until="domcontentloaded")
    link = page.locator(f'a[href="/video/{video_id}/edit"]').filter(has_text=title)
    link.first.wait_for(state="visible", timeout=30_000)
    dismiss_studio_welcome(page)
    if link.count() != 1:
        raise YouTubeUploadError("Не удалось однозначно подтвердить сохранённое видео")
    link.click()
    field = page.locator("ytcp-video-metadata-visibility #visibility-text")
    field.wait_for(state="visible", timeout=30_000)
    check_challenge(page)
    if field.inner_text().strip().lower() != visibility:
        raise YouTubeUploadError("Сохранённая видимость не совпадает с запрошенной; проверьте Studio")


def _finish_upload(page: Page, *, title: str, description: str, visibility: str,
                   made_for_kids: bool, on_progress: Callable, expected_url: str | None = None) -> dict:
    """Finalize the current wizard; never uploads a file or retries Save."""
    channel = re.match(r"^(/channel/UC[A-Za-z0-9_-]+)(?:/|$)", urlsplit(page.url).path)
    if urlsplit(page.url).hostname != "studio.youtube.com" or not channel:
        raise YouTubeUploadError("Не удалось определить канал для проверки сохранения")
    content_url = f"https://studio.youtube.com{channel.group(1)}/content"
    _wait_visible(page, "#title-textarea #textbox", timeout=60)
    actual_url = _video_url(page)
    if expected_url and actual_url != expected_url:
        raise YouTubeUploadError("Открыт другой черновик; сохранение остановлено")
    if actual_url:
        on_progress("uploading", actual_url)
    page.locator("#title-textarea #textbox").fill(title.strip())
    page.locator("#description-textarea #textbox").fill(description)
    kids_value = "VIDEO_MADE_FOR_KIDS_MFK" if made_for_kids else "VIDEO_MADE_FOR_KIDS_NOT_MFK"
    page.locator(f'tp-yt-paper-radio-button[name="{kids_value}"]').click()
    for _ in range(3):
        check_challenge(page)
        page.locator("#next-button").click()
    privacy = page.locator(f'tp-yt-paper-radio-button[name="{visibility.upper()}"]')
    privacy.wait_for(state="visible")
    privacy.click()
    deadline = time.monotonic() + settings.youtube_upload_timeout_seconds
    done = page.locator("#done-button")
    url = actual_url
    while time.monotonic() < deadline:
        check_challenge(page)
        url = _video_url(page) or url
        if expected_url and url != expected_url:
            raise YouTubeUploadError("Идентификатор черновика изменился; сохранение остановлено")
        progress = page.locator("ytcp-video-upload-progress")
        text = progress.inner_text().lower() if progress.count() else ""
        if any(word in text for word in ("upload failed", "upload blocked", "daily upload limit", "не удалось загрузить")):
            raise YouTubeUploadError("YouTube отклонил загрузку; проверьте ограничения канала")
        # Studio explicitly permits Save while checks continue in the background.
        # This is not a successful-check assertion; YouTube's enabled Save remains required.
        if (done.is_enabled() and done.get_attribute("aria-disabled") != "true" and url
                and any(word in text for word in ("upload complete", "processing", "checks complete",
                                                  "checking", "checks completed", "checks are taking longer"))):
            break
        page.wait_for_timeout(1000)
    else:
        raise YouTubeUploadError("YouTube не завершил загрузку/обработку за отведённое время")
    on_progress("submitting", url)  # Persist the receipt BEFORE the irreversible click.
    done.click()
    # Do not infer success merely from a closed dialog. Observe a receipt first.
    try:
        # The title field is already hidden on Visibility, so it cannot confirm Save.
        done.wait_for(state="hidden", timeout=60_000)
        _confirm_saved(page, content_url=content_url, url=url, title=title.strip(), visibility=visibility)
    except Exception as exc:
        raise YouTubeUploadError("YouTube не подтвердил сохранение. Проверьте ссылку в журнале перед повтором") from exc
    on_progress("published", url)
    return {"video_url": url, "visibility": visibility, "confirmed": True}


def resume_draft(*, encrypted_session: str, video_url: str, title: str, description: str,
                 visibility: str, made_for_kids: bool, on_progress: Callable) -> dict:
    """Open exactly an existing draft ID in the authenticated channel, without a file input."""
    url = canonical_video_url(video_url)
    if not url or visibility not in {"private", "unlisted", "public"}:
        raise YouTubeUploadError("Неверная ссылка на черновик или видимость")
    video_id = parse_qs(urlsplit(url).query)["v"][0]
    with youtube_page(encrypted_session) as page:
        open_studio(page)
        channel_path = urlsplit(page.url).path.rstrip("/")
        page.goto(f"https://studio.youtube.com{channel_path}/content", wait_until="domcontentloaded")
        title_link = page.locator(f'a[href="/content?d=ud&udvid={video_id}"]').filter(has_text=title)
        title_link.first.wait_for(state="visible", timeout=30_000)
        dismiss_studio_welcome(page)
        if title_link.count() != 1 or "Draft" not in title_link.locator("xpath=../..").inner_text():
            raise YouTubeUploadError("Не найден однозначный черновик с этим названием; повторная загрузка запрещена")
        title_link.click()
        return _finish_upload(page, title=title, description=description, visibility=visibility,
                              made_for_kids=made_for_kids, on_progress=on_progress, expected_url=url)


def upload_video(
    *, encrypted_session: str, video_path: Path, title: str, description: str,
    visibility: str, made_for_kids: bool,
    on_progress: Callable[[str, str | None], None],
) -> dict:
    if visibility not in {"private", "unlisted", "public"}:
        raise YouTubeUploadError("Неверная видимость видео")
    if not video_path.is_file() or video_path.stat().st_size == 0:
        raise YouTubeUploadError("Файл видео отсутствует или пуст")
    if not title.strip() or len(title) > 100 or len(description) > 5000:
        raise YouTubeUploadError("Заголовок должен быть 1–100 символов, описание — до 5000")
    from .media import validate_shorts_file
    validate_shorts_file(video_path)

    with youtube_page(encrypted_session) as page:
        open_studio(page)
        on_progress("authenticated", None)
        open_upload_form(page)
        page.locator('input[type="file"]').first.set_input_files(str(video_path.resolve()))
        on_progress("uploading", None)
        return _finish_upload(page, title=title, description=description, visibility=visibility,
                              made_for_kids=made_for_kids, on_progress=on_progress)
