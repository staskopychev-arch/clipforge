import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main, worker
from app.config import settings
from app.db import Base, get_db
from app.models import Account, Clip, Job, JobType, RecordStatus, SourceVideo, TikTokWebPublication
from app.security import create_session, decrypt, encrypt
from app.services import publishing
from app.services.publishing import enqueue_tiktok_web
from app.services.tiktok_web import TikTokWebPolicyError, TikTokWebSessionError, canonical_video_url


class TikTokWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.engine = create_engine(f"sqlite:///{self.root / 'test.db'}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.patches = [patch.object(worker, "SessionLocal", self.sessions), patch.object(settings, "media_root", self.root),
            patch.object(publishing, "validate_shorts_file", return_value={"shorts_eligible": True})]
        for p in self.patches:
            p.start()
        settings.ensure_directories()
        def test_db():
            with self.sessions() as db:
                yield db
        main.app.dependency_overrides[get_db] = test_db
        self.client = TestClient(main.app)
        self.client.cookies.set("clipforge_session", create_session(settings.admin_username))
        with self.sessions() as db:
            account = Account(platform="tiktok_web", external_id="fixture:tiktok", display_name="Fixture owned channel",
                access_token=encrypt('{"cookies":[]}'), refresh_token=encrypt(""), status=RecordStatus.ready)
            source = SourceVideo(source_url="local-fixture", rights_confirmed=True, status=RecordStatus.ready)
            db.add_all([account, source])
            db.flush()
            path = settings.clips_dir / "fixture.mp4"
            path.write_bytes(b"LOCAL_FIXTURE_NOT_A_REAL_UPLOAD")
            clip = Clip(source_id=source.id, local_path=str(path), start_seconds=0, duration_seconds=20,
                caption="Fixture only", status=RecordStatus.ready)
            db.add(clip)
            db.commit()
            self.account_id, self.clip_id = account.id, clip.id

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        for p in reversed(self.patches):
            p.stop()
        self.engine.dispose()
        self.directory.cleanup()

    def enqueue(self):
        with self.sessions() as db:
            job = enqueue_tiktok_web(db, account_id=self.account_id, clip=db.get(Clip, self.clip_id), description="Fixture caption", visibility="private")
            db.commit()
            return job

    def test_cookie_import_detects_platform_encrypts_and_requires_check(self):
        raw = [{"name": "sessionid", "value": "DO_NOT_EXPOSE", "domain": ".tiktok.com", "sameSite": "no_restriction"}]
        response = self.client.post("/api/accounts/import", files={"file": ("tt.json", json.dumps(raw))})
        self.assertEqual(response.status_code, 200, response.text)
        with self.sessions() as db:
            account = db.scalar(select(Account).where(Account.external_id != "fixture:tiktok"))
            self.assertEqual(account.platform, "tiktok_web")
            self.assertEqual(account.status, RecordStatus.reauth_required)
            self.assertNotIn("DO_NOT_EXPOSE", account.access_token)
            self.assertEqual(json.loads(decrypt(account.access_token))["cookies"][0]["sameSite"], "None")
        self.assertNotIn("DO_NOT_EXPOSE", self.client.get("/api/state").text)

    def test_mixed_cookies_and_false_platform_are_rejected_without_secrets(self):
        for domains in ((".tiktok.com", ".google.com"), ("tiktok.com.attacker.test",)):
            raw = {"accounts": [{"platform": "tiktok_web", "cookies": [{"name": "sid", "value": "SECRET", "domain": d} for d in domains]}]}
            response = self.client.post("/api/accounts/import", files={"file": ("a.json", json.dumps(raw))})
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("SECRET", response.text)

    def test_tiktok_check_does_not_use_oauth_or_youtube(self):
        with patch.object(main, "check_tiktok_web_session", return_value={"authenticated": True}) as check, patch.object(main, "check_session") as youtube:
            response = self.client.post(f"/api/accounts/{self.account_id}/check-session")
        self.assertEqual(response.status_code, 200)
        check.assert_called_once()
        youtube.assert_not_called()

    def test_check_challenge_marks_reauth(self):
        with patch.object(main, "check_tiktok_web_session", side_effect=TikTokWebSessionError("Manual CAPTCHA required")):
            response = self.client.post(f"/api/accounts/{self.account_id}/check-session")
        self.assertEqual(response.status_code, 409)
        with self.sessions() as db:
            self.assertEqual(db.get(Account, self.account_id).status, RecordStatus.reauth_required)

    def test_policy_check_marks_error_not_invalid_login(self):
        with patch.object(main, "check_tiktok_web_session", side_effect=TikTokWebPolicyError("30522")):
            response = self.client.post(f"/api/accounts/{self.account_id}/check-session")
        self.assertEqual(response.status_code, 409)
        with self.sessions() as db:
            self.assertEqual(db.get(Account, self.account_id).status, RecordStatus.error)

    def test_policy_during_worker_upload_preserves_receipt_and_never_retries(self):
        job = self.enqueue()
        def rejected(**kwargs):
            kwargs["on_progress"]("uploading", None)
            raise TikTokWebPolicyError("30522")
        with patch.object(worker, "upload_tiktok_web", side_effect=rejected):
            with self.assertRaises(TikTokWebPolicyError):
                worker.process_tiktok_web_publish(job)
        worker.fail_job(job.id, "30522", error_kind="platform_policy")
        with self.sessions() as db:
            row = db.get(Job, job.id)
            self.assertEqual(db.get(Account, self.account_id).status, RecordStatus.error)
            self.assertEqual(row.status, RecordStatus.error)
            self.assertEqual(row.result["platform_error_code"], 30522)
            self.assertIsNone(row.next_attempt_at)
            self.assertEqual(db.scalar(select(TikTokWebPublication)).stage, "uploading")
        self.assertEqual(self.client.post(f"/api/jobs/{job.id}/retry").status_code, 409)

    def test_explicit_consent_required_and_durable_reservation_created(self):
        payload = {"account_id": self.account_id, "clip_id": self.clip_id, "description": "Fixture caption"}
        self.assertEqual(self.client.post("/api/tiktok-web/publish", json=payload).status_code, 400)
        response = self.client.post("/api/tiktok-web/publish", json={**payload, "consent": True})
        self.assertEqual(response.status_code, 202, response.text)
        with self.sessions() as db:
            job = db.get(Job, response.json()["job_id"])
            self.assertEqual(job.type, JobType.tiktok_web_publish)
            self.assertEqual(db.scalar(select(TikTokWebPublication)).stage, "reserved")

    def test_duplicate_file_rejected_and_uncertain_retry_blocked(self):
        job = self.enqueue()
        with self.assertRaises(ValueError):
            self.enqueue()
        with self.sessions() as db:
            db.get(Job, job.id).status = RecordStatus.error
            db.scalar(select(TikTokWebPublication)).stage = "submitting"
            db.commit()
        self.assertEqual(self.client.post(f"/api/jobs/{job.id}/retry").status_code, 409)

    def test_worker_ui_upload_and_receipt_before_post(self):
        job = self.enqueue()
        def ui_upload(**kwargs):
            kwargs["on_progress"]("uploading", None)
            kwargs["on_progress"]("submitting", None)
            with self.sessions() as db:
                self.assertEqual(db.scalar(select(TikTokWebPublication)).stage, "submitting")
            url = "https://www.tiktok.com/@fixture/video/1234567890123456789"
            kwargs["on_progress"]("published", url)
            return {"confirmed": True, "video_url": url}
        with patch.object(worker, "upload_tiktok_web", side_effect=ui_upload) as upload:
            result = worker.process_tiktok_web_publish(job)
            self.assertTrue(result["confirmed"])
            self.assertTrue(worker.process_tiktok_web_publish(job)["duplicate_skipped"])
        upload.assert_called_once()

    def test_failure_never_auto_retries_and_stale_upload_not_resubmitted(self):
        job = self.enqueue()
        worker.fail_job(job.id, "Unknown result")
        with self.sessions() as db:
            record = db.get(Job, job.id)
            self.assertEqual(record.status, RecordStatus.error)
            self.assertIsNone(record.next_attempt_at)
            record.status, record.started_at = RecordStatus.processing, None
            db.commit()
        worker.recover_stale_jobs()
        with self.sessions() as db:
            self.assertEqual(db.get(Job, job.id).status, RecordStatus.error)

    def test_expired_session_has_structured_reason_and_no_auto_retry(self):
        job=self.enqueue()
        with patch.object(worker,'upload_tiktok_web',side_effect=worker.TikTokWebExpiredSessionError('Session expired')):
            self.assertTrue(worker.run_once(None))
        with self.sessions() as db:
            row=db.get(Job,job.id)
            self.assertEqual(row.status,RecordStatus.reauth_required)
            self.assertEqual(row.result['error_kind'],'session_expired')
            self.assertIsNone(row.next_attempt_at)
            self.assertEqual(db.scalar(select(TikTokWebPublication)).stage,'reserved')

    def test_direct_horizontal_upload_renders_before_tiktok(self):
        with patch.object(main, "probe_duration", return_value=20), patch.object(main, "probe_video_stream", return_value=(1920, 1080, True)):
            response = self.client.post("/api/tiktok-web/direct-upload", data={"account_id": self.account_id, "description": "Fixture", "consent": "true"}, files={"file": ("fixture.mp4", b"fixture", "video/mp4")})
        self.assertEqual(response.status_code, 202, response.text)
        with self.sessions() as db:
            job = db.get(Job, response.json()["job_id"])
            self.assertEqual(job.type, JobType.render)
            self.assertEqual(job.payload["tiktok_web_account_id"], self.account_id)
            self.assertNotIn("youtube_account_id", job.payload)

    def test_existing_and_new_panels_preserved(self):
        page = self.client.get("/").text
        for element in ("automationForm", "automationAccountStatus", "ttCookieDialog", "ytDirectForm", "accountImportForm", "loginQueuePanel"):
            self.assertIn(f'id="{element}"', page)
        for element in ('ttDirectForm','ttCheckSession','tiktokWebPanel','publishForm'):
            self.assertNotIn(f'id="{element}"',page)

    def test_receipt_url_allows_only_canonical_tiktok_videos(self):
        self.assertEqual(canonical_video_url("https://www.tiktok.com/@fixture/video/1234567890123456789?x=y"), "https://www.tiktok.com/@fixture/video/1234567890123456789")
        for value in ("https://www.tiktok.com.attacker.test/@fixture/video/1234567890123456789", "https://www.tiktok.com/@fixture", "http://www.tiktok.com/@fixture/video/1234567890123456789"):
            self.assertIsNone(canonical_video_url(value))
