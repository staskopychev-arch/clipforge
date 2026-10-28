"""Browser-scoped Secure DNS; never modifies Windows or another Chrome profile."""

import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory, NamedTemporaryFile
from urllib.parse import urlsplit

from playwright.sync_api import Browser, Playwright

from ..config import settings


def secure_dns_preferences(template: str) -> dict:
    """Chromium stores these browser-wide preferences in its Local State file."""
    parsed = urlsplit(template)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.fragment or any(c.isspace() for c in template)):
        raise ValueError("DoH template must be an HTTPS URL without credentials")
    return {
        "dns_over_https": {"mode": "secure", "templates": template},
        "async_dns": {"enabled": True},
    }


def configure_secure_dns_profile(profile: Path, template: str) -> None:
    """Update only DNS preferences before launch, preserving other profile state."""
    changes = secure_dns_preferences(template)
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = profile / "Local State"
    if destination.is_symlink():
        raise ValueError("Browser Local State must not be a symlink")
    preferences = json.loads(destination.read_text(encoding="utf-8")) if destination.exists() else {}
    if not isinstance(preferences, dict):
        raise ValueError("Browser Local State must contain a JSON object")
    preferences.update(changes)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=profile, prefix=".clipforge-dns-", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(preferences, stream)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def isolated_youtube_browser(driver: Playwright, *, headless: bool):
    """Use a fresh DNS-configured bootstrap profile and separate session contexts.

    Account cookies/storage state belong to an incognito context created by the
    caller, not the on-disk bootstrap profile. The profile is discarded on exit.
    TLS verification and machine-level Chrome policies remain unchanged.
    """
    options = {"headless": headless}
    if settings.production_mode:
        options["chromium_sandbox"] = True
    if settings.browser_executable_path:
        options["executable_path"] = settings.browser_executable_path
    if not settings.youtube_doh_enabled:
        browser = driver.chromium.launch(**options)
        try:
            yield browser
        finally:
            browser.close()
        return

    secure_dns_preferences(settings.youtube_doh_template)
    with TemporaryDirectory(prefix="clipforge-youtube-doh-", ignore_cleanup_errors=True) as directory:
        # This is our own new runtime profile, never the user's Chrome directory.
        profile = Path(directory)
        configure_secure_dns_profile(profile, settings.youtube_doh_template)
        bootstrap = driver.chromium.launch_persistent_context(str(profile), **options)
        try:
            browser: Browser | None = bootstrap.browser
            if browser is None:
                raise RuntimeError("Chromium did not expose its isolated browser")
            yield browser
        finally:
            bootstrap.close()
