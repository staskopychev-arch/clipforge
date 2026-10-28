import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app import worker
from app.config import settings
from app.db import Base
from app.models import Account, AutomationPlan, Clip, Job, JobType, RecordStatus, SourceVideo, TikTokWebPublication, utcnow
from app.services import publishing
from app.services.content_selection import next_unique_start
from app.services.scheduler import schedule_tick, start_plan, stop_plan


class TikTokSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_engine(f"sqlite:///{self.root/'test.db'}")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.patches = [patch.object(settings, "media_root", self.root), patch.object(worker, "SessionLocal", self.sessions),
                        patch.object(publishing, "validate_shorts_file", return_value={}),
                        patch("app.services.scheduler.shutil.disk_usage", return_value=SimpleNamespace(free=10*1024**3))]
        for item in self.patches: item.start()
        settings.ensure_directories()
        with self.sessions() as db:
            accounts = [Account(platform="tiktok_web", external_id=f"fixture-{i}", access_token="fixture", refresh_token="", status=RecordStatus.ready) for i in range(3)]
            path = self.root/'source.mp4'
            path.write_bytes(b"fixture source")
            source = SourceVideo(source_url="https://www.youtube.com/watch?v=fixture1234", title="Bread tutorial", rights_confirmed=True,
                                 duration_seconds=90, local_path=str(path), status=RecordStatus.ready)
            db.add_all([*accounts, source]); db.commit()
            self.ids = [a.id for a in accounts]; self.sid = source.id
            self.sids = [source.id]
            for i in range(1, 10):
                extra = SourceVideo(source_url=f"https://youtu.be/scheduler-{i}", title="Bread tutorial", rights_confirmed=True, duration_seconds=90, local_path=str(path), status=RecordStatus.ready)
                db.add(extra); db.flush(); self.sids.append(extra.id)
            db.commit()
        self.config = {"platform":"tiktok_web", "topic":"any", "queries":["bread recipe"], "publish":True,
                       "account_ids":self.ids, "source_ids":self.sids, "interval_minutes":60, "daily_limit":3, "clip_count":1, "clip_duration":15, "visibility":"public"}

    def tearDown(self):
        for item in reversed(self.patches): item.stop()
        self.engine.dispose(); self.temp.cleanup()

    def finish(self, db, *, reviewed=False, uncertain=False):
        plan = db.get(AutomationPlan,1); parent = db.get(Job,plan.last_pipeline_id)
        source = db.get(SourceVideo,parent.payload.get("source_id", self.sid))
        start = next_unique_start(db,source,15)
        clip = Clip(source_id=source.id,start_seconds=start,duration_seconds=15,status=RecordStatus.ready)
        db.add(clip); db.flush()
        post = Job(type=JobType.tiktok_web_publish,status=RecordStatus.error if reviewed or uncertain else RecordStatus.ready,
                   payload={"account_id":parent.payload['tiktok_web_account_id'],"clip_id":clip.id,"pipeline_id":parent.id,"automation_plan_id":1},
                   result=None if uncertain else {"confirmed":True,"video_url":"https://www.tiktok.com/@fixture/video/1234567890123456789","public_visibility_confirmed":not reviewed})
        db.add(post); db.flush()
        db.add(TikTokWebPublication(account_id=post.payload['account_id'],job_id=post.id,file_hash=clip.id,
                                   stage="submitting" if uncertain else "published"))
        parent.status=RecordStatus.ready; parent.result={"clip_ids":[clip.id]}
        plan.next_run_at=utcnow()-timedelta(seconds=1); db.commit()
        return post

    def test_round_robin_persists_and_each_account_gets_a_different_excerpt(self):
        starts=[]
        with self.sessions() as db:
            start_plan(db,self.config); self.assertTrue(schedule_tick(db))
        for index in range(4):
            with self.sessions() as db:
                parent=db.get(Job,db.get(AutomationPlan,1).last_pipeline_id)
                self.assertEqual(parent.payload['tiktok_web_account_id'],self.ids[index%3])
                self.assertTrue(parent.payload['unique_tiktok_excerpt'])
                self.assertEqual(parent.payload['pipeline_id'],parent.id)
                self.assertFalse(schedule_tick(db))
                post=self.finish(db,reviewed=index==0)
                starts.append(db.get(Clip,post.payload['clip_id']).start_seconds)
                self.assertTrue(schedule_tick(db))
                self.assertTrue(db.get(AutomationPlan,1).enabled)
        self.assertEqual(starts,[0,0,0,0])

    def test_search_mode_does_not_replace_current_query_with_unrelated_cached_media(self):
        with self.sessions() as db:
            start_plan(db, {**self.config, 'source_ids': None, 'topic': 'any',
                'queries': ['tortang talong cooking']})
            self.assertTrue(schedule_tick(db))
            job = db.get(Job, db.get(AutomationPlan, 1).last_pipeline_id)
            self.assertEqual(job.type, JobType.discovery)
            self.assertEqual(job.payload['query'], 'tortang talong cooking')
            self.assertNotIn('source_id', job.payload)

    def test_empty_search_cannot_fall_back_to_unrelated_cached_video(self):
        with self.sessions() as db:
            start_plan(db, {**self.config, 'source_ids': None, 'topic': 'any',
                'queries': ['tortang talong cooking']})
            schedule_tick(db)
            job = db.get(Job, db.get(AutomationPlan, 1).last_pipeline_id)
        with patch.object(worker, 'search_youtube', return_value=[]):
            with self.assertRaisesRegex(RuntimeError, 'Поиск не нашёл'):
                worker.process_discovery(job)
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(Job)).all()), 1)

    def test_single_source_preparation_does_not_download_all_twenty_results(self):
        results = [{'url': f'https://www.youtube.com/watch?v=cooking{i:04}',
                    'title': 'Tortang talong cooking', 'channel': 'Cooking fixture'} for i in range(20)]
        with self.sessions() as db:
            job = Job(type=JobType.discovery, payload={'query': 'tortang talong cooking',
                'topic': 'any', 'max_results': 20, 'single_source': True,
                'clip_count': 1, 'clip_duration': 15})
            db.add(job); db.commit()
        with patch.object(worker, 'search_youtube', return_value=results):
            outcome = worker.process_discovery(job)
        self.assertEqual(len(outcome['created_source_ids']), 1)
        with self.sessions() as db:
            downloads = db.scalars(select(Job).where(Job.type == JobType.download)).all()
            self.assertEqual(len(downloads), 1)
            self.assertNotIn('fallback_candidates', downloads[0].payload)

    def test_uncertain_post_pauses_without_switching_accounts(self):
        with self.sessions() as db:
            start_plan(db,self.config); schedule_tick(db); self.finish(db,uncertain=True)
            count=len(db.scalars(select(Job)).all())
            self.assertFalse(schedule_tick(db)); self.assertFalse(db.get(AutomationPlan,1).enabled)
            self.assertEqual(len(db.scalars(select(Job)).all()),count)

    def expire_current(self, db, *, stage='reserved', kind='session_expired'):
        post=self.finish(db,uncertain=True)
        post.status=RecordStatus.reauth_required;post.result={'error_kind':kind}
        db.get(Account,post.payload['account_id']).status=RecordStatus.reauth_required
        receipt=db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id==post.id))
        receipt.stage=stage;db.commit()
        return post,receipt

    def test_expired_session_before_post_skips_without_retry_or_success(self):
        with self.sessions() as db:
            start_plan(db,self.config);schedule_tick(db)
            post,receipt=self.expire_current(db)
            self.assertFalse(schedule_tick(db))
            p=db.get(AutomationPlan,1)
            self.assertTrue(p.enabled);self.assertEqual(p.config['account_index'],1)
            self.assertEqual(p.config['expired_session_accounts'][self.ids[0]]['job_id'],post.id)
            self.assertEqual(post.status,RecordStatus.reauth_required);self.assertEqual(receipt.stage,'reserved')
            self.assertIsNone(receipt.video_url)
            self.assertTrue(schedule_tick(db))
            self.assertEqual(db.get(Job,p.last_pipeline_id).payload['tiktok_web_account_id'],self.ids[1])

    def test_expiry_after_post_or_manual_challenge_still_pauses(self):
        for stage,kind in [('submitting','session_expired'),('reserved','manual_verification')]:
            with self.subTest(stage=stage,kind=kind),self.sessions() as db:
                for aid in self.ids: db.get(Account,aid).status=RecordStatus.ready
                db.commit()
                start_plan(db,self.config);schedule_tick(db)
                self.expire_current(db,stage=stage,kind=kind)
                self.assertFalse(schedule_tick(db))
                self.assertFalse(db.get(AutomationPlan,1).enabled)
                self.assertNotIn('expired_session_accounts',db.get(AutomationPlan,1).config)

    def test_bounded_run_stops_after_all_expired_without_claiming_publications(self):
        with self.sessions() as db:
            start_plan(db,{**self.config,'account_post_targets':{aid:1 for aid in self.ids}})
            for _ in self.ids:
                self.assertTrue(schedule_tick(db));self.expire_current(db)
                self.assertFalse(schedule_tick(db))
            self.assertFalse(schedule_tick(db))
            p=db.get(AutomationPlan,1)
            self.assertFalse(p.enabled);self.assertEqual(p.status,'stopped')
            self.assertEqual(set(p.config['expired_session_accounts']),set(self.ids))
            self.assertTrue(all(r.stage=='reserved' for r in db.scalars(select(TikTokWebPublication)).all()))

    def test_no_daily_quota_rotates_through_accounts_with_old_receipts(self):
        with self.sessions() as db:
            for aid in self.ids:
                for index in range(4):
                    job=Job(type=JobType.tiktok_web_publish,status=RecordStatus.ready,payload={'account_id':aid})
                    db.add(job); db.flush()
                    db.add(TikTokWebPublication(account_id=aid,job_id=job.id,file_hash=f'{aid}-{index}',stage='published'))
            db.commit()
            start_plan(db,{**self.config,'interval_minutes':1,'daily_limit':0})
            self.assertTrue(schedule_tick(db))
            plan=db.get(AutomationPlan,1)
            self.assertEqual(db.get(Job,plan.last_pipeline_id).payload['tiktok_web_account_id'],self.ids[0])
            self.finish(db)
            self.assertTrue(schedule_tick(db))
            self.assertEqual(db.get(Job,plan.last_pipeline_id).payload['tiktok_web_account_id'],self.ids[1])

    def test_pending_source_fallback_is_not_mistaken_for_terminal_failure(self):
        with self.sessions() as db:
            start_plan(db,self.config); schedule_tick(db)
            plan=db.get(AutomationPlan,1); parent=db.get(Job,plan.last_pipeline_id)
            following=Job(type=JobType.download,payload={"pipeline_id":parent.id},status=RecordStatus.queued)
            db.add(following); db.flush()
            parent.status=RecordStatus.error; parent.result={"fallback_job_id":following.id}; db.commit()
            self.assertFalse(schedule_tick(db)); self.assertTrue(plan.enabled)
            self.assertEqual(plan.last_pipeline_id,parent.id)

    def test_global_guard_rejects_same_excerpt_on_another_account(self):
        with self.sessions() as db:
            source=db.get(SourceVideo,self.sid)
            for index in range(2):
                path=self.root/f'{index}.mp4'; path.write_bytes(f'different bytes {index}'.encode())
                clip=Clip(source_id=source.id,start_seconds=index,duration_seconds=15,local_path=str(path),status=RecordStatus.ready)
                db.add(clip); db.flush()
                if index==0:
                    publishing.enqueue_tiktok_web(db,account_id=self.ids[0],clip=clip,description='fixture',unique_across_accounts=True); db.commit()
                else:
                    with self.assertRaisesRegex(ValueError,'уже ставился'):
                        publishing.enqueue_tiktok_web(db,account_id=self.ids[1],clip=clip,description='fixture',unique_across_accounts=True)

    def test_render_restart_reuses_reserved_publication_instead_of_creating_another(self):
        with self.sessions() as db:
            start_plan(db,self.config); schedule_tick(db)
            parent=db.get(Job,db.get(AutomationPlan,1).last_pipeline_id)
        def render(source, destination, start, duration):
            destination.write_bytes(b'rendered fixture')
        with patch.object(worker, 'render_vertical_clip', side_effect=render) as renderer:
            first=worker.process_render(parent)
            second=worker.process_render(parent)
            renderer.assert_called_once()
        self.assertEqual(first['clip_ids'],second['clip_ids'])
        self.assertEqual(first['publish_job_id'],second['publish_job_id'])
        self.assertTrue(second['recovered_existing'])

    def test_tiktok_stop_cancels_queued_publication(self):
        with self.sessions() as db:
            start_plan(db,self.config); schedule_tick(db); post=self.finish(db)
            post.status=RecordStatus.queued; db.commit(); stop_plan(db)
            self.assertEqual(post.status,RecordStatus.error)
            self.assertFalse(schedule_tick(db))

    def test_preview_cannot_use_unlisted_tiktok_visibility(self):
        with self.sessions() as db:
            with self.assertRaises(ValueError): start_plan(db,{**self.config,"visibility":"unlisted"})

    def test_verified_pre_post_failure_does_not_consume_publication_quota(self):
        with self.sessions() as db:
            start_plan(db,{**self.config,"daily_limit":1}); schedule_tick(db)
            post=self.finish(db,uncertain=True)
            post.result={"verified_no_post":True,"stage":"uploading"}
            receipt=db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id==post.id))
            receipt.stage='uploading'
            plan=db.get(AutomationPlan,1); plan.last_pipeline_id=None; db.commit()
            self.assertTrue(schedule_tick(db))
            new=db.get(Job,plan.last_pipeline_id)
            self.assertEqual(new.payload['tiktok_web_account_id'],self.ids[0])
            self.assertEqual(receipt.stage,'uploading')
