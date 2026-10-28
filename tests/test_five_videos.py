import copy
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from app import worker
from sqlalchemy import select
from app.models import SourceVideo, Account, AutomationPlan, Clip, Job, RecordStatus, TikTokWebPublication, utcnow
from app.services.five_videos import save_draft, start_batch
from app.services.clip_review import review_clip
from app.services.scheduler import schedule_tick
import test_tiktok_scheduler as baseline


class FiveVideoTests(unittest.TestCase):
    setUp = baseline.TikTokSchedulerTests.setUp
    tearDown = baseline.TikTokSchedulerTests.tearDown

    def draft(self, db, amount=20):
        accounts = [db.get(Account, aid) for aid in self.ids]
        for i in range(amount - len(accounts)):
            a = Account(platform='tiktok_web', external_id=f'five-fixture-{i}', display_name=f'Account {i}', access_token='fixture', refresh_token='', status=RecordStatus.ready)
            db.add(a); accounts.append(a)
        db.flush(); ids = [a.id for a in accounts[:amount]]
        videos = []
        for i in range(5):
            path = self.root / f'five-{i}.mp4'; path.write_bytes(f'different-video-{i}'.encode())
            source = SourceVideo(source_url=f"https://youtu.be/five-source-{i}", rights_confirmed=True, status=RecordStatus.ready)
            db.add(source); db.flush()
            c = Clip(source_id=source.id, start_seconds=0, duration_seconds=15, status=RecordStatus.ready, local_path=str(path))
            db.add(c); db.flush()
            videos.append({'description': f'Filipino description {i}', 'clip_id': c.id})
        db.commit(); save_draft(db, videos, ids)
        return videos, ids

    def start(self, db):
        start_batch(db, interval_minutes=3, daily_limit=0, clip_duration=15, queries=['bread recipe'], topic='any', visibility='public')
        batch = db.get(AutomationPlan, 1).config['five_video_batch']
        for cid in batch['clip_ids']:
            if cid:
                review_clip(db, batch_id=batch['id'], clip_id=cid, decision='approved', watched=True)

    def finish(self, db, n, public=True):
        plan = db.get(AutomationPlan, 1)
        job = db.get(Job, plan.last_pipeline_id)
        url = f'https://www.tiktok.com/@fixture/video/{7699000000000000000+n}'
        job.status = RecordStatus.ready
        job.result = {'confirmed': True, 'video_url': url, 'public_visibility_confirmed': public}
        receipt = db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id == job.id))
        receipt.stage = 'published'; receipt.video_url = url
        plan.next_run_at = utcnow() - timedelta(seconds=1); db.commit()
        return job

    def test_thirty_accounts_five_clips_video_major_and_exactly_one_hundred_fifty_posts(self):
        with self.sessions() as db:
            videos, ids = self.draft(db, amount=30)
            self.start(db); self.assertTrue(schedule_tick(db))
            for n in range(150):
                job = db.get(Job, db.get(AutomationPlan, 1).last_pipeline_id)
                self.assertEqual(job.payload['account_id'], ids[n % 30])
                self.assertEqual(job.payload['clip_id'], videos[n // 30]['clip_id'])
                self.assertTrue(job.payload['description'].startswith(videos[n // 30]['description']))
                self.assertFalse(schedule_tick(db), 'Do not enqueue another while one is pending')
                self.finish(db, n, public=n != 0)
                self.assertEqual(schedule_tick(db), n < 149)
            plan = db.get(AutomationPlan, 1)
            self.assertFalse(plan.enabled); self.assertEqual(plan.status, 'stopped')
            self.assertEqual(plan.config['five_video_batch']['position'], 150)
            self.assertEqual(plan.config['five_video_batch']['status'], 'complete')
            self.assertEqual(len(db.scalars(select(Job)).all()), 150)
            self.assertFalse(schedule_tick(db))
            self.assertEqual(len(db.scalars(select(TikTokWebPublication)).all()), 150)

    def test_draft_save_does_not_start_or_reset_stopped_plan(self):
        with self.sessions() as db:
            videos, ids = self.draft(db)
            plan = db.get(AutomationPlan, 1)
            plan.config = {**plan.config, 'account_index': 2, 'query_index': 19, 'caption': 'old'}
            plan.last_pipeline_id = 'historical'; db.commit()
            before = copy.deepcopy(plan.config)
            videos[0]['description'] = 'Edited'
            save_draft(db, videos, ids)
            plan = db.get(AutomationPlan, 1)
            self.assertFalse(plan.enabled)
            self.assertEqual(plan.last_pipeline_id, 'historical')
            for key in ('account_index', 'query_index', 'caption'):
                self.assertEqual(plan.config[key], before[key])
            self.assertEqual(db.scalars(select(Job)).all(), [])

    def test_uncertain_submission_pauses_without_switch_or_repeat(self):
        with self.sessions() as db:
            self.draft(db); self.start(db); schedule_tick(db)
            plan = db.get(AutomationPlan, 1); job = db.get(Job, plan.last_pipeline_id)
            job.status = RecordStatus.error; job.error = 'Uncertain after Post'; db.commit()
            self.assertFalse(schedule_tick(db))
            self.assertFalse(plan.enabled); self.assertEqual(plan.status, 'paused')
            self.assertEqual(plan.config['five_video_batch']['position'], 0)
            self.assertEqual(len(db.scalars(select(Job)).all()), 1)
            with self.assertRaises(ValueError): self.start(db)

    def test_stopped_batch_allows_new_account_draft_without_replacing_queue(self):
        with self.sessions() as db:
            videos, ids = self.draft(db)
            self.start(db)
            plan = db.get(AutomationPlan, 1)
            plan.enabled = False; plan.status = 'stopped'; db.commit()
            original = copy.deepcopy(plan.config['five_video_batch'])
            save_draft(db, videos, ids[-7:])
            plan = db.get(AutomationPlan, 1)
            self.assertEqual(plan.config['five_video_draft']['account_ids'], ids[-7:])
            self.assertEqual(plan.config['five_video_batch'], original)
            self.assertFalse(plan.enabled)
            self.assertEqual(db.scalars(select(Job)).all(), [])
            self.start(db)
            self.assertNotEqual(plan.config['five_video_batch']['id'], original['id'])
            self.assertEqual(plan.config['five_video_batch']['account_ids'], ids[-7:])
            self.assertEqual(plan.config['five_video_batch']['position'], 0)
            archived = plan.config['five_video_batch_history'][0]
            self.assertEqual(archived['id'], original['id'])
            self.assertEqual(archived['account_ids'], original['account_ids'])

    def test_running_batch_still_rejects_account_changes(self):
        with self.sessions() as db:
            videos, ids = self.draft(db); self.start(db)
            with self.assertRaises(ValueError): save_draft(db, videos, ids[-7:])
            self.assertEqual(db.get(AutomationPlan, 1).config['five_video_draft']['account_ids'], ids)

    def test_new_account_draft_cannot_bypass_unconfirmed_submission(self):
        with self.sessions() as db:
            videos, ids = self.draft(db); self.start(db); schedule_tick(db)
            plan = db.get(AutomationPlan, 1); job = db.get(Job, plan.last_pipeline_id)
            job.status = RecordStatus.error; job.error = 'Uncertain after Post'
            plan.enabled = False; plan.status = 'stopped'; db.commit()
            original = copy.deepcopy(plan.config['five_video_batch'])
            save_draft(db, videos, ids[-7:])
            with self.assertRaises(ValueError): self.start(db)
            self.assertFalse(plan.enabled)
            self.assertEqual(plan.config['five_video_batch'], original)
            self.assertEqual(len(db.scalars(select(Job)).all()), 1)

    def mark_post_refused(self, db, *, owner_review=False):
        plan = db.get(AutomationPlan, 1); job = db.get(Job, plan.last_pipeline_id)
        job.status = RecordStatus.error
        job.result = {'stage':'submitting','platform_post_code':21,'requires_owner_review':owner_review}
        job.error = 'Post refused: 21'
        db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id == job.id)).stage = 'submitting'
        plan.next_run_at = utcnow() - timedelta(seconds=1); db.commit()
        return job

    def test_owner_enabled_post_error_skip_preserves_receipt_and_counts_only_created_posts(self):
        with self.sessions() as db:
            videos, ids = self.draft(db, amount=1); self.start(db)
            plan = db.get(AutomationPlan, 1)
            batch = copy.deepcopy(plan.config['five_video_batch']); batch['skip_post_error_21'] = True
            plan.config = {**plan.config, 'five_video_batch':batch}; db.commit()
            schedule_tick(db); refused = self.mark_post_refused(db)
            self.assertTrue(schedule_tick(db))
            plan = db.get(AutomationPlan,1); b = plan.config['five_video_batch']
            self.assertEqual(b['position'],1); self.assertEqual(b['completed'],[]); self.assertEqual(len(b['skipped']),1)
            receipt = db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id==refused.id))
            self.assertEqual(receipt.stage,'submitting'); self.assertEqual(db.get(Job,refused.id).status,RecordStatus.error)
            for n in range(4):
                self.finish(db,n)
                self.assertEqual(schedule_tick(db),n<3)
            b = db.get(AutomationPlan,1).config['five_video_batch']
            self.assertEqual(b['position'],5); self.assertEqual(len(b['completed']),4); self.assertEqual(len(b['skipped']),1)
            self.assertFalse(db.get(AutomationPlan,1).enabled)
            self.assertEqual(len(db.scalars(select(Job)).all()),5)

    def test_owner_review_and_session_errors_never_skip_even_with_owner_option(self):
        with self.sessions() as db:
            self.draft(db); self.start(db)
            plan=db.get(AutomationPlan,1); batch=copy.deepcopy(plan.config['five_video_batch'])
            batch['skip_post_error_21']=True; plan.config={**plan.config,'five_video_batch':batch}; db.commit()
            schedule_tick(db); self.mark_post_refused(db,owner_review=True)
            self.assertFalse(schedule_tick(db))
            self.assertEqual(db.get(AutomationPlan,1).config['five_video_batch']['position'],0)
            self.assertEqual(len(db.scalars(select(Job)).all()),1)

    def test_expired_session_skips_remaining_account_slots_without_uploading_again(self):
        with self.sessions() as db:
            self.draft(db,amount=1);self.start(db);schedule_tick(db)
            p=db.get(AutomationPlan,1);job=db.get(Job,p.last_pipeline_id)
            job.status=RecordStatus.reauth_required;job.result={'error_kind':'session_expired'}
            db.get(Account,job.payload['account_id']).status=RecordStatus.reauth_required
            receipt=db.scalar(select(TikTokWebPublication).where(TikTokWebPublication.job_id==job.id))
            receipt.stage='reserved';db.commit()
            for _ in range(6):schedule_tick(db)
            p=db.get(AutomationPlan,1);b=p.config['five_video_batch']
            self.assertFalse(p.enabled);self.assertEqual(b['status'],'complete')
            self.assertEqual(b['position'],5);self.assertEqual(len(b['skipped']),5)
            self.assertEqual(b['completed'],[])
            self.assertEqual(len(db.scalars(select(Job)).all()),1)
            self.assertEqual(receipt.stage,'reserved');self.assertIsNone(receipt.video_url)

    def test_descriptions_must_be_different_and_five_files_unique(self):
        with self.sessions() as db:
            videos, ids = self.draft(db)
            videos[1]['description'] = videos[0]['description']; save_draft(db, videos, ids)
            with self.assertRaises(ValueError): self.start(db)
            videos[1]['description'] = 'Different'; videos[1]['clip_id'] = videos[0]['clip_id']; save_draft(db, videos, ids)
            with self.assertRaises(ValueError): self.start(db)

    def test_edit_future_caption_preserves_pending_job_and_cursor(self):
        with self.sessions() as db:
            videos, ids = self.draft(db); self.start(db); schedule_tick(db)
            plan = db.get(AutomationPlan, 1); job = db.get(Job, plan.last_pipeline_id)
            old = job.payload['description']; before = copy.deepcopy(plan.config['five_video_batch'])
            videos[0]['description'] = 'Edited for next account'; save_draft(db, videos, ids)
            self.assertEqual(db.get(Job, job.id).payload['description'], old)
            self.assertEqual(db.get(AutomationPlan, 1).config['five_video_batch'], before)
            self.finish(db, 1); self.assertTrue(schedule_tick(db))
            next_job = db.get(Job, db.get(AutomationPlan, 1).last_pipeline_id)
            self.assertTrue(next_job.payload['description'].startswith('Edited for next account'))

    def test_quota_holds_same_account_instead_of_skipping_it(self):
        with self.sessions() as db:
            self.draft(db); self.start(db); schedule_tick(db); self.finish(db, 1)
            plan = db.get(AutomationPlan, 1); aid = plan.config['five_video_batch']['account_ids'][1]
            # Current account is still the next one; invalid login must pause here.
            db.get(Account, aid).status = RecordStatus.reauth_required; db.commit()
            self.assertFalse(schedule_tick(db))
            self.assertEqual(plan.config['five_video_batch']['position'], 1)
            self.assertEqual(plan.status, 'paused')

    def test_automatic_mode_renders_exactly_five_distinct_clips_and_reuses_each(self):
        with self.sessions() as db:
            videos, ids = self.draft(db, amount=3)
            for v in videos: v['clip_id'] = None
            save_draft(db, videos, ids)
            start_batch(db, interval_minutes=3, daily_limit=0, clip_duration=15, queries=['tortang talong cooking'], topic='any', visibility='public')
            used = []
            for round_index in range(5):
                if round_index == 0:
                    self.assertTrue(schedule_tick(db))
                plan = db.get(AutomationPlan, 1); root = db.get(Job, plan.last_pipeline_id)
                self.assertEqual(root.type.value, 'discovery')
                self.assertEqual(root.payload['query'], 'tortang talong cooking')
                self.assertTrue(root.payload['single_source'])
                self.assertFalse(root.payload.get('tiktok_web_account_id'))
                # Reuse is allowed only after the actual search returns this URL.
                source = db.get(baseline.SourceVideo, self.sids[round_index])
                found = [{'url': source.source_url, 'title': 'Tortang talong cooking', 'channel': 'Cooking fixture'}]
                with patch.object(worker, 'search_youtube', return_value=found) as search:
                    discovered = worker.process_discovery(root)
                search.assert_called_once_with('tortang talong cooking', 20, 'any')
                db.expire_all(); root = db.get(Job, root.id)
                root.status = RecordStatus.ready; root.result = discovered; db.commit()
                render_job = next(j for j in db.scalars(select(Job)).all()
                    if j.type.value == 'render' and j.payload.get('pipeline_id') == root.id)
                def render(source, destination, start, duration):
                    destination.write_bytes(f'actual-different-footage-{round_index}'.encode())
                with patch.object(worker, 'render_vertical_clip', side_effect=render):
                    result = worker.process_render(render_job)
                db.expire_all(); render_job = db.get(Job, render_job.id)
                render_job.status = RecordStatus.ready; render_job.result = result; db.commit()
                self.assertFalse(schedule_tick(db))
                plan = db.get(AutomationPlan, 1)
                self.assertEqual(plan.status, 'review_required')
                cid = result['clip_ids'][0]; used.append(cid)
                review_clip(db, batch_id=plan.config['five_video_batch']['id'], clip_id=cid, decision='approved', watched=True)
                start_batch(db, interval_minutes=3, daily_limit=0, clip_duration=15,
                    queries=['tortang talong cooking'], topic='any', visibility='public')
                self.assertTrue(schedule_tick(db))
                for account_index in range(3):
                    post = db.get(Job, db.get(AutomationPlan, 1).last_pipeline_id)
                    self.assertEqual(post.payload['clip_id'], cid)
                    self.assertEqual(post.payload['account_id'], ids[account_index])
                    self.finish(db, round_index * 3 + account_index)
                    if account_index < 2:
                        self.assertTrue(schedule_tick(db))
                self.assertEqual(schedule_tick(db), round_index < 4)
            self.assertEqual(len(set(used)), 5)
            self.assertEqual([db.get(Clip, c).start_seconds for c in used], [0, 0, 0, 0, 0])

    def test_changed_search_archives_stopped_batch_and_discards_automatic_old_clips(self):
        with self.sessions() as db:
            videos, ids = self.draft(db, amount=3)
            for video in videos: video['clip_id'] = None
            save_draft(db, videos, ids)
            start_batch(db, interval_minutes=3, daily_limit=0, clip_duration=15,
                queries=['bread baking'], topic='any', visibility='public')
            plan = db.get(AutomationPlan, 1)
            original = copy.deepcopy(plan.config['five_video_batch'])
            original['clip_ids'][0] = 'previous-generated-clip'
            plan.config = {**plan.config, 'five_video_batch': original}
            plan.enabled = False; plan.status = 'stopped'; db.commit()
            start_batch(db, interval_minutes=3, daily_limit=0, clip_duration=15,
                queries=['tortang talong cooking'], topic='any', visibility='public')
            self.assertNotEqual(plan.config['five_video_batch']['id'], original['id'])
            self.assertEqual(plan.config['five_video_batch']['clip_ids'], [None] * 5)
            self.assertEqual(plan.config['five_video_batch_history'][-1]['clip_ids'][0], 'previous-generated-clip')
            self.assertTrue(schedule_tick(db))
            self.assertEqual(db.get(Job, plan.last_pipeline_id).payload['query'], 'tortang talong cooking')

    def test_changed_search_does_not_clear_uncertain_submission(self):
        with self.sessions() as db:
            self.draft(db); self.start(db); schedule_tick(db)
            plan = db.get(AutomationPlan, 1); job = db.get(Job, plan.last_pipeline_id)
            job.status = RecordStatus.error; job.error = 'Uncertain after Post'
            plan.enabled = False; plan.status = 'stopped'; db.commit()
            before = copy.deepcopy(plan.config)
            with self.assertRaises(ValueError):
                start_batch(db, interval_minutes=3, daily_limit=0, clip_duration=15,
                    queries=['tortang talong cooking'], topic='any', visibility='public')
            self.assertEqual(plan.config, before)

    def test_legacy_automatic_clip_cannot_resume_even_when_query_text_is_unchanged(self):
        with self.sessions() as db:
            videos, ids = self.draft(db, amount=3)
            for video in videos: video['clip_id'] = None
            save_draft(db, videos, ids)
            options = dict(interval_minutes=3, daily_limit=0, clip_duration=15,
                queries=['tortang talong cooking'], topic='any', visibility='public')
            start_batch(db, **options)
            plan = db.get(AutomationPlan, 1)
            legacy = copy.deepcopy(plan.config['five_video_batch'])
            legacy.pop('search_generation_version')
            legacy['clip_ids'][0] = 'legacy-wrong-topic-clip'
            legacy['position'] = 2
            legacy['completed'] = ['historical-confirmed-job-1', 'historical-confirmed-job-2']
            plan.config = {**plan.config, 'five_video_batch': legacy}
            plan.enabled = False; plan.status = 'stopped'; db.commit()
            start_batch(db, **options)
            fresh = plan.config['five_video_batch']
            self.assertNotEqual(fresh['id'], legacy['id'])
            self.assertEqual(fresh['clip_ids'], [None] * 5)
            self.assertEqual(fresh['position'], 0)
            self.assertEqual(plan.config['five_video_batch_history'][-1]['completed'], legacy['completed'])
            self.assertEqual(db.scalars(select(Job)).all(), [])
            self.assertTrue(schedule_tick(db))
            self.assertEqual(db.get(Job, plan.last_pipeline_id).type.value, 'discovery')

    def test_current_search_generation_resumes_without_resetting_clips_or_cursor(self):
        with self.sessions() as db:
            videos, ids = self.draft(db, amount=3)
            for video in videos: video['clip_id'] = None
            save_draft(db, videos, ids)
            options = dict(interval_minutes=3, daily_limit=0, clip_duration=15,
                queries=['tortang talong cooking'], topic='any', visibility='public')
            start_batch(db, **options)
            plan = db.get(AutomationPlan, 1)
            current = copy.deepcopy(plan.config['five_video_batch'])
            current['clip_ids'][0] = 'current-query-verified-clip'
            current['position'] = 2
            plan.config = {**plan.config, 'five_video_batch': current}
            plan.enabled = False; plan.status = 'stopped'; db.commit()
            start_batch(db, **options)
            self.assertEqual(plan.config['five_video_batch']['id'], current['id'])
            self.assertEqual(plan.config['five_video_batch']['clip_ids'], current['clip_ids'])
            self.assertEqual(plan.config['five_video_batch']['position'], 2)


if __name__ == '__main__': unittest.main()
