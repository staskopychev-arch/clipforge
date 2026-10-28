"""Ordinary TikTok Studio UI. No TikTok publishing API or anti-bot bypass.

The UI may vary by account/region. Unknown controls, extra consent dialogs,
CAPTCHA, login prompts and missing publication receipts fail closed.
"""
import json
import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, expect, sync_playwright

from ..config import settings
from ..security import decrypt
from .browser_network import isolated_youtube_browser
from .media import validate_shorts_file

UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?lang=en"
log = logging.getLogger(__name__)
_policy_blocks = WeakKeyDictionary()
_cookie_handlers = WeakKeyDictionary()
_upload_responses = WeakKeyDictionary()
_post_refusals = WeakKeyDictionary()


class TikTokWebError(RuntimeError):
    pass


class TikTokWebSessionError(TikTokWebError):
    pass


class TikTokWebExpiredSessionError(TikTokWebSessionError):
    """A signed-out page, distinct from CAPTCHA or an account restriction."""
    pass


class TikTokWebPolicyError(TikTokWebError):
    """Platform policy rejection requires owner review, never an automatic retry."""
    pass


class TikTokWebTransferError(TikTokWebError):
    """The site explicitly rejected media transfer; never click Retry/Post."""
    pass


class TikTokWebPostRejected(TikTokWebError):
    def __init__(self, code, requires_owner_review):
        self.code = code
        self.requires_owner_review = requires_owner_review
        super().__init__(f'TikTok отклонил публикацию: код {code}. Публикация не подтверждена; повторная загрузка отключена.')


@contextmanager
def monitor_upload_policy(page):
    """Observe ordinary UI responses; HTTP 200 can contain a policy error.

    Retain only the known numeric code, not signed URLs, auth fields or bodies.
    This observes requests made by Studio; it does not call publishing APIs.
    """
    _policy_blocks[page] = False
    _upload_responses[page] = []
    _post_refusals[page] = None

    def observed(response):
        parsed = urlsplit(response.url)
        if parsed.scheme == 'https' and parsed.hostname in {'www.tiktok.com', 'tiktok.com'} and response.request.method == 'POST':
            # Only endpoint paths and numeric codes. Never retain signed queries,
            # request headers, cookie values, user IDs, or response bodies.
            entry = {'path': parsed.path, 'http_status': response.status if isinstance(response.status, int) else None}
            if 'json' in response.headers.get('content-type', '').lower():
                try:
                    payload = response.json()
                    nodes = [payload] if isinstance(payload, dict) else []
                    if nodes:
                        nodes += [payload.get('data', {}), payload.get('error', {}),
                            (payload.get('ResponseMetadata') or {}).get('Error', {})]
                    codes = []
                    for node in nodes:
                        if not isinstance(node, dict): continue
                        for key in ['status_code', 'statusCode', 'error_code', 'errorCode', 'CodeN']:
                            value = node.get(key)
                            if isinstance(value, int) or isinstance(value, str) and value.isdigit():
                                codes.append({'field': key, 'value': int(value)})
                    entry['codes'] = codes
                    if parsed.path == '/tiktok/web/project/post/v1/' and isinstance(payload, dict) and payload.get('status_code') in (21, '21'):
                        messages = ' '.join(str(payload.get(key, '')) for key in ['status_msg', 'status_message', 'message'])
                        data = payload.get('data')
                        if isinstance(data, dict):
                            messages += ' ' + ' '.join(str(data.get(key, '')) for key in ['status_msg', 'message'])
                        owner_review = bool(re.search(r'spam|risk|frequen|too.?many|too.?fast|limit|block|policy|captcha|verif|login|auth|denied|permission|restrict|prohibit|review', messages, re.I))
                        _post_refusals[page] = {'code': 21, 'requires_owner_review': owner_review}
                except Exception:
                    entry['body_unavailable'] = True
            trace = _upload_responses.get(page)
            if trace is not None:
                trace.append(entry)
                del trace[:-30]
        if parsed.scheme != "https" or parsed.hostname not in {"www.tiktok.com", "tiktok.com"} or parsed.path != "/top/v1":
            return
        if "json" not in response.headers.get("content-type", "").lower():
            return
        try:
            body = response.json()
            metadata = body.get("ResponseMetadata") if isinstance(body, dict) else None
            error = metadata.get("Error") if isinstance(metadata, dict) else None
            if isinstance(error, dict) and error.get("CodeN") in (30522, "30522"):
                _policy_blocks[page] = True
                log.warning("TikTok upload rejected by platform policy (30522); automatic retries disabled")
        except Exception:
            # An unavailable body is not evidence of success or of a block.
            return

    page.on("response", observed)
    try:
        yield
    finally:
        page.remove_listener("response", observed)
        _policy_blocks.pop(page, None)
        _upload_responses.pop(page, None)
        _post_refusals.pop(page, None)


