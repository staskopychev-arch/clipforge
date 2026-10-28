import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main
from app.config import settings
from app.db import Base, get_db
from app.models import Account, AutomationPlan, Clip, Job, JobType, RecordStatus, SourceVideo, YouTubePublication, utcnow
from app.security import create_session
from app.services.scheduler import schedule_tick, start_plan, stop_plan


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.directory.name) / 'test.db'}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.client = TestClient(main.app)
        self.client.cookies.set("clipforge_session", create_session(settings.admin_username))
        def get_test_db():
            with self.sessions() as db:
                yield db
        main.app.dependency_overrides[get_db] = get_test_db
        self.key_patch = patch.object(settings, "youtube_api_key", "test-key")
        self.key_patch.start()
        self.config = {"queries": ["licensed gameplay"], "account_ids": [], "publish": False,
                       "interval_minutes": 60, "daily_limit": 1, "clip_count": 1, "clip_duration": 20, "visibility": "private"}

    def tearDown(self):
        self.key_patch.stop()
        self.client.close()
        main.app.dependency_overrides.clear()
        self.engine.dispose()
        self.directory.cleanup()

    def test_defaults_never_start_schedule(self):
        with self.sessions() as db:
            self.assertFalse(schedule_tick(db))
            self.assertEqual(db.scalars(select(Job)).all(), [])
        self.assertFalse(self.client.get("/api/automation").json()["enabled"])

    def test_preview_schedule_no_publish_and_no_overlapping_cycles(self):
        response = self.client.post("/api/automation/start", json=self.config)
        self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            self.assertTrue(schedule_tick(db))
            self.assertFalse(schedule_tick(db))
            job = db.scalar(select(Job))
            self.assertIsNone(job.payload["youtube_account_id"])
            self.assertEqual(job.payload["automation_plan_id"], 1)
            self.assertEqual(job.type, JobType.discovery)
            job.status = RecordStatus.ready
            db.commit()
            self.assertFalse(schedule_tick(db))  # interval persists between sessions
        with self.sessions() as db:
            plan = db.get(AutomationPlan, 1)
            plan.next_run_at = utcnow() - timedelta(seconds=1)
            db.commit()
            self.assertTrue(schedule_tick(db))
            self.assertEqual(len(db.scalars(select(Job)).all()), 2)

    def test_any_cycle_failure_pauses_and_does_not_select_another_account(self):
        with self.sessions() as db:
            start_plan(db, self.config)
            schedule_tick(db)
            job = db.scalar(select(Job))
            job.status = RecordStatus.reauth_required
            job.error = "Manual login required"
            db.commit()
            self.assertFalse(schedule_tick(db))
            self.assertFalse(db.get(AutomationPlan, 1).enabled)
            self.assertEqual(db.get(AutomationPlan, 1).status, "paused")
            self.assertEqual(len(db.scalars(select(Job)).all()), 1)

    def test_terminal_discovery_failure_pauses_without_another_cycle(self):
        with self.sessions() as db:
            start_plan(db, self.config)
            schedule_tick(db)
            job = db.scalar(select(Job))
            job.status = RecordStatus.error
            job.error = "Temporary network failure"
            db.commit()
            self.assertFalse(schedule_tick(db))
            self.assertFalse(db.get(AutomationPlan, 1).enabled)
            self.assertEqual(db.get(AutomationPlan, 1).status, "paused")
            self.assertFalse(schedule_tick(db))
            self.assertEqual(len(db.scalars(select(Job)).all()), 1)

    def test_downloader_security_failure_pauses_instead_of_retrying(self):
        with self.sessions() as db:
            start_plan(db, self.config)
            schedule_tick(db)
            job = db.scalar(select(Job))
            job.status = RecordStatus.error
            job.result = {"error_kind": "manual_verification"}
            job.error = "CAPTCHA"
            db.commit()
            self.assertFalse(schedule_tick(db))
            self.assertFalse(db.get(AutomationPlan, 1).enabled)

    def test_explicit_channel_and_consent_required(self):
        request = {**self.config, "publish": True}
        self.assertEqual(self.client.post("/api/automation/start", json=request).status_code, 400)
        request.update(account_ids=["invalid"], consent=True, owns_channels=True)
        self.assertEqual(self.client.post("/api/automation/start", json=request).status_code, 409)

    def test_invalid_session_cannot_start_publishing(self):
        with self.sessions() as db:
            account = Account(platform="youtube", external_id="test", access_token="encrypted-fixture", refresh_token="", status=RecordStatus.reauth_required)
            db.add(account)
            db.commit()
            account_id = account.id
        request = {**self.config, "publish": True, "account_ids": [account_id], "consent": True, "owns_channels": True}
        self.assertEqual(self.client.post("/api/automation/start", json=request).status_code, 409)

    def test_stop_cancels_only_waiting_plan_jobs(self):
        with self.sessions() as db:
            start_plan(db, self.config)
            schedule_tick(db)
            discovery = db.scalar(select(Job))
            unrelated = Job(type=JobType.render, payload={"source_id": "manual"})
            db.add(unrelated)
            db.commit()
            stop_plan(db)
            self.assertEqual(db.get(Job, discovery.id).status, RecordStatus.error)
            self.assertEqual(db.get(Job, unrelated.id).status, RecordStatus.queued)
            self.assertFalse(schedule_tick(db))

    def test_invalid_schedule_values_rejected(self):
        for settings_override in ({"interval_minutes": 0}, {"daily_limit": -1}, {"queries": [" "]}):
            self.assertEqual(self.client.post("/api/automation/start", json={**self.config, **settings_override}).status_code, 422)

    def test_interval_below_an_hour_and_optional_quota_are_accepted(self):
        for interval, limit in [(1, 0), (15, 100), (2880, 0)]:
            with self.subTest(interval=interval, limit=limit):
                response=self.client.post("/api/automation/start",json={**self.config,"interval_minutes":interval,"daily_limit":limit})
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['config']['interval_minutes'],interval)
                self.client.post('/api/automation/stop')

    def test_content_edit_preserves_running_queue_and_only_changes_future_cycles(self):
        self.client.post('/api/automation/start',json={**self.config,'caption':'Previous caption','hashtags':'#previous'})
        with self.sessions() as db:
            self.assertTrue(schedule_tick(db))
            plan=db.get(AutomationPlan,1)
            old_state=(plan.enabled,plan.status,plan.next_run_at,plan.last_pipeline_id)
            plan.config={**plan.config,'account_index':2}
            db.commit()
            original_payload=dict(db.get(Job,plan.last_pipeline_id).payload)
        response=self.client.patch('/api/automation/content',json={'caption':' New caption ','hashtags':' #new '})
        self.assertEqual(response.status_code,200,response.text)
        with self.sessions() as db:
            plan=db.get(AutomationPlan,1)
            self.assertEqual((plan.enabled,plan.status,plan.next_run_at,plan.last_pipeline_id),old_state)
            self.assertEqual(plan.config['account_index'],2)
            self.assertEqual(plan.config['query_index'],1)
            self.assertEqual(plan.config['caption'],'New caption')
            self.assertEqual(plan.config['hashtags'],'#new')
            self.assertEqual(db.get(Job,plan.last_pipeline_id).payload,original_payload)
            self.assertEqual(len(db.scalars(select(Job)).all()),1)
            db.get(Job,plan.last_pipeline_id).status=RecordStatus.ready
            plan.next_run_at=utcnow()-timedelta(seconds=1)
            db.commit()
            self.assertTrue(schedule_tick(db))
            self.assertEqual(db.get(Job,plan.last_pipeline_id).payload['caption'],'New caption')
            self.assertEqual(db.get(Job,plan.last_pipeline_id).payload['hashtags'],'#new')

    def test_content_save_does_not_resume_paused_plan_or_allow_control_fields(self):
        with self.sessions() as db:
            start_plan(db,self.config)
            plan=db.get(AutomationPlan,1)
            plan.enabled=False; plan.status='paused'; plan.last_error='Login required'
            db.commit()
        self.assertEqual(self.client.patch('/api/automation/content',json={'caption':'','hashtags':'','enabled':True}).status_code,422)
        response=self.client.patch('/api/automation/content',json={'caption':'','hashtags':''})
        self.assertEqual(response.status_code,200)
        self.assertFalse(response.json()['enabled'])
        self.assertEqual(response.json()['status'],'paused')
        self.assertEqual(response.json()['last_error'],'Login required')
        self.assertEqual(self.client.patch('/api/automation/content',json={'caption':'x'*1201,'hashtags':''}).status_code,422)
        self.client.cookies.clear()
        self.assertEqual(self.client.patch('/api/automation/content',json={'caption':'','hashtags':''}).status_code,401)

    def test_quota_counts_uncertain_reservations_and_resumes_after_24h(self):
        with self.sessions() as db:
            account = Account(platform="youtube", external_id="owned", access_token="fixture", refresh_token="", status=RecordStatus.ready)
            source = SourceVideo(source_url="https://youtu.be/abcdefghijk", rights_confirmed=True)
            db.add_all([account, source])
            db.flush()
            clip = Clip(source_id=source.id, start_seconds=0, duration_seconds=20)
            db.add(clip)
            db.flush()
            upload = Job(type=JobType.youtube_publish, payload={"account_id": account.id, "clip_id": clip.id}, status=RecordStatus.error)
            db.add(upload)
            db.flush()
            receipt = YouTubePublication(account_id=account.id, job_id=upload.id, file_hash="reserved-fixture", stage="submitting")
            db.add(receipt)
            db.commit()
            start_plan(db, {**self.config, "account_ids": [account.id], "publish": True})
            self.assertFalse(schedule_tick(db))
            self.assertEqual(db.get(AutomationPlan, 1).status, "quota_wait")
            receipt.created_at = utcnow() - timedelta(hours=25)
            db.get(AutomationPlan, 1).next_run_at = utcnow() - timedelta(seconds=1)
            db.commit()
            self.assertTrue(schedule_tick(db))
            discovery = db.scalar(select(Job).where(Job.type == JobType.discovery))
            self.assertIn(source.source_url, discovery.payload["exclude_source_urls"])
