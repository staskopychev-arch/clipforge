"""Owned-account sign-in, one browser and one attempt at a time.

No stealth, proxy rotation, CAPTCHA solving, retries of rejected credentials, or
automatic acceptance of security/consent prompts. A challenge blocks the queue.
Browser control is available only to the authenticated administrator while paused.
"""
import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError, async_playwright
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import Account, AccountLogin, RecordStatus
from ..security import decrypt, encrypt
from .browser_network import configure_secure_dns_profile

log = logging.getLogger(__name__)
PENDING = ("queued", "processing", "awaiting_verification")
VIEWPORT = {"width": 1100, "height": 760}
MAX_IMPORT = 50
TRUSTED_HOSTS = {"accounts.google.com", "studio.youtube.com", "www.youtube.com", "www.google.com", "consent.youtube.com", "consent.google.com"}
SIGNIN_URL = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fstudio.youtube.com%2F&hl=en"


class LoginQueueError(ValueError):
    """Only static, credential-free messages are safe to surface to the panel."""


def identity_key(login):
    return hmac.new(settings.app_secret.encode(), login.lower().encode(), hashlib.sha256).hexdigest()


def parse_credentials(raw: bytes, filename: str) -> list[dict]:
    if not raw or len(raw) > 1024 * 1024:
        raise LoginQueueError("Файл должен быть непустым и не превышать 1 МБ")
    try:
        content = raw.decode("utf-8-sig")
        if filename.lower().endswith(".csv"):
            records = list(csv.DictReader(io.StringIO(content), strict=True))
        else:
            value = json.loads(content)
            records = value.get("accounts") if isinstance(value, dict) else value
    except (UnicodeError, ValueError, csv.Error):
        raise LoginQueueError("Некорректный JSON/CSV; используйте UTF-8 и поля login,password") from None
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_IMPORT:
        raise LoginQueueError("В файле должно быть от 1 до 50 ваших аккаунтов")
    result, seen = [], set()
    for index, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise LoginQueueError(f"Запись #{index}: ожидается объект")
        login = record.get("login", record.get("email"))
        password = record.get("password")
        name = record.get("display_name", f"Аккаунт {index}")
        if (not isinstance(login, str) or not re.fullmatch(r"[^\s@]{1,120}@[^\s@]{1,120}\.[^\s@]{2,30}", login.strip())
                or not isinstance(password, str) or not 1 <= len(password) <= 1024
                or "\x00" in password or "\n" in password or "\r" in password
                or not isinstance(name, str) or not 1 <= len(name.strip()) <= 120):
            raise LoginQueueError(f"Запись #{index}: проверьте login, password и display_name")
        key = identity_key(login.strip())
        if key in seen:
            raise LoginQueueError(f"Запись #{index}: повтор аккаунта в файле")
        seen.add(key)
        result.append({"login": login.strip(), "password": password, "display_name": name.strip(), "identity_key": key})
    return result


def import_credentials(db, records):
    count = len(db.scalars(select(AccountLogin.id).where(AccountLogin.status.in_(PENDING))).all())
    if count + len(records) > MAX_IMPORT:
        raise LoginQueueError("В очереди уже есть аккаунты: максимум 50 ожидающих записей")
    keys = {r["identity_key"] for r in records}
    if db.scalar(select(AccountLogin.id).where(AccountLogin.status.in_(PENDING), AccountLogin.identity_key.in_(keys)).limit(1)):
        raise LoginQueueError("Один из аккаунтов уже ожидает входа; повтор не добавлен")
    batch = str(uuid.uuid4())
    for position, record in enumerate(records):
        db.add(AccountLogin(batch_id=batch, position=position, display_name=record["display_name"],
            identity_key=record["identity_key"], login_ciphertext=encrypt(record["login"]),
            password_ciphertext=encrypt(record["password"])))
    db.commit()
    return {"imported": len(records), "batch_id": batch}


def public_record(row):
    return {"id": row.id, "display_name": row.display_name, "status": row.status,
            "message": row.message, "account_id": row.account_id}


def trusted_url(url):
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname in TRUSTED_HOSTS and not parsed.username and not parsed.password and parsed.port in (None, 443)


