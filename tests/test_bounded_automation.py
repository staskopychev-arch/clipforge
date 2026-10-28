import unittest
from datetime import timedelta
from sqlalchemy import select
from app.models import AutomationPlan,Job,JobType,RecordStatus,SourceVideo,TikTokWebPublication,utcnow
from app.services.scheduler import start_plan,schedule_tick,confirmed_tiktok_count
import test_tiktok_scheduler as baseline


class BoundedAutomationTests(unittest.TestCase):
    setUp=baseline.TikTokSchedulerTests.setUp
    tearDown=baseline.TikTokSchedulerTests.tearDown
    finish=baseline.TikTokSchedulerTests.finish

    def add_post(self,db,aid,n,**changes):
        url=f'https://www.tiktok.com/@fixture/video/{1234567890123456780+n}'
        job=Job(type=JobType.tiktok_web_publish,status=RecordStatus.ready,
                payload={'account_id':aid},result={'confirmed':True,'video_url':url})
        db.add(job);db.flush()
        receipt=TikTokWebPublication(account_id=aid,job_id=job.id,file_hash=str(n),
            stage='published',video_url=url,created_at=utcnow()-timedelta(days=3))
        for key,value in changes.items():setattr(receipt,key,value)
        db.add(receipt);db.commit()
        return job,receipt

    def test_targets_use_old_confirmed_posts_and_stop_at_five_each(self):
        a,b=self.ids[:2]
        with self.sessions() as db:
            db.get(SourceVideo,self.sid).duration_seconds=300;db.commit()
            for n in range(3):self.add_post(db,b,n)
            start_plan(db,{**self.config,'account_ids':[a,b],'daily_limit':0,
                'source_ids':self.sids,'account_post_targets':{a:5,b:5}})
            self.assertTrue(schedule_tick(db))
        expected=[a,b,a,b,a,a,a]
        for n,aid in enumerate(expected):
            with self.sessions() as db:
                plan=db.get(AutomationPlan,1);parent=db.get(Job,plan.last_pipeline_id)
                self.assertEqual(parent.payload['tiktok_web_account_id'],aid)
                self.assertIn(parent.payload['source_id'],self.sids)
                post=self.finish(db)
                url=f'https://www.tiktok.com/@fixture/video/{2234567890123456780+n}'
                post.result={**post.result,'video_url':url}
                db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id==post.id)).video_url=url
                db.commit()
                self.assertEqual(schedule_tick(db),n<len(expected)-1)
        with self.sessions() as db:
            self.assertEqual(confirmed_tiktok_count(db,a),5)
            self.assertEqual(confirmed_tiktok_count(db,b),5)
            self.assertFalse(db.get(AutomationPlan,1).enabled)
            self.assertEqual(db.get(AutomationPlan,1).status,'stopped')
            count=len(db.scalars(select(Job)).all())
            self.assertFalse(schedule_tick(db))
            self.assertEqual(len(db.scalars(select(Job)).all()),count)

    def test_quota_wait_does_not_pretend_target_is_complete(self):
        aid=self.ids[0]
        with self.sessions() as db:
            _,r=self.add_post(db,aid,1);r.created_at=utcnow();db.commit()
            start_plan(db,{**self.config,'account_ids':[aid],'daily_limit':1,'account_post_targets':{aid:5}})
            self.assertFalse(schedule_tick(db))
            self.assertTrue(db.get(AutomationPlan,1).enabled)
            self.assertEqual(db.get(AutomationPlan,1).status,'quota_wait')

    def test_approved_source_exhaustion_never_discovers_unrelated_footage(self):
        with self.sessions() as db:
            with self.assertRaises(ValueError):
                start_plan(db,{**self.config,'source_ids':['missing-source']})
            self.assertFalse(schedule_tick(db))
            self.assertIsNone(db.get(AutomationPlan,1))
            self.assertEqual(db.scalars(select(Job)).all(),[])

    def test_receipt_mismatch_and_uncertain_attempt_are_not_created_videos(self):
        aid=self.ids[0]
        with self.sessions() as db:
            self.add_post(db,aid,1)
            j,r=self.add_post(db,aid,2);r.stage='submitting';db.commit()
            j,r=self.add_post(db,aid,3);j.result={'confirmed':False,'video_url':r.video_url};db.commit()
            j,r=self.add_post(db,aid,4);r.video_url='https://www.tiktok.com/@fixture/video/9999999999999999999';db.commit()
            self.assertEqual(confirmed_tiktok_count(db,aid),1)

    def test_invalid_targets_do_not_start_or_modify_a_plan(self):
        with self.sessions() as db:
            with self.assertRaises(ValueError):start_plan(db,{**self.config,'account_post_targets':{self.ids[0]:5}})
            self.assertIsNone(db.get(AutomationPlan,1))
            with self.assertRaises(ValueError):start_plan(db,{**self.config,'source_ids':[]})
            self.assertIsNone(db.get(AutomationPlan,1))

    def test_uncertain_result_keeps_target_run_paused_without_switching(self):
        with self.sessions() as db:
            start_plan(db,{**self.config,'account_post_targets':dict.fromkeys(self.ids,5)})
            self.assertTrue(schedule_tick(db));self.finish(db,uncertain=True)
            count=len(db.scalars(select(Job)).all())
            self.assertFalse(schedule_tick(db))
            self.assertEqual(db.get(AutomationPlan,1).status,'paused')
            self.assertEqual(len(db.scalars(select(Job)).all()),count)
