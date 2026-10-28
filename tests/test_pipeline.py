"""Offline integration tests: real SQLite queue, stubbed external services."""
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
from app.models import Account, Clip, Job, JobType, RecordStatus, SourceVideo, YouTubePublication
from app.security import create_session, decrypt, encrypt
from app.services.publishing import enqueue_youtube
from app.services import publishing
from app.services.y2save import Y2SaveDownloader, Y2SaveError


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="clipforge-qa-")
        self.root = Path(self.directory.name)
        self.engine = create_engine(f"sqlite:///{self.root / 'test.db'}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.patches = [patch.object(worker, "SessionLocal", self.sessions),
                        patch.object(settings, "media_root", self.root),
                        patch.object(settings, "youtube_api_key", "offline-test-key"),
                        patch.object(publishing, "validate_shorts_file", return_value={"shorts_eligible": True}),
                        patch.object(worker, "check_session", return_value={"authenticated": True})]
        for item in self.patches:
            item.start()
        settings.ensure_directories()

        def test_db():
            with self.sessions() as db:
                yield db
        main.app.dependency_overrides[get_db] = test_db
        self.client = TestClient(main.app)
        self.client.cookies.set("clipforge_session", create_session(settings.admin_username))
        with self.sessions() as db:
            a = Account(platform="youtube", external_id="youtube:test", display_name="Test channel",
                        access_token=encrypt(json.dumps({"cookies": [{"name": "session", "value": "test-secret", "domain": ".youtube.com", "path": "/"}], "origins": []})),
                        refresh_token=encrypt(""), status=RecordStatus.ready)
            db.add(a)
            db.commit()
            self.account_id = a.id

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()
        self.directory.cleanup()

    def start(self):
        response = self.client.post("/api/pipeline/start", json={
            "query": "licensed gameplay", "max_results": 2, "clip_count": 1, "clip_duration": 5,
            "youtube_account_id": self.account_id, "visibility": "private", "consent": True,
        })
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["job_id"]

    def test_plain_cookie_export_and_secrets_not_returned(self):
        exported = [{"name": "LOGIN", "value": "secret-do-not-print", "domain": ".youtube.com", "path": "/", "sameSite": "no_restriction", "expirationDate": 1900000000}]
        r = self.client.post("/api/accounts/import", files={"file": ("cookies.json", json.dumps(exported).encode(), "application/json")})
        self.assertEqual(r.status_code, 200, r.text)
        with self.sessions() as db:
            a = db.scalar(select(Account).where(Account.external_id != "youtube:test"))
            self.assertNotIn("secret-do-not-print", a.access_token)
            self.assertEqual(json.loads(decrypt(a.access_token))["cookies"][0]["sameSite"], "None")
        self.assertNotIn("secret-do-not-print", self.client.get("/api/state").text)

    def test_tiktok_full_pipeline_uses_one_new_source_and_preserves_attribution(self):
        self.tiktok_pipeline()

    def tiktok_pipeline(self):
        from app.models import TikTokWebPublication
        with self.sessions() as db:
            account = Account(platform="tiktok_web", external_id="tt:fixture", display_name="TikTok fixture",
                access_token=encrypt('{"cookies":[]}'), refresh_token=encrypt(""), status=RecordStatus.ready)
            db.add(account)
            db.commit()
            aid = account.id
        options = {"query": "bread recipe", "max_results": 2, "clip_count": 1, "clip_duration": 5,
            "tiktok_web_account_id": aid, "visibility": "public", "consent": False, "fresh_source": True,
            "topic": "any"}
        self.assertEqual(self.client.post("/api/pipeline/start", json=options).status_code, 400)
        options["consent"] = True
        response = self.client.post("/api/pipeline/start", json=options)
        self.assertEqual(response.status_code, 202, response.text)
        pid = response.json()["job_id"]
        class Downloader:
            def download(self, url, destination):
                destination.write_bytes(b"downloaded fixture")
        def render(source, destination, start, duration):
            destination.write_bytes(f"clip {start} {duration}".encode())
        def upload(**kw):
            self.assertIn("Used with creator permission", kw["description"])
            self.assertNotIn("Creative Commons Attribution", kw["description"])
            self.assertEqual(kw["visibility"], "public")
            kw["on_progress"]("published", "https://www.tiktok.com/@fixture/video/1234567890123456789")
            return {"confirmed": True, "video_url": "https://www.tiktok.com/@fixture/video/1234567890123456789", "public_visibility_confirmed": True}
        results = [{"url": f"https://www.youtube.com/watch?v=fixture000{i}", "title": "Bread recipe", "channel": "Author"} for i in (1, 2)]
        with patch.object(worker, "search_youtube", return_value=results), patch.object(worker, "probe_duration", return_value=30), \
             patch.object(worker, "render_vertical_clip", side_effect=render), patch.object(worker, "upload_tiktok_web", side_effect=upload) as publisher:
            for _ in range(4):
                self.assertTrue(worker.run_once(Downloader()))
            self.assertFalse(worker.run_once(Downloader()))
            publisher.assert_called_once()
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(SourceVideo)).all()), 1)
            self.assertEqual(len(db.scalars(select(Clip)).all()), 1)
            jobs = db.scalars(select(Job)).all()
            self.assertEqual(sum(j.status == RecordStatus.error for j in jobs), 0)
            self.assertEqual(sum(j.status == RecordStatus.ready for j in jobs), 4)
            receipt = db.scalar(select(TikTokWebPublication))
            self.assertEqual(receipt.stage, "published")
            self.assertEqual(db.get(Job, receipt.job_id).payload["pipeline_id"], pid)

    def test_pipeline_requires_consent(self):
        r = self.client.post("/api/pipeline/start", json={"query": "test", "youtube_account_id": self.account_id, "consent": False})
        self.assertEqual(r.status_code, 400)
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(Job)).all()), 0)

    def test_direct_horizontal_upload_is_rendered_in_background_before_publishing(self):
        with patch.object(main, "probe_duration", return_value=20), patch.object(main, "probe_video_stream", return_value=(1920, 1080, True)):
            response = self.client.post("/api/youtube/direct-upload", data={"account_id": self.account_id, "consent": "true", "title": "My short"}, files={"file": ("landscape.mp4", b"fixture", "video/mp4")})
        self.assertEqual(response.status_code, 202, response.text)
        with self.sessions() as db:
            job = db.get(Job, response.json()["job_id"])
            self.assertEqual(job.type, JobType.render)
            self.assertEqual(job.payload["youtube_account_id"], self.account_id)
            self.assertEqual(job.payload["title"], "My short")
            self.assertEqual(db.scalars(select(YouTubePublication)).all(), [])

    def test_direct_long_video_is_not_silently_published_as_regular_video(self):
        with patch.object(main, "probe_duration", return_value=181):
            response = self.client.post("/api/youtube/direct-upload", data={"account_id": self.account_id, "consent": "true"}, files={"file": ("long.mp4", b"fixture", "video/mp4")})
        self.assertEqual(response.status_code, 400)
        with self.sessions() as db:
            self.assertEqual(db.scalars(select(Job)).all(), [])

    def test_full_queue_chain_and_duplicate_guard(self):
        pipeline_id = self.start()
        results = [{"url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "Licensed clip", "channel": "Author"}]

        class Downloader:
            progress_callback = None
            def download(self, url, destination):
                destination.write_bytes(b"source-fixture")
                self.progress_callback("conversion", 100)

        def render(source, destination, start, duration):
            destination.write_bytes(f"rendered:{start}:{duration}".encode())

        def upload(**options):
            self.assertEqual(options["visibility"], "private")
            self.assertIn("Source:", options["description"])
            options["on_progress"]("submitting", "https://www.youtube.com/watch?v=abcdefghijk")
            options["on_progress"]("published", "https://www.youtube.com/watch?v=abcdefghijk")
            return {"confirmed": True, "video_url": "https://www.youtube.com/watch?v=abcdefghijk"}

        with patch.object(worker, "search_youtube", return_value=results), \
             patch.object(worker, "probe_duration", return_value=20), \
             patch.object(worker, "render_vertical_clip", side_effect=render), \
             patch.object(worker, "upload_youtube", side_effect=upload):
            for _ in range(4):
                self.assertTrue(worker.run_once(Downloader()))
            self.assertFalse(worker.run_once(Downloader()))
        with self.sessions() as db:
            jobs = db.scalars(select(Job)).all()
            self.assertEqual(len(jobs), 4)
            self.assertTrue(all(j.status == RecordStatus.ready for j in jobs))
            publication = db.scalar(select(YouTubePublication))
            self.assertEqual(publication.stage, "published")
            upload_job = db.get(Job, publication.job_id)
            self.assertEqual(upload_job.payload["pipeline_id"], pipeline_id)
            clip = db.get(Clip, upload_job.payload["clip_id"])
            with self.assertRaisesRegex(ValueError, "уже отправлялся"):
                enqueue_youtube(db, account_id=self.account_id, clip=clip)

    def test_existing_download_failure_is_not_silently_skipped(self):
        with self.sessions() as db:
            source = SourceVideo(source_url="https://www.youtube.com/watch?v=abcdefghijk", title="old", rights_confirmed=True, status=RecordStatus.error)
            db.add(source)
            db.commit()
            source_id = source.id
        pipeline_id = self.start()
        with patch.object(worker, "search_youtube", return_value=[{"url": "https://www.youtube.com/watch?v=abcdefghijk", "title": "title", "channel": "author"}]):
            worker.run_once(None)
        with self.sessions() as db:
            download = db.scalar(select(Job).where(Job.type == JobType.download))
            self.assertEqual(download.payload["source_id"], source_id)
            self.assertEqual(download.payload["pipeline_id"], pipeline_id)

    def test_permission_mode_reused_search_result_has_no_false_cc_claim(self):
        path = self.root / 'cached-recipe.mp4'
        path.write_bytes(b'fixture-video')
        with self.sessions() as db:
            source = SourceVideo(source_url='https://www.youtube.com/watch?v=recipe00001', title='Bread recipe',
                                 rights_confirmed=True, status=RecordStatus.ready, local_path=str(path))
            db.add(source); db.commit()
        pid = self.start()
        with self.sessions() as db:
            job = db.get(Job, pid)
            with patch.object(worker, 'search_youtube', return_value=[{
                    'url': source.source_url, 'title': 'Bread recipe', 'channel': 'Cooking fixture', 'rights_basis': 'author_permission'}]):
                result = worker.process_discovery(job)
        with self.sessions() as db:
            render = db.scalar(select(Job).where(Job.type == JobType.render))
            self.assertEqual(result['rights_basis'], 'author_permission')
            self.assertEqual(render.payload['rights_basis'], 'author_permission')
            self.assertIn('Used with creator permission', render.payload['caption'])
            self.assertNotIn('Creative Commons', render.payload['caption'])
            self.assertEqual(db.scalars(select(Job).where(Job.type == JobType.download)).all(), [])

    def test_legacy_download_preserves_original_cc_attribution(self):
        with self.sessions() as db:
            source = SourceVideo(source_url='https://www.youtube.com/watch?v=legacy00001', title='Legacy CC source', rights_confirmed=True)
            db.add(source); db.flush()
            job = Job(type=JobType.download, payload={'source_id': source.id, 'auto_render': {'count': 1, 'duration': 5}})
            db.add(job); db.commit()
        class Downloader:
            def download(self, url, destination):
                destination.write_bytes(b'fixture-video')
        with patch.object(worker, 'probe_duration', return_value=20):
            worker.process_download(job, Downloader())
        with self.sessions() as db:
            render = db.scalar(select(Job).where(Job.type == JobType.render))
            self.assertIn('Creative Commons Attribution', render.payload['caption'])

    def test_global_permission_mode_is_visible_without_starting_jobs(self):
        response = self.client.get('/api/state')
        self.assertEqual(response.json()['search_rights'], {'mode': 'author_permission', 'label': 'Есть разрешение автора'})
        with self.sessions() as db:
            self.assertEqual(db.scalars(select(Job)).all(), [])

    def test_uncertain_upload_never_auto_or_manual_retries(self):
        with self.sessions() as db:
            source = SourceVideo(source_url="local", rights_confirmed=True, status=RecordStatus.ready)
            db.add(source)
            db.flush()
            p = self.root / "ready.mp4"
            p.write_bytes(b"fixture")
            clip = Clip(source_id=source.id, local_path=str(p), start_seconds=0, duration_seconds=5, status=RecordStatus.ready)
            db.add(clip)
            db.flush()
            j = enqueue_youtube(db, account_id=self.account_id, clip=clip)
            db.commit()
            jid = j.id
        job = worker.claim_job()
        self.assertEqual(job.id, jid)
        self.assertIsNone(worker.claim_job())
        with self.sessions() as db:
            publication = db.scalar(select(YouTubePublication))
            publication.stage = "submitting"
            db.commit()
        worker.fail_job(jid, "Lost confirmation")
        with self.sessions() as db:
            self.assertEqual(db.get(Job, jid).status, RecordStatus.error)
        self.assertEqual(self.client.post(f"/api/jobs/{jid}/retry").status_code, 409)

    def test_empty_search_has_clear_terminal_error(self):
        jid = self.start()
        with patch.object(worker, "search_youtube", return_value=[]):
            worker.run_once(None)
        with self.sessions() as db:
            job = db.get(Job, jid)
            self.assertEqual(job.status, RecordStatus.error)
            self.assertIn("не нашёл", job.error)
            self.assertIsNone(job.next_attempt_at)


if __name__ == "__main__":
    unittest.main()
