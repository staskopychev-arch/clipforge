import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
    expect,
)

from ..config import settings
from ..models import TestEnvironment


log = logging.getLogger("clipforge.automation")


class AutomationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScenarioResult:
    url: str
    title: str
    selector: str
    visible: bool


def normalize_target_origin(value: str) -> str:
    candidate = value.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AutomationError("target_domain must be an HTTP(S) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AutomationError("target_domain must not contain credentials, a query, or a fragment")
    if parsed.path not in {"", "/"}:
        raise AutomationError("target_domain must be an origin without a path")
    host = parsed.hostname.lower().rstrip(".")
    allowed = settings.allowed_test_domains
    if not any(host == item or host.endswith(f".{item}") for item in allowed):
        raise AutomationError(f"Target host '{host}' is not present in QA_ALLOWED_DOMAINS")
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


def validate_local_cdp_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        raise AutomationError("CDP URL must use http(s) or ws(s)")
    if (parsed.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise AutomationError("CDP is restricted to the local loopback interface")
    if parsed.port is None:
        raise AutomationError("CDP URL must include an explicit local port")
    return value.strip()


def normalize_proxy_config(value: dict | None) -> dict[str, str] | None:
    if value is None:
        return None
    server = str(value.get("server", "")).strip()
    if not server:
        host = str(value.get("host", "")).strip()
        port = value.get("port")
        scheme = str(value.get("scheme", "http")).lower()
        if not host or not port:
            raise AutomationError("Proxy configuration requires server or host + port")
        server = f"{scheme}://{host}:{int(port)}"
    parsed = urlparse(server if "://" in server else f"http://{server}")
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or parsed.port is None:
        raise AutomationError("Proxy server must be a valid http(s) or socks5 URL with a port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise AutomationError("Proxy credentials must use separate username/password fields")
    host_text = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    result = {"server": f"{parsed.scheme}://{host_text}:{parsed.port}"}
    username = value.get("username")
    password = value.get("password")
    if bool(username) != bool(password):
        raise AutomationError("Proxy username and password must be provided together")
    if username:
        result["username"] = str(username)
        result["password"] = str(password)
    if value.get("bypass"):
        result["bypass"] = str(value["bypass"])
    return result


def validate_storage_state(value: dict, target_host: str) -> dict:
    cookies = value.get("cookies", [])
    origins = value.get("origins", [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise AutomationError("storage_state must contain cookies and origins arrays")
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise AutomationError("Every storage_state cookie must be an object")
        cookie_domain = str(cookie.get("domain", "")).lstrip(".").lower().rstrip(".")
        if not cookie_domain or not (target_host == cookie_domain or target_host.endswith(f".{cookie_domain}")):
            raise AutomationError("storage_state contains a cookie outside target_domain")
    for origin in origins:
        if not isinstance(origin, dict) or not isinstance(origin.get("origin"), str):
            raise AutomationError("Every storage_state origin must be a valid object")
        origin_host = (urlparse(origin["origin"]).hostname or "").lower().rstrip(".")
        if origin_host != target_host:
            raise AutomationError("storage_state contains local storage outside target_domain")
    return {"cookies": cookies, "origins": origins}


class BrowserAutomationSession:
    """One isolated Playwright context for an allowlisted E2E environment."""

    def __init__(self, environment: TestEnvironment) -> None:
        if not environment.enabled:
            raise AutomationError("Test environment is disabled")
        self.environment = environment
        self.origin = normalize_target_origin(environment.target_domain)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._owns_browser = False

    def _context_options(self) -> dict[str, Any]:
        host = urlparse(self.origin).hostname or ""
        storage_state = validate_storage_state(self.environment.get_storage_state(), host)
        options: dict[str, Any] = {
            "storage_state": storage_state,
            "accept_downloads": False,
            "ignore_https_errors": False,
        }
        proxy = normalize_proxy_config(self.environment.get_proxy_config())
        if proxy:
            options["proxy"] = proxy
        return options

    async def start(self) -> BrowserContext:
        if self._context:
            return self._context
        log.info("Starting isolated browser context for environment=%s", self.environment.name)
        self._playwright = await async_playwright().start()
        try:
            launch_options: dict[str, Any] = {"headless": settings.qa_browser_headless}
            if settings.production_mode:
                launch_options["chromium_sandbox"] = True
            if settings.browser_executable_path:
                launch_options["executable_path"] = settings.browser_executable_path
            self._browser = await self._playwright.chromium.launch(**launch_options)
            self._owns_browser = True
            self._context = await self._browser.new_context(**self._context_options())
            self._context.set_default_timeout(settings.qa_navigation_timeout_ms)
            self._context.set_default_navigation_timeout(settings.qa_navigation_timeout_ms)
            return self._context
        except Exception as exc:
            await self.close()
            raise AutomationError(f"Could not start isolated browser context: {exc}") from exc

    async def connect_local_cdp(self, cdp_url: str) -> BrowserContext:
        if self._context:
            raise AutomationError("A browser context is already active")
        endpoint = validate_local_cdp_url(cdp_url)
        log.info("Connecting to local CDP endpoint for environment=%s", self.environment.name)
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(endpoint)
            self._owns_browser = False
            # A new context keeps this E2E run isolated from the debugger's default context.
            self._context = await self._browser.new_context(**self._context_options())
            self._context.set_default_timeout(settings.qa_navigation_timeout_ms)
            self._context.set_default_navigation_timeout(settings.qa_navigation_timeout_ms)
            return self._context
        except Exception as exc:
            await self.close()
            raise AutomationError(f"Could not connect to local CDP endpoint: {exc}") from exc

    async def run_smoke_scenario(self, key_selector: str, path: str = "/") -> ScenarioResult:
        if not key_selector.strip():
            raise AutomationError("A non-empty key selector is required")
        if not path.startswith("/") or path.startswith("//"):
            raise AutomationError("Scenario path must be relative to target_domain")
        context = self._context or await self.start()
        page: Page = await context.new_page()
        target_url = f"{self.origin}{path}"
        log.info("E2E navigation started environment=%s url=%s", self.environment.name, target_url)
        try:
            response = await page.goto(target_url, wait_until="domcontentloaded")
            if response and response.status >= 400:
                raise AutomationError(f"Target returned HTTP {response.status}")
            locator = page.locator(key_selector).first
            await expect(locator).to_be_visible(timeout=settings.qa_navigation_timeout_ms)
            title = await page.title()
            log.info("E2E smoke scenario passed environment=%s selector=%s", self.environment.name, key_selector)
            return ScenarioResult(url=page.url, title=title, selector=key_selector, visible=True)
        except AutomationError:
            raise
        except Exception as exc:
            log.exception("E2E smoke scenario failed environment=%s", self.environment.name)
            raise AutomationError(f"E2E smoke scenario failed: {exc}") from exc
        finally:
            await page.close()

    async def close(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception:
                log.exception("Could not close browser context")
        if self._browser and self._owns_browser:
            try:
                await self._browser.close()
            except Exception:
                log.exception("Could not close browser")
        if self._playwright:
            await self._playwright.stop()
        self._context = None
        self._browser = None
        self._playwright = None
        self._owns_browser = False

    async def __aenter__(self) -> "BrowserAutomationSession":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        await self.close()
