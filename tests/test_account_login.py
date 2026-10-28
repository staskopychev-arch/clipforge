import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main
from app.config import settings
from app.db import Base, get_db
from app.models import Account, AccountLogin, RecordStatus
from app.security import create_session, decrypt
from app.services.account_login import LoginQueue, LoginQueueError, filtered_storage, import_credentials, parse_credentials, trusted_url

EXAMPLE = [{"login": "owner@example.com", "password": "TEST_SECRET_123", "display_name": "My channel"}]


class LoginQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.directory.name) / 'queue.db'}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.queue = LoginQueue(self.sessions)

    def tearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    def add(self, records=None):
        records = parse_credentials(json.dumps(records or EXAMPLE).encode(), "accounts.json")
        with self.sessions() as db:
            return import_credentials(db, records)

    def test_json_csv_password_spaces_and_wrapped_json(self):
        parsed = parse_credentials(b'login,password,display_name\nowner@example.com," a,b ",Channel\n', "accounts.csv")
        self.assertEqual(parsed[0]["password"], " a,b ")
        self.assertEqual(len(parse_credentials(json.dumps({"accounts": EXAMPLE}).encode(), "a.json")), 1)

    def test_bad_input_does_not_echo_secrets(self):
        for raw in (b'not-json SECRET', b'{"accounts":"SECRET"}', b'[{"login":"BAD","password":"SECRET"}]', b'\xffSECRET'):
            with self.assertRaises(LoginQueueError) as captured:
                parse_credentials(raw, "a.json")
            self.assertNotIn("SECRET", str(captured.exception))

    def test_duplicate_and_size_limits(self):
        for value in ([], EXAMPLE * 2, EXAMPLE * 51):
            with self.assertRaises(LoginQueueError):
                parse_credentials(json.dumps(value).encode(), "a.json")
        with self.assertRaises(LoginQueueError):
            parse_credentials(b'x' * (1024 * 1024 + 1), "a.json")

    def test_encryption_no_automatic_start_and_public_redaction(self):
        self.add()
        with self.sessions() as db:
            row = db.scalar(select(AccountLogin))
            self.assertNotIn("TEST_SECRET", row.password_ciphertext)
            self.assertNotIn("owner@", row.login_ciphertext)
            self.assertEqual(decrypt(row.password_ciphertext), EXAMPLE[0]["password"])
            self.assertEqual(row.status, "queued")
        public = json.dumps(self.queue.state())
        for secret in ("TEST_SECRET", "owner@", "ciphertext"):
            self.assertNotIn(secret, public)
        self.assertFalse(self.queue.running)

    def test_duplicate_pending_not_imported(self):
        self.add()
        with self.assertRaises(LoginQueueError):
            self.add()
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(AccountLogin)).all()), 1)

    def test_clear_wipes_credentials(self):
        self.add()
        self.queue.clear_pending()
        with self.sessions() as db:
            row = db.scalar(select(AccountLogin))
            self.assertEqual(row.status, "cancelled")
            self.assertIsNone(row.password_ciphertext)
            self.assertIsNone(row.login_ciphertext)

    def test_restart_does_not_resubmit_password(self):
        self.add()
        with self.sessions() as db:
            db.scalar(select(AccountLogin)).status = "awaiting_verification"
            db.commit()
        self.queue.recover()
        with self.sessions() as db:
            row = db.scalar(select(AccountLogin))
            self.assertEqual(row.status, "interrupted")
            self.assertIsNone(row.password_ciphertext)
        with self.assertRaises(LoginQueueError):
            self.queue.start()

    async def test_error_stops_without_trying_next_or_exposing_password(self):
        self.add(EXAMPLE + [{"login": "second@example.com", "password": "SECOND"}])
        with patch.object(self.queue, "_authenticate", AsyncMock(side_effect=RuntimeError("TEST_SECRET_123"))) as auth:
            self.queue.start()
            await self.queue.task
            auth.assert_awaited_once()
        self.assertEqual({r["status"] for r in self.queue.state()["records"]}, {"error", "queued"})
        self.assertNotIn("TEST_SECRET", json.dumps(self.queue.state()))
        with self.sessions() as db:
            self.assertIsNone(db.scalar(select(AccountLogin).where(AccountLogin.status == "error")).password_ciphertext)

    async def test_captcha_blocks_queue_and_stop_wipes_active_password(self):
        self.add(EXAMPLE + [{"login": "second@example.com", "password": "SECOND"}])
        calls = []
        async def auth(*_):
            calls.append(self.queue.active_id)
            await self.queue._manual("Manual verification required")
        with patch.object(self.queue, "_authenticate", auth):
            self.queue.start()
            for _ in range(30):
                if self.queue.waiting:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(self.queue.waiting)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sum(r["status"] == "queued" for r in self.queue.state()["records"]), 1)
            with self.assertRaises(LoginQueueError):
                self.queue.start()
            await self.queue.stop()
        self.assertFalse(self.queue.running)
        with self.sessions() as db:
            self.assertIsNone(db.scalar(select(AccountLogin).where(AccountLogin.status == "cancelled")).password_ciphertext)

    def test_session_saved_only_to_confirmed_channel_and_credentials_wiped(self):
        self.add()
        with self.sessions() as db:
            self.queue.active_id = db.scalar(select(AccountLogin.id))
        self.queue._save_session("UCtest", {"cookies": [{"domain": ".youtube.com", "name": "SID", "value": "SESSION"}], "origins": []})
        with self.sessions() as db:
            row, account = db.scalar(select(AccountLogin)), db.scalar(select(Account))
            self.assertEqual((row.status, account.status), ("ready", RecordStatus.ready))
            self.assertEqual(row.account_id, account.id)
            self.assertEqual(account.external_id, "youtube-channel:UCtest")
            self.assertNotIn("SESSION", account.access_token)
            self.assertIsNone(row.password_ciphertext)

    def test_unrelated_session_data_and_passkeys_not_saved(self):
        state = filtered_storage({"cookies": [{"domain": ".youtube.com", "name": "SID", "value": "OK"}, {"domain": "evilyoutube.com"}],
            "origins": [{"origin": "https://studio.youtube.com", "localStorage": []}, {"origin": "https://evil.test"}], "credentials": ["PRIVATE"]})
        self.assertEqual((len(state["cookies"]), len(state["origins"])), (1, 1))
        self.assertNotIn("credentials", state)

    def test_navigation_allowlist(self):
        self.assertTrue(trusted_url("https://accounts.google.com/signin"))
        for url in ("http://accounts.google.com", "https://accounts.google.com.attacker.test", "file:///tmp/x", "https://user:pass@accounts.google.com/", "https://accounts.google.com:8080/"):
            self.assertFalse(trusted_url(url))


class LoginQueueAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.directory.name) / 'api.db'}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.queue = LoginQueue(self.sessions)
        self.patch = patch.object(main, "login_queue", self.queue)
        self.patch.start()
        def db_override():
            with self.sessions() as db:
                yield db
        main.app.dependency_overrides[get_db] = db_override
        self.client = TestClient(main.app)
        self.client.cookies.set("clipforge_session", create_session(settings.admin_username))

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        self.patch.stop()
        self.engine.dispose()
        self.directory.cleanup()

    def test_import_requires_ownership_confirmation(self):
        response = self.client.post("/api/account-logins/import", files={"file": ("a.json", json.dumps(EXAMPLE))})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/account-logins/import", data={"owns_accounts": "true"}, files={"file": ("a.json", json.dumps(EXAMPLE))})
        self.assertEqual(response.status_code, 201, response.text)
        public = self.client.get("/api/account-logins")
        self.assertEqual(response.json()["imported"], 1)
        self.assertFalse(public.json()["running"])
        self.assertNotIn("TEST_SECRET", public.text)

    def test_panel_authentication_required(self):
        self.client.cookies.clear()
        for path in ("/api/account-logins", "/api/account-logins/id/screen"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post("/api/account-logins/start").status_code, 401)

    def test_control_requires_manual_session_and_never_echoes_text(self):
        response = self.client.post("/api/account-logins/no-session/action", json={"kind": "text", "text": "TEST_SECRET"})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("TEST_SECRET", response.text)

    def test_original_buttons_preserved_and_login_panel_present(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        for element in ("uploadVideoBtn", "accountImportForm", "ytDirectForm", "loginCredentialsForm", "loginQueueStart", "loginManualResume"):
            self.assertIn(f'id="{element}"', page.text)


if __name__ == "__main__":
    unittest.main()
