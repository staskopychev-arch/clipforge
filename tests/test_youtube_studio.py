"""Regression tests for Studio readiness; no accounts or external uploads."""
import unittest
from unittest.mock import MagicMock, patch

from app.config import settings
from app.services.youtube_upload import (
    YouTubeSessionError, YouTubeUploadError, dismiss_studio_welcome,
    open_studio, open_upload_form, studio_ready, canonical_video_url, _video_url, resume_draft, _confirm_saved,
)


class StudioTests(unittest.TestCase):
    def page(self):
        page = MagicMock()
        page.url = "https://studio.youtube.com/channel/UC_test_channel"
        page.locator.return_value.count.return_value = 1
        page.locator.return_value.is_visible.return_value = False
        page.locator.return_value.inner_text.return_value = "Channel dashboard"
        page.get_by_role.return_value.count.return_value = 1
        page.get_by_role.return_value.first.is_visible.return_value = True
        page.get_by_text.return_value.count.return_value = 0
        return page

    def test_zero_size_app_host_does_not_reject_visible_toolbar(self):
        page = self.page()
        self.assertTrue(studio_ready(page))
        page.locator.return_value.is_visible.assert_not_called()

    def test_readiness_requires_channel_and_real_toolbar(self):
        page = self.page()
        for url in ("https://accounts.google.com/signin", "https://studio.youtube.com/", "https://example.com/channel/UC_test"):
            page.url = url
            self.assertFalse(studio_ready(page))
        page.url = "https://studio.youtube.com/channel/UC_test"
        page.get_by_role.return_value.first.is_visible.return_value = False
        self.assertFalse(studio_ready(page))

    def test_welcome_only_clicks_expected_continue(self):
        page = self.page()
        welcome = page.get_by_text.return_value
        welcome.count.return_value = 1
        welcome.first.is_visible.return_value = True
        with patch("app.services.youtube_upload.check_challenge"):
            dismiss_studio_welcome(page)
        page.get_by_role.assert_called_once_with("button", name="Continue", exact=True)
        page.get_by_role.return_value.click.assert_called_once()
        welcome.first.wait_for.assert_called_once_with(state="hidden", timeout=15_000)

    def test_security_challenge_never_clicks_continue(self):
        page = self.page()
        with patch("app.services.youtube_upload.check_challenge", side_effect=YouTubeSessionError("manual verification")):
            with self.assertRaises(YouTubeSessionError):
                dismiss_studio_welcome(page)
        page.get_by_role.return_value.click.assert_not_called()

    def test_unsupported_browser_is_not_bad_session(self):
        page = self.page()
        page.locator.return_value.inner_text.return_value = "You are using an unsupported browser"
        with patch("app.services.youtube_upload.check_challenge"):
            with self.assertRaises(YouTubeUploadError) as result:
                open_studio(page)
        self.assertNotIsInstance(result.exception, YouTubeSessionError)

    def test_timeout_is_not_bad_session(self):
        with patch.object(settings, "youtube_studio_timeout_seconds", 0):
            with self.assertRaises(YouTubeUploadError) as result:
                open_studio(self.page())
        self.assertNotIsInstance(result.exception, YouTubeSessionError)

    def test_hidden_file_input_is_accepted_without_uploading(self):
        page = self.page()
        with patch("app.services.youtube_upload.check_challenge"), patch("app.services.youtube_upload.dismiss_studio_welcome") as dismiss:
            open_upload_form(page)
        dismiss.assert_not_called()
        page.locator.return_value.first.set_input_files.assert_not_called()
        page.locator.return_value.is_visible.assert_not_called()

    def test_modern_shorts_receipt(self):
        page = self.page()
        page.locator.return_value.evaluate_all.return_value = ["https://youtube.com/shorts/esIYpNeSCtI"]
        self.assertEqual(_video_url(page), "https://www.youtube.com/watch?v=esIYpNeSCtI")
        self.assertIn("ytcp-uploads-dialog", page.locator.call_args.args[0])

    def test_receipts_reject_other_hosts_and_invalid_ids(self):
        for url in ("https://evil.test/shorts/esIYpNeSCtI", "https://youtube.com/shorts/abc"):
            self.assertIsNone(canonical_video_url(url))
        self.assertEqual(canonical_video_url("https://youtu.be/esIYpNeSCtI"), "https://www.youtube.com/watch?v=esIYpNeSCtI")

    def test_resume_draft_does_not_upload_file(self):
        page = self.page()
        page.locator.return_value.filter.return_value.count.return_value = 1
        page.locator.return_value.filter.return_value.locator.return_value.inner_text.return_value = "Test • Draft"
        with patch("app.services.youtube_upload.youtube_page") as context, \
             patch("app.services.youtube_upload.open_studio"), \
             patch("app.services.youtube_upload.dismiss_studio_welcome"), \
             patch("app.services.youtube_upload._finish_upload", return_value={"confirmed": True}):
            context.return_value.__enter__.return_value = page
            resume_draft(encrypted_session="fixture", video_url="https://youtu.be/esIYpNeSCtI",
                         title="Test", description="", visibility="private", made_for_kids=False, on_progress=MagicMock())
        for call in page.mock_calls:
            self.assertNotIn("set_input_files", str(call))

    def test_saved_video_requires_matching_visibility_without_save_click(self):
        page = self.page()
        link = page.locator.return_value.filter.return_value
        link.count.return_value = 1
        page.locator.return_value.inner_text.return_value = "Private"
        options = dict(content_url="https://studio.youtube.com/channel/UC_test/content",
                       url="https://www.youtube.com/watch?v=esIYpNeSCtI", title="Test", visibility="private")
        with patch("app.services.youtube_upload.check_challenge"), patch("app.services.youtube_upload.dismiss_studio_welcome"):
            _confirm_saved(page, **options)
            page.locator.return_value.inner_text.return_value = "Public"
            with self.assertRaises(YouTubeUploadError):
                _confirm_saved(page, **options)
        page.locator.return_value.click.assert_not_called()
