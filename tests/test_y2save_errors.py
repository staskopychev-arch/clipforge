"""Site conversion errors must fail promptly, not wait for the whole deadline."""
import unittest
from unittest.mock import MagicMock, patch

from app.services.y2save import Y2SaveDownloader, Y2SaveError


class DownloaderErrorTests(unittest.TestCase):
    def test_explicit_download_failure_is_terminal(self):
        for message in ("Error: Download Failed. Please Try Again. 44%", "Failed to download", "Download has failed"):
            with self.subTest(message=message):
                page = MagicMock()
                page.locator.return_value.inner_text.return_value = message
                downloader = Y2SaveDownloader()
                with patch.object(downloader, "_raise_if_challenged"), patch.object(downloader, "_download_control") as control:
                    with self.assertRaisesRegex(Y2SaveError, "отклонил конвертацию"):
                        downloader._wait_download_control(page, 600000)
                    control.assert_not_called()
                    page.wait_for_timeout.assert_not_called()


if __name__ == "__main__":
    unittest.main()