@contextmanager
def tiktok_page(encrypted_session):
    try:
        state = json.loads(decrypt(encrypted_session))
        if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not state["cookies"]:
            raise ValueError()
        if any(c.get("domain", "").lstrip(".").lower() != "tiktok.com" and not c.get("domain", "").lstrip(".").lower().endswith(".tiktok.com") for c in state["cookies"]):
            raise ValueError()
    except Exception:
        raise TikTokWebSessionError("Импортируйте действующий JSON cookie своего TikTok-аккаунта") from None
    # This helper configures DNS only inside an isolated runtime profile. It does
    # not alter identity, browser fingerprints, proxies, system DNS or TLS checks.
    with sync_playwright() as driver, isolated_youtube_browser(driver, headless=settings.tiktok_web_headless) as browser:
        context = browser.new_context(storage_state=state, locale="en-US")
        try:
            page = context.new_page()
            page.set_default_timeout(15000)
            page.set_default_navigation_timeout(45000)
            with monitor_upload_policy(page):
                yield page
        finally:
            context.close()


def check_challenge(page):
    if _policy_blocks.get(page):
        raise TikTokWebPolicyError("TikTok заблокировал подготовку загрузки: 30522 — Policy requirements, request blocked. Post не нажат. Требуется проверка аккаунта/сессии владельцем или обращение в поддержку TikTok; автоматические повторы остановлены")
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.tiktok.com", "tiktok.com"}:
        raise TikTokWebSessionError("TikTok перенаправил на другую страницу; подтвердите вход вручную")
    body = page.locator("body").inner_text(timeout=5000).lower()
    markers = ("verify to continue", "verify you are human", "complete the puzzle", "drag the slider", "too many attempts", "unusual traffic", "пройдите проверку", "подтвердите, что вы")
    if any(marker in body for marker in markers):
        raise TikTokWebSessionError("TikTok требует ручной проверки; обход и автоматический повтор отключены")
    challenge = page.locator('iframe[src*="captcha"], [class*="captcha-verify"], .captcha_verify_container')
    if any(item.is_visible() for item in challenge.all()):
        raise TikTokWebSessionError("TikTok показал CAPTCHA; задача остановлена")
    if "/login" in parsed.path:
        raise TikTokWebExpiredSessionError("TikTok запросил вход; обновите cookie своего аккаунта")
    login = page.get_by_role("button", name=re.compile(r"^(Log in|Войти)$"))
    if any(item.is_visible() for item in login.all()):
        raise TikTokWebExpiredSessionError("TikTok не принял сессию; выполните обычный вход и обновите cookie")
    refusal = _post_refusals.get(page)
    if refusal:
        raise TikTokWebPostRejected(refusal['code'], refusal['requires_owner_review'])
    return body


