"""Offline release tests: real schemas, transfer archives, guards; no uploads."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch, Mock, call

import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import main
from app.config import Settings, settings
from app.db import Base, get_db
from app.models import Account, SourceVideo, Clip, Job, JobType, RecordStatus, YouTubePublication
from app.security import encrypt, verify_password, create_session
from app.deployment_security import LoginLimiter, same_origin
from deploy.data_transfer import database_connection, export_data, restore_data
from deploy.init_secrets import initialize
from deploy.runtime import prepare_data_directory

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_bootstrap_chmods_before_dropping_directory_ownership(self):
        path = Mock()
        path.is_symlink.return_value = False
        path.stat.return_value.st_mode = 0o40755
        operations = Mock()
        operations.attach_mock(path.chmod, "chmod")
        with patch("deploy.runtime.os.chown", create=True) as chown:
            operations.attach_mock(chown, "chown")
            prepare_data_directory(path)
        self.assertEqual(operations.mock_calls, [call.chown(path, 0, 0), call.chmod(0o700), call.chown(path, 10001, 10001)])

    def test_bootstrap_does_not_chmod_already_owned_private_directory(self):
        path = Mock()
        path.is_symlink.return_value = False
        path.stat.return_value.st_mode = 0o40700
        with patch("deploy.runtime.os.chown", create=True) as chown:
            prepare_data_directory(path)
        path.chmod.assert_not_called()
        chown.assert_called_once_with(path, 10001, 10001)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clipforge-deploy-test-")
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.database = self.media / "clipforge.db"
        self.engine = create_engine(f"sqlite:///{self.database}")
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            account = Account(platform="youtube", external_id="test", access_token=encrypt("test-token"), refresh_token=encrypt(""))
            source = SourceVideo(source_url="local", rights_confirmed=True, status=RecordStatus.ready)
            db.add_all([account, source])
            db.flush()
            (self.media / "sources").mkdir()
            (self.media / "clips").mkdir()
            source.local_path = str(self.media / "sources" / f"{source.id}.mp4")
            Path(source.local_path).write_bytes(b"source-fixture")
            clip = Clip(source_id=source.id, start_seconds=0, duration_seconds=5, status=RecordStatus.ready)
            db.add(clip)
            db.flush()
            clip.local_path = str(self.media / "clips" / f"{clip.id}.mp4")
            Path(clip.local_path).write_bytes(b"clip-fixture")
            job = Job(type=JobType.youtube_publish, status=RecordStatus.ready, payload={"account_id":account.id, "clip_id":clip.id})
            db.add(job)
            db.flush()
            db.add(YouTubePublication(account_id=account.id, job_id=job.id, file_hash="test-hash", stage="published", video_url="https://youtu.be/abcdefghijk"))
            db.commit()
            self.job_id, self.clip_id = job.id, clip.id

    def tearDown(self):
        main.app.dependency_overrides.clear()
        self.engine.dispose()
        self.temporary.cleanup()

    def test_roundtrip_preserves_receipt_and_rebases_paths(self):
        archive = self.root / "private.zip"
        self.assertEqual(export_data(self.database, self.media, archive), 3)
        destination = self.root / "restored"
        self.assertEqual(restore_data(archive, destination), 3)
        with database_connection(destination / "clipforge.db") as db:
            self.assertEqual(db.execute("SELECT stage FROM youtube_publications").fetchone()[0], "published")
            local_path = db.execute("SELECT local_path FROM clips").fetchone()[0]
            self.assertEqual(Path(local_path), destination / "clips" / f"{self.clip_id}.mp4")
            self.assertEqual(Path(local_path).read_bytes(), b"clip-fixture")
        with database_connection(self.database) as db:
            self.assertEqual(Path(db.execute("SELECT local_path FROM clips").fetchone()[0]).parent, self.media / "clips")

    def test_export_refuses_pending_jobs(self):
        with database_connection(self.database) as db:
            db.execute("UPDATE jobs SET status='processing'")
        with self.assertRaisesRegex(RuntimeError, "pending jobs"):
            export_data(self.database, self.media, self.root / "backup.zip")

    def test_backup_preserves_noncanonical_direct_upload_paths(self):
        nested = self.media / "sources" / "youtube_direct_uploads"
        nested.mkdir()
        original = nested / "random-upload.mp4"
        original.write_bytes(b"direct-upload-fixture")
        with database_connection(self.database) as db:
            db.execute("UPDATE source_videos SET local_path=?", (str(original),))
        archive = self.root / "direct-backup.zip"
        export_data(self.database, self.media, archive)
        restored = self.root / "restored-direct"
        restore_data(archive, restored)
        with database_connection(restored / "clipforge.db") as db:
            stored = Path(db.execute("SELECT local_path FROM source_videos").fetchone()[0])
        self.assertEqual(stored.read_bytes(), b"direct-upload-fixture")
        self.assertEqual(original.read_bytes(), b"direct-upload-fixture")

    def test_backup_rejects_referenced_files_outside_media_roots(self):
        outside = self.root / "outside.mp4"
        outside.write_bytes(b"must-not-export")
        with database_connection(self.database) as db:
            db.execute("UPDATE source_videos SET local_path=?", (str(outside),))
        with self.assertRaisesRegex(ValueError, "outside"):
            export_data(self.database, self.media, self.root / "outside.zip")

    def test_restore_never_overwrites_existing_database(self):
        archive = self.root / "private.zip"
        export_data(self.database, self.media, archive)
        with self.assertRaises(FileExistsError):
            restore_data(archive, self.media)

    def test_restore_rejects_checksum_corruption_before_install(self):
        original, corrupt = self.root / "original.zip", self.root / "corrupt.zip"
        export_data(self.database, self.media, original)
        with zipfile.ZipFile(original) as reader, zipfile.ZipFile(corrupt, "x") as writer:
            for item in reader.infolist():
                value = reader.read(item.filename)
                if item.filename.startswith("clips/"):
                    value = bytes([value[0] ^ 1]) + value[1:]
                writer.writestr(item.filename, value)
        target = self.root / "invalid"
        with self.assertRaisesRegex(ValueError, "checksum"):
            restore_data(corrupt, target)
        self.assertFalse((target / "clipforge.db").exists())

    def test_restore_rejects_zip_traversal(self):
        archive = self.root / "traversal.zip"
        with zipfile.ZipFile(archive, "x") as stream:
            stream.writestr("manifest.json", json.dumps({"format":1,"files":{"../escape":{"size":0,"sha256":""}}}))
            stream.writestr("../escape", b"")
        with self.assertRaises(ValueError):
            restore_data(archive, self.root / "invalid")
        self.assertFalse((self.root / "escape").exists())

    def test_secrets_preserve_key_hash_password_and_refuse_overwrite(self):
        folder = self.root / "secrets"
        initialize(folder, "test-only-password-123", "x" * 48, "")
        self.assertEqual((folder / "app_secret").read_text().strip(), "x" * 48)
        self.assertTrue(verify_password("test-only-password-123", (folder / "admin_password_hash").read_text().strip()))
        with self.assertRaises(FileExistsError):
            initialize(folder, "test-only-password-123", "y" * 48, "")

    def test_production_rejects_defaults_and_public_http(self):
        config = Settings(_env_file=None, production_mode=True)
        with self.assertRaises(RuntimeError):
            config.validate_runtime()
        folder = self.root / "secrets"
        initialize(folder, "test-only-password-123", "x" * 48, "")
        config.app_secret = "x" * 48
        config.admin_password_hash = (folder / "admin_password_hash").read_text().strip()
        config.embedded_worker = config.auto_create_schema = False
        config.public_base_url = "http://public.example"
        with self.assertRaises(RuntimeError):
            config.validate_runtime()
        config.public_base_url = "https://public.example"
        config.validate_runtime()
        config.admin_password_hash = "scrypt$$"
        with self.assertRaises(RuntimeError):
            config.validate_runtime()

    def test_historical_revision_adopts_existing_receipts(self):
        with database_connection(self.database) as db:
            db.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
            db.execute("INSERT INTO alembic_version VALUES ('0005_connection_hub_id')")
        environment = {**os.environ, "DATABASE_URL":f"sqlite:///{self.database}", "PRODUCTION_MODE":"false", "EMBEDDED_WORKER":"false"}
        result = subprocess.run([sys.executable, "-m", "deploy.migrate"], cwd=ROOT, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with database_connection(self.database) as db:
            self.assertEqual(db.execute("SELECT version_num FROM alembic_version").fetchone()[0], "0011_account_views")
            self.assertEqual(db.execute("SELECT job_id, stage FROM youtube_publications").fetchone(), (self.job_id, "published"))

    def test_migration_refuses_unknown_nonempty_schema(self):
        environment = {**os.environ, "DATABASE_URL":f"sqlite:///{self.database}", "PRODUCTION_MODE":"false", "EMBEDDED_WORKER":"false"}
        result = subprocess.run([sys.executable, "-m", "deploy.migrate"], cwd=ROOT, env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unversioned nonempty database", result.stderr)
        with database_connection(self.database) as db:
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='alembic_version'").fetchone())

    def test_origin_and_login_limit(self):
        self.assertTrue(same_origin("https://clips.example:443", "https://clips.example"))
        self.assertFalse(same_origin("https://evil.example", "https://clips.example"))
        limiter = LoginLimiter(limit=2)
        self.assertTrue(limiter.allow("client"))
        self.assertTrue(limiter.allow("client"))
        self.assertFalse(limiter.allow("client"))

    def test_http_guards_and_health(self):
        def database():
            with Session(self.engine) as db:
                yield db
        main.app.dependency_overrides[get_db] = database
        with patch.object(settings, "production_mode", True), patch.object(settings, "public_base_url", "https://clips.example"), patch.object(main, "login_limiter", LoginLimiter(limit=2)):
            client = TestClient(main.app)
            client.cookies.set("clipforge_session", create_session(settings.admin_username))
            self.assertEqual(client.get("/health").json(), {"status":"ok"})
            self.assertEqual(client.post("/api/pipeline/start", json={}, headers={"Origin":"https://evil.example"}).status_code, 403)
            self.assertEqual(client.get("/api/state").headers["X-Frame-Options"], "DENY")
            for _ in range(2):
                self.assertEqual(client.post("/login", data={"username":"bad","password":"bad"}, headers={"Origin":"https://clips.example"}, follow_redirects=False).status_code, 303)
            self.assertEqual(client.post("/login", data={"username":"bad","password":"bad"}, headers={"Origin":"https://clips.example"}).status_code, 429)
            client.close()

    def test_fresh_alembic_chain_and_repeat_migration(self):
        path = self.root / "new.db"
        environment = {**os.environ, "DATABASE_URL":f"sqlite:///{path}", "PRODUCTION_MODE":"false", "EMBEDDED_WORKER":"false"}
        for _ in range(2):
            result = subprocess.run([sys.executable, "-m", "deploy.migrate"], cwd=ROOT, env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with database_connection(path) as db:
            self.assertEqual(db.execute("SELECT version_num FROM alembic_version").fetchone()[0], "0011_account_views")
            self.assertTrue(db.execute("SELECT 1 FROM sqlite_master WHERE name='youtube_publications'").fetchone())

    def test_compose_uses_private_ports_and_current_job_types(self):
        document = yaml.safe_load((ROOT / "compose.yml").read_text())
        web, worker = document["services"]["web"], document["services"]["worker"]
        self.assertEqual(web["ports"], ["127.0.0.1:8001:8000"])
        self.assertEqual(worker["environment"]["EMBEDDED_WORKER"], "false")
        self.assertIn("youtube_publish", worker["environment"]["WORKER_JOB_TYPES"])
        self.assertNotIn("automation_publish", worker["environment"]["WORKER_JOB_TYPES"])
        self.assertTrue(worker["read_only"])
        self.assertFalse(worker.get("privileged", False))
        self.assertEqual(web["depends_on"]["migrate"]["condition"], "service_completed_successfully")


if __name__ == "__main__":
    unittest.main()
