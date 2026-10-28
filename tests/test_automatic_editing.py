import random,unittest
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import select
from app import worker
from app.models import AutomationPlan,Clip,Job,JobType,RecordStatus,SourceVideo
from app.services.editing import EditingOptions,capture_editing,resolve_automatic,EditingSetupError
from app.services.source_identity import FirstResultError
import test_editing as baseline


class AutomaticEditingTests(unittest.TestCase):
    setUp=baseline.EditingTests.setUp
    tearDown=baseline.EditingTests.tearDown
    upload_music=baseline.EditingTests.upload_music

    def enable(self):
        result=self.client.patch('/api/editing',json={'automatic':True})
        self.assertEqual(result.status_code,200,result.text)

    def test_empty_music_library_allows_automatic_jobs_with_original_audio(self):
        self.enable()
        response=self.client.post('/api/discover',json={'query':'bread recipe'})
        self.assertEqual(response.status_code,200,response.text)
        with self.sessions() as db:
            job=db.get(Job,response.json()['job_id'])
            self.assertEqual(job.payload['editing']['music_pool'],[])
        chosen=resolve_automatic(60,1920,1080,[],rng=random.Random(1))
        self.assertIsNone(chosen['music_id'])
        self.assertNotEqual(chosen['speed'],1)
        self.assertTrue(10<=chosen['duration_seconds']<=15)

    def test_unavailable_tracks_fall_back_to_original_audio(self):
        chosen=resolve_automatic(60,1920,1080,['a'*32],rng=random.Random(2))
        self.assertIsNone(chosen['music_id'])

    def test_automatic_render_without_library_passes_original_audio_to_encoder(self):
        self.enable()
        path=self.root/'original-source.mp4';path.write_bytes(b'source')
        with self.sessions() as db:
            source=SourceVideo(source_url='https://youtu.be/original-audio-fixture',local_path=str(path),
                duration_seconds=60,status=RecordStatus.ready,rights_confirmed=True)
            db.add(source);db.flush()
            job=Job(type=JobType.render,payload={'source_id':source.id,'duration':15,'count':1,'editing':capture_editing(db)})
            db.add(job);db.commit()
        def render(source,destination,start,duration,**kwargs):
            self.assertIsNone(kwargs['music'])
            self.assertNotEqual(kwargs['speed'],1)
            destination.write_bytes(b'finished clip')
        with patch.object(worker,'probe_video_stream',return_value=(1920,1080,True)), \
             patch.object(worker,'render_vertical_clip',side_effect=render) as renderer:
            result=worker.process_render(job)
            self.assertEqual(result,worker.process_render(job))
            renderer.assert_called_once()
        self.assertIsNone(result['editing']['music_id'])
        self.assertEqual(result['editing']['quality'],'1080p')
        self.assertEqual(result['editing']['profile_version'],2)
        self.assertIn('presentation',result['editing'])

    def test_random_plans_obey_ranges_and_never_use_original_resolution(self):
        music=self.upload_music()['settings']['music_id']
        seen_tracks=set();seen_speeds=set();seen_starts=set()
        second=self.upload_music()['settings']['music_id']
        for i in range(300):
            total=10 if i%3==0 else 60
            width,height=[(1920,1080),(720,1280),(854,480)][i%3]
            edit=resolve_automatic(total,width,height,[music,second],rng=random.Random(i))
            self.assertGreaterEqual(edit['speed'],.98);self.assertLessEqual(edit['speed'],1.03)
            self.assertNotEqual(edit['speed'],1)
            self.assertTrue(10-1e-8<=edit['duration_seconds']<=15+1e-8)
            excerpt=edit['duration_seconds']*edit['speed']
            self.assertTrue(10-1e-8<=excerpt<=15+1e-8)
            self.assertGreaterEqual(edit['start_seconds'],0)
            self.assertLessEqual(edit['start_seconds']+excerpt,total+1e-8)
            self.assertNotEqual(int(edit['quality'][:-1]),min(width,height))
            seen_tracks.add(edit['music_id']);seen_speeds.add(edit['speed']);seen_starts.add(edit['start_seconds'])
        self.assertEqual(seen_tracks,{music,second});self.assertGreater(len(seen_speeds),20);self.assertGreater(len(seen_starts),50)
        with self.assertRaises(ValueError):resolve_automatic(9,1920,1080,[music])

    def test_snapshot_captures_library_for_each_job(self):
        first=self.upload_music()['settings']['music_id'];self.enable()
        response=self.client.post('/api/discover',json={'query':'bread recipe'})
        self.assertEqual(response.status_code,200,response.text)
        self.upload_music()
        with self.sessions() as db:
            job=db.get(Job,response.json()['job_id'])
            self.assertTrue(job.payload['editing']['automatic'])
            self.assertEqual(job.payload['editing']['music_pool'],[first])
            self.assertEqual(len(capture_editing(db)['music_pool']),2)

    def test_failed_render_retry_does_not_randomize_again_or_create_second_clip(self):
        self.upload_music();self.enable()
        path=self.root/'automatic-source.mp4';path.write_bytes(b'source')
        with self.sessions() as db:
            source=SourceVideo(source_url='https://youtu.be/automatic-fixture',local_path=str(path),duration_seconds=60,
                status=RecordStatus.ready,rights_confirmed=True)
            db.add(source);db.flush()
            job=Job(type=JobType.render,payload={'source_id':source.id,'duration':5,'count':1,'editing':capture_editing(db)})
            db.add(job);db.commit()
        calls=[]
        def render(source,destination,start,duration,**kwargs):
            calls.append((start,duration,kwargs))
            if len(calls)==1:raise RuntimeError('Interrupted encoder')
            destination.write_bytes(b'finished clip')
        with patch.object(worker,'probe_video_stream',return_value=(720,1280,True)) as probe, \
             patch.object(worker,'resolve_presentation',wraps=worker.resolve_presentation) as present, \
             patch.object(worker,'resolve_automatic',wraps=resolve_automatic) as choose, \
             patch.object(worker,'render_vertical_clip',side_effect=render):
            with self.assertRaisesRegex(RuntimeError,'Interrupted'):worker.process_render(job)
            result=worker.process_render(job)
            probe.assert_called_once();choose.assert_called_once()
            present.assert_called_once()
        self.assertEqual(calls[0],calls[1]);self.assertNotEqual(calls[0][2]['speed'],1)
        self.assertNotEqual(calls[0][2]['quality'],'720p')
        self.assertIsNotNone(calls[0][2]['music'])
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(Clip)).all()),1)
            saved=db.get(Job,job.id).payload['resolved_editing']
            self.assertEqual(result['editing'],saved)

    def test_direct_upload_automatic_snapshot_is_supported(self):
        from app.services.editing import direct_duration
        self.upload_music();self.enable()
        with self.sessions() as db:snapshot=capture_editing(db)
        self.assertEqual(direct_duration(30,snapshot),15)