def open_upload(page):
    if page not in _cookie_handlers:
        # Consent can arrive AFTER the file input/editor. Handle this observed
        # optional-cookie overlay during actionability checks, even when late.
        page.add_locator_handler(page.get_by_role("button", name="Decline optional cookies", exact=True), lambda locator: locator.click())
        page.add_locator_handler(page.get_by_text("Turn on automatic content checks?", exact=True), lambda _: dismiss_optional_check_setup(page))
        page.add_locator_handler(page.get_by_text("New editing features added", exact=True), lambda _: dismiss_editor_tip(page))
        _cookie_handlers[page] = True
    page.goto(UPLOAD_URL, wait_until="domcontentloaded")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        check_challenge(page)
        field = page.locator('input[type="file"]')
        if field.count() == 1:
            return field
        page.wait_for_timeout(500)
    raise TikTokWebError("Не найдена однозначная форма загрузки TikTok Studio. Файл не отправлен")


def check_session(encrypted_session):
    try:
        with tiktok_page(encrypted_session) as page:
            open_upload(page)
            # The file input can precede Studio's asynchronous permission result.
            # Do not mark an account ready before a late policy refusal appears.
            for _ in range(10):
                page.wait_for_timeout(500)
                check_challenge(page)
            return {"authenticated": True, "platform": "tiktok_web", "upload_permission_verified": False}
    except TikTokWebError:
        raise
    except Exception:
        raise TikTokWebError("Не удалось открыть TikTok Studio; проверьте соединение с сервером") from None


def caption_field(page):
    # Prefer the explicit caption container; fallback is allowed only if exactly
    # one editable field is visible. Never type into an arbitrary first match.
    for selector in ('[data-e2e="caption_container"] [contenteditable="true"]:visible', '[data-e2e="upload-caption"] [contenteditable="true"]:visible', '[contenteditable="true"][role="textbox"]:visible', '[contenteditable="true"]:visible'):
        field = page.locator(selector)
        if field.count() == 1:
            return field
    raise TikTokWebError("Не найдено однозначное поле описания TikTok; публикация не нажата")


def set_visibility(page, visibility):
    label = {"private": "Only you", "public": "Everyone"}.get(visibility)
    if not label:
        raise TikTokWebError("Неверная видимость TikTok")
    # Current Studio says "Who can see this post". Keep the previous supported
    # labels, but still scope all interactions to the uniquely labelled region.
    name = re.compile(r"^Who can (?:watch|view|see) this (?:video|post)\??$", re.I)
    native = page.get_by_role("combobox", name=name)
    if native.count() == 1 and native.evaluate("el => el.tagName") == "SELECT":
        native.select_option(label=label)
        if native.locator("option:checked").inner_text().strip() != label:
            raise TikTokWebError("TikTok не подтвердил видимость видео")
        return
    heading = page.get_by_text(name, exact=True)
    try:
        heading.wait_for(state="visible", timeout=15000)
    except PlaywrightTimeoutError:
        raise TikTokWebError("Не найдены настройки видимости. Публикация остановлена, чтобы не менять её случайно") from None
    if heading.count() != 1:
        raise TikTokWebError("Не найдены настройки видимости. Публикация остановлена, чтобы не менять её случайно")
    region = heading.locator("..")
    for _ in range(3):
        combo = region.get_by_role("combobox")
        if combo.count() != 1:
            combo = region.get_by_text(re.compile(r"^(Everyone|Friends|Only you)$"), exact=True)
        if combo.count() == 1 and combo.first.is_visible():
            if combo.first.inner_text().strip() == label:
                return
            combo.first.click()
            option = page.get_by_role("option", name=label, exact=True)
            if option.count() != 1:
                option = page.get_by_text(label, exact=True)
            if option.count() != 1 or not option.first.is_visible():
                raise TikTokWebError("Не найден однозначный вариант видимости; публикация остановлена")
            option.first.click()
            try:
                expect(combo.first).to_have_text(label, timeout=5000)
            except (AssertionError, PlaywrightTimeoutError):
                raise TikTokWebError("TikTok не подтвердил выбранную видимость") from None
            return
        region = region.locator("..")
    raise TikTokWebError("Интерфейс видимости TikTok изменился; автоматическая публикация остановлена")


