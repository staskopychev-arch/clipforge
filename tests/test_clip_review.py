import copy
import unittest
from contextlib import ExitStack
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import select
from app import main
from app.db import get_db
from app.models import AutomationPlan, Clip, Job, JobType, RecordStatus
from app.config import settings
from app.security import create_session
from app.services.clip_review import review_clip
from app.services.five_videos import save_draft, start_batch
from app.services.scheduler import schedule_tick
import test_five_videos as baseline


class ClipReviewTests(unittest.TestCase):
    setUp = baseline.FiveVideoTests.setUp
    tearDown = baseline.FiveVideoTests.tearDown
    draft = baseline.FiveVideoTests.draft

    def prepare(self, db):
        self.videos, self.accounts = self.draft(db, amount=3)
        self.options = dict(interval_minutes=3, daily_limit=0, clip_duration=15,
                            queries=['bread recipe'], topic='any', visibility='public')
        start_batch(db, **self.options)
        plan = db.get(AutomationPlan, 1)
        return plan, self.videos[0]['clip_id']

    def test_no_upload_reservation_without_review_even_for_legacy_batch(self):
        with self.sessions() as db:
            plan, cid = self.prepare(db)
            self.assertFalse(schedule_tick(db))
            self.assertFalse(plan.enabled)
            self.assertEqual(plan.status, 'review_required')
            self.assertEqual(plan.config['five_video_batch']['position'], 0)
            self.assertEqual(db.scalars(select(Job)).all(), [])

    def test_approval_does_not_start_plan_and_applies_only_to_actual_file(self):
        with self.sessions() as db:
            plan, cid = self.prepare(db)
            schedule_tick(db)
            batch_id = plan.config['five_video_batch']['id']
            with self.assertRaises(ValueError):
                review_clip(db, batch_id=batch_id, clip_id=cid, decision='approved')
            review_clip(db, batch_id=batch_id, clip_id=cid, decision='approved', watched=True)
            db.refresh(plan)
            self.assertFalse(plan.enabled)
            self.assertEqual(db.scalars(select(Job)).all(), [])
            start_batch(db, **self.options)
            self.assertTrue(schedule_tick(db))
            self.assertEqual(db.get(Job, plan.last_pipeline_id).payload['clip_id'], cid)

    def test_changed_file_cannot_reuse_approval(self):
        with self.sessions() as db:
            plan, cid = self.prepare(db)
            schedule_tick(db)
            review_clip(db, batch_id=plan.config['five_video_batch']['id'], clip_id=cid, decision='approved', watched=True)
            Path(db.get(Clip, cid).local_path).write_bytes(b'changed-video')
            start_batch(db, **self.options)
            self.assertFalse(schedule_tick(db))
            self.assertEqual(db.scalars(select(Job)).all(), [])

    def test_rejection_keeps_history_and_excludes_source_from_new_search(self):
        with self.sessions() as db:
            plan, cid = self.prepare(db)
            schedule_tick(db)
            old_id = plan.config['five_video_batch']['id']
            review_clip(db, batch_id=old_id, clip_id=cid, decision='rejected')
            db.refresh(plan)
            self.assertFalse(plan.enabled)
            self.assertEqual(plan.config['five_video_batch']['status'], 'rejected')
            with self.assertRaises(ValueError):
                start_batch(db, **self.options)
            db.rollback(); db.refresh(plan)
            for video in self.videos:
                video['clip_id'] = None
            save_draft(db, self.videos, self.accounts)
            start_batch(db, **self.options)
            self.assertNotEqual(plan.config['five_video_batch']['id'], old_id)
            self.assertEqual(plan.config['five_video_batch_history'][-1]['id'], old_id)
            self.assertTrue(schedule_tick(db))
            root = db.get(Job, plan.last_pipeline_id)
            self.assertEqual(root.type, JobType.discovery)
            self.assertIn('https://youtu.be/five-source-0', root.payload['exclude_source_urls'])
            self.assertIsNotNone(db.get(Clip, cid))

    def test_unknown_pending_submission_remains_blocked_after_rejection(self):
        with self.sessions() as db:
            plan, cid = self.prepare(db)
            pending = Job(type=JobType.tiktok_web_publish, status=RecordStatus.error, payload={'account_id': self.accounts[0], 'clip_id': cid})
            db.add(pending); db.flush()
            b = copy.deepcopy(plan.config['five_video_batch']); b['pending_job_id'] = pending.id
            plan.config = {**plan.config, 'five_video_batch': b}; db.commit()
            review_clip(db, batch_id=b['id'], clip_id=cid, decision='rejected')
            db.refresh(plan)
            before = copy.deepcopy(plan.config)
            with self.assertRaises(ValueError):
                start_batch(db, **self.options)
            self.assertEqual(plan.config, before)
            self.assertEqual(db.get(Job, pending.id).status, RecordStatus.error)

    def test_stale_review_or_active_upload_cannot_change_state(self):
        with self.sessions() as db:
            plan, cid = self.prepare(db)
            batch = plan.config['five_video_batch']
            with self.assertRaises(ValueError):
                review_clip(db, batch_id='old-id', clip_id=cid, decision='approved', watched=True)
            pending = Job(type=JobType.tiktok_web_publish, status=RecordStatus.processing, payload={})
            db.add(pending); db.flush()
            plan.config = {**plan.config, 'five_video_batch': {**batch, 'pending_job_id': pending.id}}
            db.commit()
            before = copy.deepcopy(plan.config)
            with self.assertRaises(ValueError):
                review_clip(db, batch_id=batch['id'], clip_id=cid, decision='rejected')
            self.assertEqual(plan.config, before)

    def test_preview_requires_login_supports_ranges_and_refuses_outside_files(self):
        with self.sessions() as db:
            plan, cid = self.prepare(db)
            clip = db.get(Clip, cid)
            safe = settings.clips_dir / 'review-fixture.mp4'
            safe.write_bytes(b'0123456789abcdef')
            clip.local_path = str(safe); db.commit()

        def override():
            with self.sessions() as db:
                yield db
        main.app.dependency_overrides[get_db] = override
        try:
            with ExitStack() as resources:
                client = TestClient(main.app)
                resources.callback(client.close)
                self.assertEqual(client.get(f'/api/clips/{cid}/preview').status_code, 401)
                client.cookies.set('clipforge_session', create_session(settings.admin_username))
                response = client.get(f'/api/clips/{cid}/preview', headers={'Range': 'bytes=0-3'})
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response.content, b'0123')
                with self.sessions() as db:
                    db.get(Clip, cid).local_path = str(self.root / 'five-0.mp4'); db.commit()
                self.assertEqual(client.get(f'/api/clips/{cid}/preview').status_code, 404)
        finally:
            main.app.dependency_overrides.clear()


if __name__ == '__main__':
    unittest.main()
