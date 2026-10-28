"""Real media checks: timeline, dimensions, looped replacement audio and pitch."""
import array,math,os,subprocess,tempfile,unittest
from pathlib import Path
from app.services.media import render_vertical_clip,prepare_music,probe_duration,probe_video_stream
from app.config import settings


@unittest.skipUnless(os.environ.get('RUN_FFMPEG_TESTS')=='1','Opt-in real FFmpeg fixtures')
class EditingMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source.mp4';self.music=self.root/'track.m4a'
        filters=';'.join(f'color=c={color}:s=320x180:r=30:d=2[v{i}]' for i,color in enumerate(['red','green','blue','yellow']))
        filters+=';[v0][v1][v2][v3]concat=n=4:v=1:a=0[v]'
        self.ffmpeg('-f','lavfi','-i','sine=frequency=440:duration=8','-filter_complex',filters,
            '-map','[v]','-map','0:a','-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac','-t','8',str(self.source))
        wav=self.root/'track.wav'
        self.ffmpeg('-f','lavfi','-i','sine=frequency=880:duration=0.6',str(wav))
        prepare_music(wav,self.music)

    def tearDown(self):self.temp.cleanup()

    def ffmpeg(self,*args):
        result=subprocess.run([settings.ffmpeg_executable,'-hide_banner','-loglevel','error','-y',*args],capture_output=True,timeout=120)
        self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        return result.stdout

    def tone(self,path,offset):
        raw=self.ffmpeg('-ss',str(offset),'-i',str(path),'-t','0.4','-vn','-ac','1','-ar','8000','-f','f32le','pipe:1')
        samples=array.array('f');samples.frombytes(raw)
        def energy(freq):
            return abs(sum(v*complex(math.cos(2*math.pi*freq*i/8000),math.sin(2*math.pi*freq*i/8000)) for i,v in enumerate(samples)))
        return energy(440),energy(880)

    def test_trim_speed_resolution_and_music_replacement_through_end(self):
        output=self.root/'result.mp4'
        render_vertical_clip(self.source,output,2,2,speed=2,quality='480p',music=self.music)
        self.assertAlmostEqual(probe_duration(output),2,delta=.15)
        self.assertEqual(probe_video_stream(output),(480,854,True))
        for offset,channel in [(.25,1),(1.25,2)]:
            rgb=self.ffmpeg('-ss',str(offset),'-i',str(output),'-vf','format=rgb24,crop=1:1:240:427','-frames:v','1','-f','rawvideo','pipe:1')
            self.assertEqual(len(rgb),3)
            self.assertGreater(rgb[channel],max(rgb[i] for i in range(3) if i!=channel)+40)
        for offset in (.1,1.5):
            original,replacement=self.tone(output,offset)
            self.assertGreater(replacement,10*original)

    def test_original_audio_keeps_pitch_when_speed_changes_without_music(self):
        output=self.root/'original-audio.mp4'
        render_vertical_clip(self.source,output,0,2,speed=1.5,quality='720p')
        self.assertEqual(probe_video_stream(output),(720,1280,True))
        original,replacement=self.tone(output,.2)
        self.assertGreater(original,10*replacement)
        self.assertAlmostEqual(probe_duration(output),2,delta=.15)

    def test_automatic_plan_renders_ten_to_fifteen_seconds_with_replacement_audio(self):
        import random
        from unittest.mock import patch
        from app.services.editing import resolve_automatic
        longer=self.root/'longer.mp4'
        self.ffmpeg('-stream_loop','2','-i',str(self.source),'-t','24','-c','copy',str(longer))
        class Choices(random.Random):
            def choice(self,values):
                if values and isinstance(values[0],str) and values[0].endswith('p'):return values[-1]
                return values[0]
        with patch.object(settings,'media_root',self.root):
            settings.music_dir.mkdir()
            track_id='a'*32
            selected=settings.music_dir/(track_id+'.m4a');selected.write_bytes(self.music.read_bytes())
            edit=resolve_automatic(probe_duration(longer),320,180,[track_id],rng=Choices(9))
            output=self.root/'auto.mp4'
            render_vertical_clip(longer,output,edit['start_seconds'],edit['duration_seconds'],
                speed=edit['speed'],quality=edit['quality'],music=selected)
        self.assertTrue(10<=probe_duration(output)<=15)
        self.assertNotEqual(edit['speed'],1)
        self.assertEqual(probe_video_stream(output),(480,854,True))
        original,replacement=self.tone(output,probe_duration(output)-.6)
        self.assertGreater(replacement,10*original)