def canonical_video_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in {"www.tiktok.com", "tiktok.com"} or parsed.username or parsed.password:
        return None
    if re.fullmatch(r"/@[A-Za-z0-9_.]{1,64}/video/[0-9]{10,25}", parsed.path):
        return "https://www.tiktok.com" + parsed.path
    return None


def check_upload_failure(page):
    """A known UI error takes priority over missing controls or disabled Post."""
    body = page.locator("body").inner_text(timeout=5000).lower()
    if any(marker in body for marker in ("upload failed", "couldn’t upload", "couldn't upload", "не удалось загрузить")):
        # Only public destination hosts/statuses are collected, never complete
        # request URLs, signatures, cookies, request/response bodies or headers.
        try:
            failures = page.evaluate("""() => performance.getEntriesByType('resource').filter(e => e.responseStatus >= 400).map(e => ({host: new URL(e.name).hostname, status: e.responseStatus})).slice(-12)""")
            log.warning("TikTok rejected upload; failed resource hosts/statuses: %s", json.dumps(failures))
        except Exception:
            pass
        raise TikTokWebTransferError("TikTok отклонил загрузку файла: «Couldn't upload video». Кнопка Post не нажата; автоматические Retry и повторная отправка отключены")


def dismiss_editor_tip(page):
    """Dismiss only the observed informational editor tour, not consent/security."""
    title = page.get_by_text("New editing features added", exact=True)
    if title.count() != 1 or not title.is_visible():
        return
    # Called as an overlay handler. Known optional overlays may be stacked;
    # close the upper offer first while Playwright pauses other handlers.
    decline = page.get_by_role("button", name="Decline optional cookies", exact=True)
    if decline.count() == 1 and decline.is_visible():
        decline.click()
    dismiss_optional_check_setup(page)
    region = title
    for _ in range(7):
        if region.count() != 1:
            break
        text = region.inner_text()
        button = region.get_by_text("Got it", exact=True)
        if "easier than ever before" in text and "professional and engaging videos" in text and button.count() == 1 and button.is_visible():
            # Studio uses a clickable div/span for this control in some accounts.
            button.click()
            title.wait_for(state="hidden", timeout=5000)
            return
        region = region.locator("..")
    raise TikTokWebError("Неоднозначная подсказка редактора; Post не нажат")


def dismiss_optional_check_setup(page):
    """Decline only the observed optional onboarding offer, never a challenge."""
    title = page.get_by_text("Turn on automatic content checks?", exact=True)
    if title.count() != 1 or not title.is_visible():
        return
    decline = page.get_by_role("button", name="Decline optional cookies", exact=True)
    if decline.count() == 1 and decline.is_visible():
        decline.click()
    region = title
    for _ in range(7):
        if region.count() != 1:
            break
        text = region.inner_text()
        cancel = region.get_by_role("button", name="Cancel", exact=True)
        enable = region.get_by_role("button", name="Turn on", exact=True)
        normalized_text = text.replace("’", "'")
        known_offer = ("Music copyright check" in text or (
            "We'll automatically check your video for copyright and other potential violations." in normalized_text
            and "You can adjust these settings anytime in Profile > Settings" in text))
        if known_offer and "Content check lite" in text and cancel.count() == 1 and enable.count() == 1 and cancel.is_visible():
            cancel.click()
            title.wait_for(state="hidden", timeout=5000)
            return
        region = region.locator("..")
    raise TikTokWebError("Неоднозначное окно настройки проверок; Post не нажат")


