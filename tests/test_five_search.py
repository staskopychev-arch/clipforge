import copy
import unittest
from unittest.mock import patch

from sqlalchemy import select
from app import worker
from app.models import AutomationPlan, Job, JobType, RecordStatus, SourceVideo
from app.services.five_videos import save_draft, start_batch
from app.services.scheduler import schedule_tick, start_plan
from app.services.youtube import YouTubeNoResultsError, YouTubeSearchError
import test_five_videos as baseline_five


class FiveSearchTests(unittest.TestCase):
    setUp = baseline_five.FiveVideoTests.setUp
    tearDown = baseline_five.FiveVideoTests.tearDown
    draft = baseline_five.FiveVideoTests.draft

    def test_five_video_preparation_captures_saved_editing(self):
        from app.services.editing import EditingOptions,update_editing
        with self.sessions() as db:
            options=EditingOptions(start_seconds=3,end_seconds=24,speed=1.5,quality='720p')
            update_editing(db,options)
            plan,root=self.prepare(db)
            self.assertEqual(root.payload['editing'],options.model_dump())
            update_editing(db,EditingOptions())
            db.refresh(root)
            self.assertEqual(root.payload['editing'],options.model_dump())

    def test_regular_start_preserves_saved_editing_and_music_library(self):
        from app.services.editing import EditingOptions,update_editing
        with self.sessions() as db:
            options=EditingOptions(speed=1.25,quality='480p')
            update_editing(db,options)
            start_plan(db,{**self.config,'source_ids':None})
            self.assertEqual(db.get(AutomationPlan,1).config['editing'],options.model_dump())
            self.assertEqual(db.get(AutomationPlan,1).config['music_library'],[])
            self.assertTrue(schedule_tick(db))
            job=db.get(Job,db.get(AutomationPlan,1).last_pipeline_id)
            self.assertEqual(job.payload['editing'],options.model_dump())

    def prepare(self, db):
        videos, ids = self.draft(db, amount=3)
        for video in videos:
            video['clip_id'] = None
        save_draft(db, videos, ids)
        self.options = dict(interval_minutes=3, daily_limit=0, clip_duration=15,
                            queries=['empty query', 'bread recipe'], topic='any', visibility='public')
        start_batch(db, **self.options)
        self.assertTrue(schedule_tick(db))
        plan = db.get(AutomationPlan, 1)
        return plan, db.get(Job, plan.last_pipeline_id)

    def pause_search(self, db, plan, root):
        root.status = RecordStatus.error
        root.error = 'No search results'
        root.result = {'error_kind': 'empty_search'}
        plan.enabled = False
        plan.status = 'paused'
        plan.last_error = root.error
        db.commit()

    def test_empty_first_query_uses_next_explicit_query(self):
        with self.sessions() as db:
            plan, root = self.prepare(db)
            self.assertEqual(root.payload['queries'], self.options['queries'])
            source = db.get(SourceVideo, self.sid)
            found = [{'url': source.source_url, 'title': 'Bread recipe', 'channel': 'Fixture'}]
            with patch.object(worker, 'search_youtube', side_effect=[[], found]) as search:
                result = worker.process_discovery(root)
            self.assertEqual([c.args[0] for c in search.call_args_list], self.options['queries'])
            self.assertEqual(result['found'], 1)
            self.assertEqual(result['search_attempts'][0]['returned'], 0)
            self.assertEqual(result['search_attempts'][1]['accepted'], 1)
            db.expire_all()
            self.assertEqual(len(db.scalars(select(Job).where(Job.type == JobType.render)).all()), 1)
            self.assertEqual(len(db.scalars(select(Job).where(Job.type == JobType.tiktok_web_publish)).all()), 0)

    def test_all_queries_empty_preserves_evidence_and_does_not_reuse_media(self):
        with self.sessions() as db:
            plan, root = self.prepare(db)
            with patch.object(worker, 'search_youtube', return_value=[]) as search:
                with self.assertRaises(YouTubeNoResultsError) as caught:
                    worker.process_discovery(root)
            self.assertEqual(search.call_count, 2)
            self.assertEqual([a['query'] for a in caught.exception.attempts], self.options['queries'])
            worker.fail_job(root.id, str(caught.exception), error_kind='empty_search', search_attempts=caught.exception.attempts)
            db.expire_all()
            self.assertEqual(db.get(Job, root.id).result['search_attempts'], caught.exception.attempts)
            self.assertEqual(len(db.scalars(select(Job)).all()), 1)

    def test_quota_or_auth_error_does_not_fall_back_to_another_query(self):
        with self.sessions() as db:
            plan, root = self.prepare(db)
            with patch.object(worker, 'search_youtube', side_effect=YouTubeSearchError('Quota exhausted')) as search:
                with self.assertRaises(YouTubeSearchError):
                    worker.process_discovery(root)
            self.assertEqual(search.call_count, 1)

    def test_same_query_restart_creates_new_search_without_overwriting_failed_job(self):
        with self.sessions() as db:
            plan, root = self.prepare(db)
            self.pause_search(db, plan, root)
            before = copy.deepcopy(plan.config['five_video_batch'])
            start_batch(db, **self.options)
            self.assertEqual(plan.config['five_video_batch']['id'], before['id'])
            self.assertIsNone(plan.config['five_video_batch']['prepare_pipeline_id'])
            self.assertTrue(schedule_tick(db))
            self.assertNotEqual(plan.last_pipeline_id, root.id)
            self.assertEqual(db.get(Job, root.id).error, 'No search results')
            self.assertEqual(db.get(Job, root.id).status, RecordStatus.error)

    def test_previous_first_result_stop_can_be_resumed_with_ordered_selection(self):
        with self.sessions() as db:
            plan, root = self.prepare(db)
            self.pause_search(db, plan, root)
            root.result = {'error_kind': 'first_result_unusable'}
            db.commit()
            before_id = plan.config['five_video_batch']['id']
            start_batch(db, **self.options)
            self.assertEqual(plan.config['five_video_batch']['id'], before_id)
            self.assertIsNone(plan.config['five_video_batch']['prepare_pipeline_id'])
            self.assertTrue(schedule_tick(db))
            self.assertNotEqual(plan.last_pipeline_id, root.id)

    def test_changed_query_after_empty_search_archives_untouched_batch(self):
        with self.sessions() as db:
            plan, root = self.prepare(db)
            self.pause_search(db, plan, root)
            old_id = plan.config['five_video_batch']['id']
            start_batch(db, **{**self.options, 'queries': ['new bread recipe']})
            self.assertNotEqual(plan.config['five_video_batch']['id'], old_id)
            self.assertEqual(plan.config['five_video_batch_history'][-1]['id'], old_id)
            self.assertTrue(schedule_tick(db))
            self.assertEqual(db.get(Job, plan.last_pipeline_id).payload['queries'], ['new bread recipe'])

    def assert_search_change_blocked(self, kind):
        with self.sessions() as db:
            plan, root = self.prepare(db)
            self.pause_search(db, plan, root)
            if kind == 'download_child':
                db.add(Job(type=JobType.download, status=RecordStatus.error, payload={'pipeline_id': root.id}))
            else:
                root.result = {'error_kind': kind}
            db.commit()
            before = copy.deepcopy(plan.config)
            with self.assertRaises(ValueError):
                start_batch(db, **{**self.options, 'queries': ['new recipe']})
            self.assertEqual(plan.config, before)

    def test_download_pause_cannot_be_cleared_by_search_change(self):
        self.assert_search_change_blocked('download_child')

    def test_manual_verification_pause_cannot_be_cleared_by_search_change(self):
        self.assert_search_change_blocked('manual_verification')

    def test_regular_mode_updates_saved_query_in_shared_editor(self):
        with self.sessions() as db:
            plan, root = self.prepare(db)
            self.pause_search(db, plan, root)
            start_plan(db, {**self.config, 'queries': ['new ordinary query'], 'topic': 'any'})
            self.assertEqual(plan.config['five_video_draft']['queries'], ['new ordinary query'])
            self.assertEqual(plan.config['five_video_draft']['topic'], 'any')

    def test_saving_search_draft_does_not_rewrite_batch_query_or_start_it(self):
        with self.sessions() as db:
            plan, root = self.prepare(db)
            self.pause_search(db, plan, root)
            old = copy.deepcopy(plan.config)
            draft = old['five_video_draft']
            save_draft(db, draft['videos'], draft['account_ids'], queries=['saved recipe'], topic='any')
            self.assertFalse(plan.enabled)
            db.refresh(plan)
            self.assertEqual(plan.config['queries'], old['queries'])
            self.assertEqual(plan.config['five_video_batch'], old['five_video_batch'])
            self.assertEqual(plan.config['five_video_draft']['queries'], ['saved recipe'])
            self.assertEqual(len(db.scalars(select(Job)).all()), 1)


if __name__ == '__main__':
    unittest.main()
