import json,unittest
from pathlib import Path
from unittest.mock import patch
from sqlalchemy import select
from app import worker,main
from app.models import AutomationPlan,Clip,Job,JobType,RecordStatus,SourceVideo
from app.services.editing import EditingOptions,editing_snapshot,excerpt_plan,update_editing
from app.services.media import MediaError
import test_pipeline as baseline


class EditingTests(unittest.TestCase):
    setUp=baseline.PipelineTests.setUp
    tearDown=baseline.PipelineTests.tearDown

    def upload_music(self):
        def normalize(source,destination):
            self.assertEqual(source.read_bytes(),b'audio input')
            destination.write_bytes(b'normalized audio');return 9
        with patch.object(main,'prepare_music',side_effect=normalize):
            result=self.client.post('/api/editing/music',files={'file':('My track.mp3',b'audio input','audio/mpeg')})
        self.assertEqual(result.status_code,201,result.text)
        return result.json()

    def test_music_upload_selects_private_track_and_validates_actual_audio(self):
        edit=self.upload_music();identity=edit['settings']['music_id']
        self.assertEqual(len(edit['tracks']),1)
        response=self.client.get(edit['tracks'][0]['preview_url'])
        self.assertEqual(response.content,b'normalized audio')
        self.assertTrue((self.root/'music'/f'{identity}.m4a').is_file())
        self.assertEqual(len(list((self.root/'music').iterdir())),1)
        with patch.object(main,'prepare_music',side_effect=MediaError('No audio')):
            response=self.client.post('/api/editing/music',files={'file':('broken.mp3',b'not audio','audio/mpeg')})
        self.assertEqual(response.status_code,400)
        self.assertEqual(len(list((self.root/'music').iterdir())),1)
        self.assertEqual(self.client.get('/api/editing').json(),edit)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(edit['tracks'][0]['preview_url']).status_code,401)

    def test_invalid_settings_do_not_change_preferences(self):
        for invalid in [{'start_seconds':-1},{'start_seconds':8,'end_seconds':3},{'speed':2.5},
                        {'quality':'bogus'},{'music_id':'../secrets'}]:
            response=self.client.patch('/api/editing',json=invalid)
            self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.client.get('/api/editing').json()['settings'],EditingOptions().model_dump())
        self.assertEqual(self.client.patch('/api/editing',json={'music_id':'a'*32}).status_code,409)

    def test_render_uses_saved_snapshot_trim_speed_quality_and_retry_reuses_one_clip(self):
        options={'automatic':False,'start_seconds':2,'end_seconds':14,'speed':1.5,'quality':'720p','music_id':None}
        self.assertEqual(self.client.patch('/api/editing',json=options).status_code,200)
        path=self.root/'source.mp4';path.write_bytes(b'video')
        with self.sessions() as db:
            source=SourceVideo(source_url='https://youtu.be/editing-clip',rights_confirmed=True,status=RecordStatus.ready,
                local_path=str(path),duration_seconds=30)
            db.add(source);db.commit()
        response=self.client.post('/api/render',json={'source_id':source.id,'duration':20})
        self.assertEqual(response.status_code,200,response.text)
        self.client.patch('/api/editing',json={})
        with self.sessions() as db:job=db.get(Job,response.json()['job_id'])
        self.assertEqual(job.payload['editing'],options)
        def render(source,destination,start,duration,**kwargs):
            self.assertEqual((start,duration),(2,8))
            self.assertEqual(kwargs,{'speed':1.5,'quality':'720p','music':None})
            destination.write_bytes(b'edited clip')
        with patch.object(worker,'render_vertical_clip',side_effect=render) as renderer:
            first=worker.process_render(job);second=worker.process_render(job)
            self.assertEqual(first,second);renderer.assert_called_once()
        with self.sessions() as db:
            clip=db.get(Clip,first['clip_ids'][0]);self.assertEqual((clip.start_seconds,clip.duration_seconds),(2,8))

    def test_discovery_download_and_render_share_music_snapshot(self):
        options=self.upload_music()['settings']
        response=self.client.post('/api/discover',json={'query':'bread recipe','clip_duration':5})
        with self.sessions() as db:job=db.get(Job,response.json()['job_id'])
        self.assertEqual(job.payload['editing'],options)
        self.client.patch('/api/editing',json={})
        with patch.object(worker,'search_youtube',return_value=[{'url':'https://youtu.be/music-snapshot','title':'Bread','channel':'Author'}]):
            worker.process_discovery(job)
        with self.sessions() as db:download=db.scalar(select(Job).where(Job.type==JobType.download))
        self.assertEqual(download.payload['auto_render']['editing'],options)
        class Downloader:
            def download(self,url,path):path.write_bytes(b'video')
        with patch.object(worker,'probe_duration',return_value=20):worker.process_download(download,Downloader())
        with self.sessions() as db:render=db.scalar(select(Job).where(Job.type==JobType.render))
        self.assertEqual(render.payload['editing'],options)

    def test_portrait_direct_upload_is_edited_before_publication(self):
        self.client.patch('/api/editing',json={'speed':2,'quality':'720p'})
        with patch.object(main,'probe_duration',return_value=20),patch.object(main,'probe_video_stream',return_value=(1080,1920,True)):
            response=self.client.post('/api/youtube/direct-upload',data={'account_id':self.account_id,'consent':'true'},files={'file':('portrait.mp4',b'fixture','video/mp4')})
        self.assertEqual(response.status_code,202,response.text)
        with self.sessions() as db:job=db.get(Job,response.json()['job_id'])
        self.assertEqual(job.type,JobType.render)
        self.assertEqual(job.payload['duration'],10)
        self.assertEqual(job.payload['editing']['speed'],2)

    def test_excerpt_limit_applies_after_speed_and_checks_bounds(self):
        self.assertEqual(excerpt_plan(60,15,EditingOptions(start_seconds=5,end_seconds=25,speed=2)),(5,10))
        self.assertEqual(excerpt_plan(60,15,EditingOptions(start_seconds=5,end_seconds=50,speed=0.5)),(5,15))
        with self.assertRaises(ValueError):excerpt_plan(10,15,EditingOptions(speed=2))

    def test_private_transfer_preserves_music_and_edit_preferences(self):
        from deploy.data_transfer import export_data,restore_data,database_connection
        music=self.upload_music();identity=music['settings']['music_id']
        backup=self.root/'private.zip';destination=self.root/'restored'
        self.assertEqual(export_data(self.root/'test.db',self.root,backup),2)
        self.assertEqual(restore_data(backup,destination),2)
        self.assertEqual((destination/'music'/f'{identity}.m4a').read_bytes(),b'normalized audio')
        with database_connection(destination/'clipforge.db') as db:
            config=json.loads(db.execute('SELECT config FROM automation_plans WHERE id=1').fetchone()[0])
        self.assertEqual(config['editing']['music_id'],identity)