def studio_receipt(page, description, visibility, on_progress, previous_urls):
    """Read the newly created post and its saved privacy from ordinary Studio UI."""
    page.goto("https://www.tiktok.com/tiktokstudio/content?lang=en", wait_until="domcontentloaded")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        check_challenge(page)
        matches = []
        for anchor in page.locator('a[href*="/video/"]').all():
            url = canonical_video_url(anchor.evaluate("el => el.href"))
            if url and url not in previous_urls and " ".join(description.split()) == " ".join(anchor.inner_text().split()):
                matches.append((anchor, url))
        if len(matches) == 1:
            anchor, url = matches[0]
            row = anchor
            for _ in range(12):
                text = row.inner_text()
                if row.locator('a[href*="/video/"]').count() == 1 and re.search(r"(?:^|\n)(Everyone|Only me|Only you|Friends)(?:\n|$)", text):
                    break
                row = row.locator("..")
            else:
                page.wait_for_timeout(1000)
                continue
            # The URL itself is a durable creation receipt even when moderation
            # prevents the requested public visibility. Never upload it again.
            on_progress("published", url)
            saved = re.search(r"(?:^|\n)(Everyone|Only me|Only you|Friends)(?:\n|$)", text).group(1)
            return {"confirmed": True, "video_url": url, "requested_visibility": visibility,
                    "visibility": "public" if saved == "Everyone" else "friends" if saved == "Friends" else "private",
                    "public_visibility_confirmed": saved == "Everyone",
                    "moderation_status": "under_review" if "Content under review" in text else "not_reported",
                    "confirmation_method": "Studio Posts readback"}
        if len(matches) > 1:
            raise TikTokWebError("Несколько новых публикаций с этим описанием; нужна ручная проверка Studio")
        page.wait_for_timeout(1000)
    raise TikTokWebError("Post нажат, но новая публикация не подтверждена в Studio. Повторная отправка заблокирована")


def _post(page, description, visibility, on_progress, previous_urls=None):
    check_challenge(page)
    check_upload_failure(page)
    try:
        field = caption_field(page)
        field.click()
        field.press("ControlOrMeta+A")
        page.keyboard.insert_text(description)
        if field.inner_text().strip() != description.strip():
            raise TikTokWebError("TikTok не сохранил описание; Post не нажат")
        set_visibility(page, visibility)
    except (PlaywrightTimeoutError, TikTokWebError):
        # A transfer failure can appear AFTER the initial check and cover the
        # visibility control. Report the actual platform error, not a timeout.
        check_challenge(page)
        check_upload_failure(page)
        raise
    deadline = time.monotonic() + settings.tiktok_web_timeout_seconds
    post = page.get_by_role("button", name="Post", exact=True)
    while time.monotonic() < deadline:
        check_challenge(page)
        check_upload_failure(page)
        uploaded = page.get_by_text(re.compile(r"^Uploaded\s*[（(]", re.I))
        # Post can become enabled at 99%; require Studio's completed transfer.
        uploading = page.get_by_text(re.compile(r"^(?:[0-9]|[1-9][0-9])%$"))
        if any(item.is_visible() for item in uploaded.all()) and not any(item.is_visible() for item in uploading.all()) and post.count() == 1 and post.is_visible() and post.is_enabled() and post.get_attribute("aria-disabled") != "true":
            break
        page.wait_for_timeout(1000)
    else:
        raise TikTokWebError("TikTok не разрешил публикацию за отведённое время; проверьте загруженный файл в Studio")
    # The reservation is durable BEFORE the irreversible Post click.
    on_progress("submitting", None)
    post.click()
    continued = False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        check_challenge(page)
        success = page.get_by_text("Your video has been uploaded", exact=True)
        manage = page.get_by_role("button", name="Manage posts", exact=True)
        if (success.count() == 1 and success.is_visible() and manage.count() == 1 and manage.is_visible()
                and page.get_by_text("You can manage your posts or upload another video.", exact=True).is_visible()):
            # The current success panel has no dialog role or permalink.
            # Read the actual Studio row; the panel alone is not a receipt.
            return studio_receipt(page, description, visibility, on_progress, previous_urls or set())
        for dialog in page.get_by_role("dialog").all():
            if dialog.is_visible() and "Continue to post?" in dialog.inner_text() and "copyright check is incomplete" in dialog.inner_text().lower():
                button = dialog.get_by_role("button", name="Post now", exact=True)
                if not continued and button.count() == 1 and button.is_visible():
                    continued = True
                    button.click()
        if "/tiktokstudio/content" in urlsplit(page.url).path:
            return studio_receipt(page, description, visibility, on_progress, previous_urls or set())
        direct = canonical_video_url(page.url)
        if direct:
            if visibility == "public":
                return studio_receipt(page, description, visibility, on_progress, previous_urls or set())
            on_progress("published", direct)
            return {"confirmed": True, "video_url": direct, "visibility": visibility}
        # A success dialog plus a unique video permalink is a receipt. A toast,
        # closed upload form or content-list navigation alone is NOT proof.
        for dialog in page.get_by_role("dialog").all():
            text = dialog.inner_text().lower()
            if not any(marker in text for marker in ("video uploaded", "video has been uploaded", "video posted", "successfully posted")):
                continue
            links = {url for href in dialog.locator("a[href]").evaluate_all("items => items.map(item => item.href)") if (url := canonical_video_url(href))}
            if len(links) == 1:
                url = links.pop()
                if visibility == "public":
                    return studio_receipt(page, description, visibility, on_progress, previous_urls or set())
                on_progress("published", url)
                return {"confirmed": True, "video_url": url, "visibility": visibility}
        page.wait_for_timeout(1000)
    raise TikTokWebError("Post нажат, но ссылка публикации не подтверждена. Проверьте TikTok Studio; повторная отправка заблокирована")


