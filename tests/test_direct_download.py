import json,os,subprocess,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from app.services import direct_download
from app.services.y2save import DownloadBlocked,Y2SaveError
from app.services.youtube import search_creative_commons
from app.services.media import probe_duration,probe_video_stream


class DirectDownloadTests(unittest.TestCase):
    def test_public_file_is_validated_and_installed_atomically(self):
        calls=[]
        def start(command,**options):
            calls.append(command)
            (Path(command[-1])/'video.mp4').write_bytes(b'valid fixture')
            process=Mock(returncode=0);process.communicate.return_value=(json.dumps({'status':'ready'}),'')
            return process
        with tempfile.TemporaryDirectory() as temp,patch.object(direct_download.subprocess,'Popen',side_effect=start), \
             patch.object(direct_download,'probe_duration',return_value=20), \
             patch.object(direct_download,'probe_video_stream',return_value=(720,1280,True)):
            target=Path(temp)/'source.mp4'
            self.assertEqual(direct_download.DirectDownloader().download('https://youtu.be/neutral-fixture',target),target)
            self.assertEqual(target.read_bytes(),b'valid fixture')
            self.assertEqual([p.name for p in target.parent.iterdir()],['source.mp4'])
            self.assertNotIn('cookie',' '.join(calls[0]).lower())

    def test_login_or_access_requirement_stops_download(self):
        process=Mock(returncode=0);process.communicate.return_value=(json.dumps({'status':'access_required'}),'')
        with tempfile.TemporaryDirectory() as temp,patch.object(direct_download.subprocess,'Popen',return_value=process):
            with self.assertRaises(DownloadBlocked):
                direct_download.DirectDownloader().download('https://youtu.be/neutral-fixture',Path(temp)/'out.mp4')
            self.assertEqual(list(Path(temp).iterdir()),[])

    def test_timeout_terminates_process_and_cleans_partial_file(self):
        process=Mock(returncode=1,pid=123456)
        process.communicate.side_effect=[subprocess.TimeoutExpired('direct',120),('','')]
        with tempfile.TemporaryDirectory() as temp,patch.object(direct_download.subprocess,'Popen',return_value=process):
            if os.name=='posix':
                with patch.object(direct_download.os,'killpg') as kill:
                    with self.assertRaises(Y2SaveError):direct_download.DirectDownloader().download('https://youtu.be/neutral-fixture',Path(temp)/'out.mp4')
                    kill.assert_called_once()
            else:
                with self.assertRaises(Y2SaveError):direct_download.DirectDownloader().download('https://youtu.be/neutral-fixture',Path(temp)/'out.mp4')
                process.kill.assert_called_once()
            self.assertEqual(list(Path(temp).iterdir()),[])

    def test_raw_error_output_is_not_exposed(self):
        process=Mock(returncode=1);process.communicate.return_value=('bad json','https://secret.test/file?token=private')
        with tempfile.TemporaryDirectory() as temp,patch.object(direct_download.subprocess,'Popen',return_value=process):
            with self.assertRaises(Y2SaveError) as caught:
                direct_download.DirectDownloader().download('https://youtu.be/neutral-fixture',Path(temp)/'out.mp4')
            self.assertNotIn('private',str(caught.exception))


@unittest.skipUnless(os.environ.get('RUN_DIRECT_DOWNLOAD_TESTS')=='1','Opt-in neutral public-media download')
class DirectLiveDownloadTests(unittest.TestCase):
    def test_neutral_cc_video_downloads_through_production_provider(self):
        results=search_creative_commons('bread recipe short tutorial',5,'short')
        self.assertTrue(results)
        with tempfile.TemporaryDirectory() as temp:
            target=Path(temp)/'neutral.mp4'
            from app.services.downloaders import FallbackDownloader
            chain=FallbackDownloader()
            self.assertEqual(chain.providers[0][0],'direct')
            chain.download(results[0]['url'],target)
            self.assertEqual(chain.last_provider,'direct')
            self.assertGreater(target.stat().st_size,1024)
            self.assertGreater(probe_duration(target),0)
            width,height,_=probe_video_stream(target)
            self.assertGreater(width,0);self.assertGreater(height,0)