async def authenticated_channel(page):
    parsed = urlsplit(page.url)
    match = re.match(r"^/channel/(UC[A-Za-z0-9_-]+)(?:/|$)", parsed.path)
    if parsed.hostname != "studio.youtube.com" or not match:
        return None
    if not await page.locator("ytcp-app").count():
        return None
    create = page.get_by_role("button", name="Create", exact=True)
    return match.group(1) if await create.count() and await create.first.is_visible() else None


def filtered_storage(state):
    allowed = ("google.com", "youtube.com", "googlevideo.com", "ytimg.com")
    def valid_host(host):
        return any(host == suffix or host.endswith("." + suffix) for suffix in allowed)
    cookies = [c for c in state.get("cookies", []) if valid_host(c.get("domain", "").lower().lstrip("."))]
    origins = [o for o in state.get("origins", []) if urlsplit(o.get("origin", "")).scheme == "https" and valid_host(urlsplit(o["origin"]).hostname or "")]
    if not cookies:
        raise LoginQueueError("Google не выдал сохраняемую сессию; очередь остановлена")
    return {"cookies": cookies, "origins": origins}


class LoginQueue:
    """Single-process controller: deployment deliberately runs one web worker.

    Queue rows persist, but a restart never resumes a password submission. Only
    a new explicit Start can run unattempted rows. Media workers remain independent.
    """
    def __init__(self, session_factory=SessionLocal):
        self.db = session_factory
        self.task = None
        self.active_id = None
        self.page = None
        self.context = None
        self.waiting = False
        self.resume_event = None
        self.reason = "Очередь не запущена"
        self.controls = asyncio.Lock()

    @property
    def running(self):
        return bool(self.task and not self.task.done())

    def recover(self):
        with self.db() as db:
            for row in db.scalars(select(AccountLogin).where(AccountLogin.status.in_(("processing", "awaiting_verification")))).all():
                row.status = "interrupted"
                row.message = "Сервис перезапущен; незавершённый вход не повторяется. Импортируйте запись заново."
                row.login_ciphertext = row.password_ciphertext = None
            db.commit()

    def state(self):
        with self.db() as db:
            rows = db.scalars(select(AccountLogin).order_by(AccountLogin.created_at.desc(), AccountLogin.position).limit(150)).all()
            return {"running": self.running, "active_id": self.active_id, "waiting": self.waiting,
                    "message": self.reason, "records": [public_record(row) for row in rows]}

    def start(self):
        if (os.environ.get("PWDEBUG", "0") not in {"", "0"}
                or "pw:" in os.environ.get("DEBUG", "")):
            raise LoginQueueError("Отключите Playwright DEBUG/PWDEBUG перед работой с паролями")
        if self.running:
            raise LoginQueueError("Очередь уже работает")
        with self.db() as db:
            if not db.scalar(select(AccountLogin.id).where(AccountLogin.status == "queued").limit(1)):
                raise LoginQueueError("Нет ожидающих аккаунтов")
        self.reason = "Последовательный вход запущен"
        self.task = asyncio.create_task(self._run(), name="owned-account-login")

    async def stop(self):
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.reason = "Остановлено. Незавершённый аккаунт нужно импортировать заново."

    def clear_pending(self):
        if self.running:
            raise LoginQueueError("Сначала остановите очередь")
        with self.db() as db:
            for row in db.scalars(select(AccountLogin).where(AccountLogin.status == "queued")).all():
                row.status, row.message = "cancelled", "Удалено из очереди пользователем"
                row.login_ciphertext = row.password_ciphertext = None
            db.commit()

    def _update(self, task_id, status, message, *, wipe=False):
        with self.db() as db:
            row = db.get(AccountLogin, task_id)
            if row.status == "ready" and status != "ready":
                return  # cancelling browser cleanup must not undo a committed login
            row.status, row.message = status, message
            if wipe:
                row.login_ciphertext = row.password_ciphertext = None
            db.commit()

    async def _run(self):
        try:
            while True:
                with self.db() as db:
                    row = db.scalar(select(AccountLogin).where(AccountLogin.status == "queued").order_by(AccountLogin.created_at, AccountLogin.position).limit(1))
                    if not row:
                        self.reason = "Очередь завершена"
                        return
                    self.active_id = row.id
                    login, password = decrypt(row.login_ciphertext), decrypt(row.password_ciphertext)
                    row.status, row.message = "processing", "Открывается обычная форма Google"
                    db.commit()
                try:
                    await self._authenticate(login, password)
                finally:
                    login = password = None
                self.active_id = None
                with self.db() as db:
                    if not db.scalar(select(AccountLogin.id).where(AccountLogin.status == "queued").limit(1)):
                        self.reason = "Очередь завершена"
                        return
                self.reason = "Вход сохранён; следующая запись через 60 секунд"
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            if self.active_id:
                self._update(self.active_id, "cancelled", "Вход отменён пользователем", wipe=True)
            raise
        except Exception as exc:
            # Playwright errors often contain form input values: never log their
            # text, tracebacks, call logs, full URLs, screenshots or request bodies.
            message = str(exc) if isinstance(exc, LoginQueueError) else "Не удалось завершить вход. Проверьте сеть и обычный браузер; автоматических повторов нет."
            if self.active_id:
                self._update(self.active_id, "error", message, wipe=True)
            self.reason = message
            log.warning("Login queue paused (%s); no next account attempted", type(exc).__name__)
        finally:
            self.active_id = None
            self.waiting = False
            self.resume_event = None

    async def _authenticate(self, login, password):
        with TemporaryDirectory(prefix="clipforge-owned-login-", ignore_cleanup_errors=True) as folder:
            if settings.youtube_doh_enabled:
                configure_secure_dns_profile(Path(folder), settings.youtube_doh_template)
            options = {"headless": False, "viewport": VIEWPORT, "locale": "en-US", "accept_downloads": False}
            if settings.production_mode:
                options["chromium_sandbox"] = True
            if settings.browser_executable_path:
                options["executable_path"] = settings.browser_executable_path
            async with async_playwright() as driver:
                context = await driver.chromium.launch_persistent_context(folder, **options)
                self.context = context
                try:
                    page = await context.new_page()
                    self.page = page
                    page.set_default_timeout(10000)
                    async def guard(route):
                        request = route.request
                        if request.is_navigation_request() and request.frame == page.main_frame and not trusted_url(request.url):
                            await route.abort()
                        else:
                            await route.continue_()
                    await page.route("**/*", guard)
                    await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=45000)
                    await self._sign_in(page, context, login, password)
                finally:
                    self.page = self.context = None
                    try:
                        await asyncio.wait_for(context.close(), 10)
                    except Exception:
                        log.warning("Login browser cleanup did not finish normally")

    async def _sign_in(self, page, context, login, password):
        email_sent = password_sent = False
        deadline = time.monotonic() + 25
        while True:
            if not trusted_url(page.url):
                raise LoginQueueError("Неожиданный адрес страницы; вход остановлен")
            try:
                channel_id = await authenticated_channel(page)
                body = (await page.locator("body").inner_text(timeout=5000)).lower()
            except PlaywrightError:
                # Normal sign-in redirects may destroy an execution context.
                # Only wait briefly; never resubmit credentials to recover it.
                if page.is_closed():
                    raise LoginQueueError("Окно входа закрыто; очередь остановлена") from None
                if time.monotonic() > deadline:
                    await self._manual("Страница не завершила переход. Проверьте окно браузера вручную.")
                    deadline = time.monotonic() + 25
                await asyncio.sleep(0.5)
                continue
            if channel_id:
                self._save_session(channel_id, await context.storage_state())
                return
            if any(word in body for word in ("browser or app may not be secure", "couldn’t sign you in", "couldn't sign you in", "wrong password", "couldn’t find your google account", "couldn't find your google account", "too many failed attempts", "unusual traffic")):
                raise LoginQueueError("Google отклонил вход или запросил ограничение. Очередь остановлена, следующий аккаунт не запускается.")
            challenge = page.locator('iframe[src*="recaptcha"],iframe[src*="hcaptcha"]')
            challenge_visible = any([await frame.is_visible() for frame in await challenge.all()])
            email = page.locator('input[type="email"]:visible')
            password_field = page.locator('input[type="password"]:visible')
            # Google may label its ordinary password page "Verify it's you".
            # Only an actual, expected credential form may be filled automatically;
            # challenge frames and stronger verification prompts always win.
            ordinary_form = (urlsplit(page.url).hostname == "accounts.google.com" and
                ((not email_sent and await email.count() and await page.locator("#identifierNext").count()) or
                 (not password_sent and await password_field.count() and await page.locator("#passwordNext").count())))
            security = (challenge_visible or any(word in body for word in ("2-step verification", "verify you are human", "confirm you’re not a robot", "confirm you're not a robot")) or
                (not ordinary_form and any(word in body for word in ("verify it’s you", "verify it's you", "подтвердите, что это вы"))))
            if security:
                await self._manual("Google запросил подтверждение. Пройдите его вручную в окне ниже и нажмите «Проверить и продолжить».")
                deadline = time.monotonic() + 25
                continue
            if ordinary_form and not email_sent and await email.count():
                await email.first.fill(login)
                email_sent = True  # never resubmit the same credential automatically
                await page.locator("#identifierNext").click()
                deadline = time.monotonic() + 25
            elif ordinary_form and not password_sent and await password_field.count():
                await password_field.first.fill(password)
                password_sent = True
                await page.locator("#passwordNext").click()
                deadline = time.monotonic() + 25
            elif time.monotonic() > deadline:
                await self._manual("Завершите вход, выбор канала или подтверждение вручную. Автоматически принимается только обычная форма логина и пароля.")
                # The operator may have completed sign-in on youtube.com rather
                # than Studio. One ordinary navigation checks the chosen channel.
                if urlsplit(page.url).hostname == "www.youtube.com":
                    await page.goto("https://studio.youtube.com/", wait_until="domcontentloaded")
                deadline = time.monotonic() + 25
            await asyncio.sleep(0.5)

    async def _manual(self, message):
        self.resume_event = asyncio.Event()
        self.waiting, self.reason = True, message
        self._update(self.active_id, "awaiting_verification", message)
        try:
            await asyncio.wait_for(self.resume_event.wait(), 900)
        except asyncio.TimeoutError:
            raise LoginQueueError("Подтверждение не выполнено за 15 минут; очередь остановлена") from None
        finally:
            self.waiting = False
        self._update(self.active_id, "processing", "Проверяется завершение входа")

    def _save_session(self, channel_id, state):
        state = filtered_storage(state)
        with self.db() as db:
            row = db.get(AccountLogin, self.active_id)
            external_id = f"youtube-channel:{channel_id}"
            account = db.scalar(select(Account).where(Account.external_id == external_id))
            if not account:
                account = Account(platform="youtube", external_id=external_id, display_name=row.display_name,
                                  access_token="", refresh_token="")
                db.add(account)
                db.flush()
            account.access_token = encrypt(json.dumps(state))
            account.refresh_token = encrypt("")
            account.display_name, account.status = row.display_name, RecordStatus.ready
            row.account_id, row.status, row.message = account.id, "ready", "Вход в Studio подтверждён, сессия сохранена"
            row.login_ciphertext = row.password_ciphertext = None
            db.commit()

    def _manual_page(self, task_id):
        if not self.waiting or task_id != self.active_id or not self.page or self.page.is_closed():
            raise LoginQueueError("Нет активной ручной проверки для этой записи")
        if not trusted_url(self.page.url):
            raise LoginQueueError("Управление доступно только на странице Google/YouTube")
        return self.page

    async def screenshot(self, task_id):
        async with self.controls:
            page = self._manual_page(task_id)
            return await page.screenshot(type="jpeg", quality=65, timeout=5000)

    async def action(self, task_id, payload):
        async with self.controls:
            page = self._manual_page(task_id)
            kind = payload.get("kind")
            if kind == "resume":
                self.resume_event.set()
            elif kind == "click":
                x, y = payload.get("x"), payload.get("y")
                if type(x) not in (int, float) or type(y) not in (int, float) or not 0 <= x < VIEWPORT["width"] or not 0 <= y < VIEWPORT["height"]:
                    raise LoginQueueError("Клик вне окна браузера")
                await page.mouse.click(x, y)
            elif kind == "text":
                value = payload.get("text")
                if not isinstance(value, str) or not 1 <= len(value) <= 500 or any(ord(c) < 32 for c in value):
                    raise LoginQueueError("Недопустимый текст")
                await page.keyboard.insert_text(value)
            elif kind == "key" and payload.get("key") in {"Tab", "Shift+Tab", "Enter", "Backspace", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Space", "Control+A"}:
                await page.keyboard.press(payload["key"])
            elif kind == "scroll" and type(payload.get("dy")) is int and abs(payload["dy"]) <= 600:
                await page.mouse.wheel(0, payload["dy"])
            else:
                raise LoginQueueError("Недопустимое действие")


login_queue = LoginQueue()