def _upload_on_page(page, video_path, description, visibility, on_progress):
    page.goto("https://www.tiktok.com/tiktokstudio/content?lang=en", wait_until="domcontentloaded")
    page.wait_for_timeout(5000)
    check_challenge(page)
    previous_urls = {url for href in page.locator('a[href*="/video/"]').evaluate_all("items => items.map(el => el.href)") if (url := canonical_video_url(href))}
    file_input = open_upload(page)
    on_progress("authenticated", None)
    on_progress("uploading", None)
    file_input.set_input_files(str(video_path.resolve()))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        check_challenge(page)
        check_upload_failure(page)
        try:
            caption_field(page)
            break
        except TikTokWebError:
            page.wait_for_timeout(500)
    return _post(page, description, visibility, on_progress, previous_urls)


def save_failure_diagnostics(page, diagnostic_id, error):
    if not diagnostic_id or not re.fullmatch(r"[a-f0-9-]{36}", diagnostic_id):
        return
    try:
        folder = settings.media_root / "qa" / ("tiktok-web-" + diagnostic_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        page.screenshot(path=str(folder / "failure.png"), full_page=True, timeout=5000)
        data = {"error_type": type(error).__name__, "path": urlsplit(page.url).path,
                "body": page.locator("body").inner_text(timeout=5000)[:15000],
                "network_metadata": list(_upload_responses.get(page, []))}
        data['post_refusal'] = _post_refusals.get(page)
        path = folder / "failure.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        path.chmod(0o600)
        log.warning("TikTok private UI diagnostics saved for job %s (%s)", diagnostic_id, type(error).__name__)
    except Exception:
        log.warning("TikTok UI diagnostics unavailable for job %s", diagnostic_id)


def upload_video(*, encrypted_session, video_path: Path, description, visibility, on_progress, diagnostic_id=None):
    if visibility not in {"private", "public"} or not 1 <= len(description.strip()) <= 2200:
        raise TikTokWebError("Укажите видимость и описание длиной 1–2200 символов")
    validate_shorts_file(video_path)
    try:
        with tiktok_page(encrypted_session) as page:
            try:
                return _upload_on_page(page, video_path, description, visibility, on_progress)
            except Exception as exc:
                save_failure_diagnostics(page, diagnostic_id, exc)
                raise
    except TikTokWebError:
        raise
    except Exception:
        # Playwright's raw error may include storage_state/caption values.
        raise TikTokWebError("Сетевая ошибка или изменившаяся форма TikTok. Проверьте Studio перед повтором") from None
