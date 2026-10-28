"""Actual encoder + OCR check with asymmetric frames, audio and input metadata."""
import json,os,subprocess,tempfile,unittest
from pathlib import Path
from app.config import settings
from app.services.auto_montage import detect_excerpt_text,resolve_presentation
from app.services.editing import resolve_automatic
from app.services.media import render_vertical_clip,probe_video_stream,probe_duration


@unittest.skipUnless(os.environ.get('RUN_FFMPEG_TESTS')=='1','Opt-in real FFmpeg/OCR fixture')
class MontageMediaTests(unittest.TestCase):
    def test_full_automatic_render_mirrors_clear_frames_and_removes_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.mp4';out=root/'out.mp4'
            result=subprocess.run([settings.ffmpeg_executable,'-hide_banner','-loglevel','error','-y',
                '-f','lavfi','-i','color=red:s=640x360:r=30:d=24,drawbox=x=320:y=0:w=320:h=360:color=blue:t=fill',
                '-f','lavfi','-i','sine=frequency=440:duration=24','-c:v','libx264','-preset','ultrafast',
                '-c:a','aac','-metadata','title=SECRET INPUT TITLE','-metadata','location=+45.0+090.0/',str(source)],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            edit=resolve_automatic(24,640,360,[],profile_version=2)
            presentation=resolve_presentation(source,edit,"Природа %{literal}: [ocean]")
            self.assertEqual(presentation['text_check'],'clear')
            self.assertTrue(presentation['mirror'])
            render_vertical_clip(source,out,edit['start_seconds'],edit['duration_seconds'],
                speed=edit['speed'],quality=edit['quality'],presentation=presentation)
            self.assertEqual(probe_video_stream(out),(1080,1920,True))
            self.assertAlmostEqual(probe_duration(out),edit['duration_seconds'],delta=.15)
            result=subprocess.run([settings.ffprobe_executable,'-v','error','-show_entries','format_tags:stream_tags','-of','json',str(out)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertNotIn('SECRET INPUT TITLE',result.stdout)
            self.assertNotIn('+45.0',result.stdout)
            result=subprocess.run([settings.ffmpeg_executable,'-hide_banner','-loglevel','error','-ss','4','-i',str(out),
                '-vf','format=rgb24,crop=1:1:200:960','-frames:v','1','-f','rawvideo','pipe:1'],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertGreater(result.stdout[2],result.stdout[0]+70) # blue moved from right to left
            self.assertFalse(list(root.glob('.*.heading.txt')))

    def test_actual_ocr_finds_english_and_cyrillic_and_prevents_flip(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'text.mp4'
            result=subprocess.run([settings.ffmpeg_executable,'-hide_banner','-loglevel','error','-y',
                '-f','lavfi','-i',"color=black:s=960x540:r=10:d=1,drawtext=font=DejaVuSans:text='GAME 123 ИГРА':fontcolor=white:fontsize=70:x=100:y=200",
                '-c:v','libx264',str(source)],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertEqual(detect_excerpt_text(source,0,1),'text')
