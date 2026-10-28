import tempfile
import asyncio
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.services import downloaders, media
from app.config import settings
from app.services.y2save import DownloadBlocked, Y2SaveError


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.target = Path(self.directory.name) / "source.mp4"

    def tearDown(self):
        self.directory.cleanup()

    def provider(self, failure=None):
        provider = Mock()
        if failure:
            provider.download.side_effect = failure
        else:
            provider.download.side_effect = lambda url, path: path.write_bytes(b"fake-media")
        return provider

    def test_fallback_after_provider_failure_and_atomic_result(self):
        first, second = self.provider(Y2SaveError("upstream unavailable")), self.provider()
        chain = downloaders.FallbackDownloader([("first", first), ("second", second)])
        with patch.object(downloaders, "probe_duration", return_value=20), patch.object(downloaders, "probe_video_stream", return_value=(1280, 720, True)):
            self.assertEqual(chain.download("https://youtu.be/abcdefghijk", self.target), self.target)
        self.assertTrue(self.target.is_file())
        self.assertEqual(chain.last_provider, "second")
        self.assertEqual(chain.attempts[0]["status"], "error")
        first.close.assert_called_once()
        self.assertEqual(list(self.target.parent.glob(".*candidate*")), [])

    def test_no_failover_to_evade_verification(self):
        first, second = self.provider(DownloadBlocked("manual verification required")), self.provider()
        chain = downloaders.FallbackDownloader([("first", first), ("second", second)])
        with self.assertRaises(DownloadBlocked):
            chain.download("https://youtu.be/abcdefghijk", self.target)
        second.download.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_invalid_media_is_not_accepted(self):
        chain = downloaders.FallbackDownloader([("bad", self.provider())])
        with patch.object(downloaders, "probe_duration", side_effect=media.MediaError("HTML instead of MP4")), self.assertRaises(Y2SaveError):
            chain.download("https://youtu.be/abcdefghijk", self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.iterdir()), [])

    def test_cooldown_prevents_repeated_requests_to_failed_provider(self):
        provider = self.provider(Y2SaveError("unavailable"))
        chain = downloaders.FallbackDownloader([("failed", provider)])
        for _ in range(2):
            with self.assertRaises(Y2SaveError):
                chain.download("https://youtu.be/abcdefghijk", self.target)
        self.assertEqual(provider.download.call_count, 1)
        self.assertEqual(chain.attempts, [{"provider": "failed", "status": "cooldown"}])

    def test_cnv_timeout_cancels_the_whole_download(self):
        async def stalled_download(url, destination):
            await asyncio.sleep(60)
        provider = downloaders.CnvMP3Downloader()
        with patch.object(settings, "download_provider_timeout_seconds", 0.01), patch.object(provider, "_download", side_effect=stalled_download), self.assertRaises(Y2SaveError):
            provider.download("https://youtu.be/abcdefghijk", self.target)

    def test_final_failure_preserves_both_provider_reasons_without_signed_urls(self):
        chain=downloaders.FallbackDownloader([('first',self.provider(Y2SaveError('CnvMP3 сообщил об ошибке: https://secret.test/file?token=private'))),
                                            ('second',self.provider(Y2SaveError('Y2Save did not start a file download')))])
        snapshots=[]
        chain.progress_callback=lambda stage,percent:snapshots.append([dict(item) for item in chain.attempts])
        with self.assertRaises(downloaders.ProvidersUnavailable) as caught:
            chain.download('https://youtu.be/neutral-fixture',self.target)
        self.assertEqual(len(caught.exception.provider_attempts),2)
        self.assertEqual(snapshots[-1],caught.exception.provider_attempts)
        self.assertEqual(caught.exception.provider_attempts[0]['reason'],'Сервис отклонил конвертацию')
        self.assertNotIn('private',str(caught.exception.provider_attempts))


class ShortsTests(unittest.TestCase):
    def test_square_and_portrait_supported_horizontal_rejected(self):
        with patch.object(media, "probe_duration", return_value=20):
            for dimensions in ((1080, 1920, True), (1080, 1080, False)):
                with patch.object(media, "probe_video_stream", return_value=dimensions):
                    self.assertTrue(media.validate_shorts_file(Path("fixture.mp4"))["shorts_eligible"])
            with patch.object(media, "probe_video_stream", return_value=(1920, 1080, True)), self.assertRaises(media.MediaError):
                media.validate_shorts_file(Path("fixture.mp4"))

    def test_actual_duration_limit(self):
        with patch.object(media, "probe_video_stream", return_value=(1080, 1920, True)):
            with patch.object(media, "probe_duration", return_value=180):
                self.assertTrue(media.validate_shorts_file(Path("fixture.mp4"))["shorts_eligible"])
            with patch.object(media, "probe_duration", return_value=180.01), self.assertRaises(media.MediaError):
                media.validate_shorts_file(Path("fixture.mp4"))
