import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.config import settings
from app.services.browser_network import isolated_youtube_browser, secure_dns_preferences, configure_secure_dns_profile


class BrowserNetworkTests(unittest.TestCase):
    def test_dns_update_preserves_other_profile_settings(self):
        with TemporaryDirectory() as directory:
            profile = Path(directory)
            state = profile / "Local State"
            state.write_text(json.dumps({"custom":{"retained":True}, "dns_over_https":{"mode":"off"}}))
            configure_secure_dns_profile(profile, "https://dns.google/dns-query")
            actual = json.loads(state.read_text())
            self.assertEqual(actual["custom"], {"retained":True})
            self.assertEqual(actual["dns_over_https"]["mode"], "secure")
            self.assertEqual(list(profile.glob(".clipforge-dns-*")), [])

    def test_invalid_local_state_is_not_replaced(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "Local State"
            state.write_text("invalid-json")
            with self.assertRaises(ValueError):
                configure_secure_dns_profile(Path(directory), "https://dns.google/dns-query")
            self.assertEqual(state.read_text(), "invalid-json")

    def test_dns_update_rejects_insecure_url_without_modification(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "Local State"
            state.write_text('{"retained":true}')
            with self.assertRaises(ValueError):
                configure_secure_dns_profile(Path(directory), "http://dns.google/dns-query")
            self.assertEqual(state.read_text(), '{"retained":true}')

    def test_secure_preferences_without_insecure_fallback(self):
        prefs = secure_dns_preferences("https://dns.google/dns-query")
        self.assertEqual(prefs["dns_over_https"]["mode"], "secure")
        self.assertTrue(prefs["async_dns"]["enabled"])

    def test_invalid_dns_urls_rejected(self):
        for template in ("http://dns.google/resolve", "https://user:secret@dns.google/", "file:///tmp/dns", "https://dns.google/ fragment"):
            with self.subTest(template=template), self.assertRaises(ValueError):
                secure_dns_preferences(template)

    def test_temporary_profile_is_private_and_cleaned_up(self):
        driver = MagicMock()
        bootstrap = driver.chromium.launch_persistent_context.return_value
        with patch.object(settings, "youtube_doh_enabled", True), patch.object(settings, "youtube_doh_template", "https://dns.google/dns-query"):
            with isolated_youtube_browser(driver, headless=True) as browser:
                directory = Path(driver.chromium.launch_persistent_context.call_args.args[0])
                prefs = json.loads((directory / "Local State").read_text(encoding="utf-8"))
                self.assertEqual(prefs["dns_over_https"]["templates"], "https://dns.google/dns-query")
                self.assertIs(browser, bootstrap.browser)
                self.assertNotIn("cookies", prefs)
        bootstrap.close.assert_called_once()
        self.assertFalse(directory.exists())

    def test_browser_closes_on_error(self):
        driver = MagicMock()
        bootstrap = driver.chromium.launch_persistent_context.return_value
        with patch.object(settings, "youtube_doh_enabled", True):
            with self.assertRaises(RuntimeError):
                with isolated_youtube_browser(driver, headless=True):
                    raise RuntimeError("test navigation failed")
        bootstrap.close.assert_called_once()

    def test_disabled_setting_keeps_standard_launch(self):
        driver = MagicMock()
        with patch.object(settings, "youtube_doh_enabled", False):
            with isolated_youtube_browser(driver, headless=True) as browser:
                self.assertIs(browser, driver.chromium.launch.return_value)
        driver.chromium.launch_persistent_context.assert_not_called()
        driver.chromium.launch.return_value.close.assert_called_once()
