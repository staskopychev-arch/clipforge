"""Opt-in real Chromium test. Every Google request is fulfilled LOCALLY.

No real Google login, CAPTCHA or account is contacted/created by this fixture.
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Base
from app.models import Account, AccountLogin
from app.services.account_login import LoginQueue, SIGNIN_URL, import_credentials, parse_credentials


@unittest.skipUnless(os.environ.get("RUN_BROWSER_LOGIN_TESTS") == "1", "Opt-in browser fixture")
class BrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_browser_form_fill_manual_pause_and_session_save(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite:///{Path(directory) / 'fixture.db'}")
            Base.metadata.create_all(engine)
            sessions = sessionmaker(engine, expire_on_commit=False)
            queue = LoginQueue(sessions)
            with sessions() as db:
                import_credentials(db, parse_credentials(b'[{"login":"fixture@example.com","password":"FIXTURE_ONLY"}]', "fixture.json"))
                queue.active_id = db.scalar(select(AccountLogin.id))
            async with async_playwright() as driver:
                options = {"headless": True, "chromium_sandbox": True}
                if settings.browser_executable_path:
                    options["executable_path"] = settings.browser_executable_path
                browser = await driver.chromium.launch(**options)
                context = await browser.new_context()
                page = await context.new_page()
                navigations = []
                async def route(request):
                    url = request.request.url
                    navigations.append(url.split("?")[0])
                    if urlsplit(url).hostname == "studio.youtube.com":
                        html = '<ytcp-app><button>Create</button></ytcp-app>'
                    elif "/fixture-password" in url:
                        html = '<h1>Verify it\'s you</h1><input type="password"><button id="passwordNext" onclick="location.href=\'/fixture-check\'">Next</button>'
                    elif "/fixture-check" in url:
                        html = '<p>Verify it\'s you</p><button id="operator" onclick="location.href=\'https://studio.youtube.com/channel/UCfixture\'">Fixture operator confirmation</button>'
                    else:
                        html = '<input type="email"><button id="identifierNext" onclick="location.href=\'/fixture-password\'">Next</button>'
                    await request.fulfill(status=200, content_type="text/html", body=html)
                await context.route("**/*", route)
                # A synthetic session marker, never a real Google cookie.
                await context.add_cookies([{"name": "FIXTURE", "value": "TEST_ONLY", "domain": ".youtube.com", "path": "/"}])
                await page.goto(SIGNIN_URL)
                queue.page = page
                task = asyncio.create_task(queue._sign_in(page, context, "fixture@example.com", "FIXTURE_ONLY"))
                try:
                    for _ in range(100):
                        if queue.waiting:
                            break
                        await asyncio.sleep(0.1)
                    if task.done():
                        task.result()
                    self.assertTrue(queue.waiting, f"Fixture did not pause: {page.url}; {await page.locator('body').inner_text()}")
                    self.assertFalse(task.done())
                    image = await queue.screenshot(queue.active_id)
                    self.assertGreater(len(image), 500)
                    # Simulated operator acts on our fixture, not a real CAPTCHA.
                    await page.locator("#operator").click()
                    await queue.action(queue.active_id, {"kind": "resume"})
                    await asyncio.wait_for(task, 15)
                    with sessions() as db:
                        self.assertEqual(db.get(AccountLogin, queue.active_id).status, "ready")
                        self.assertIsNone(db.get(AccountLogin, queue.active_id).password_ciphertext)
                        self.assertEqual(db.scalar(select(Account)).external_id, "youtube-channel:UCfixture")
                    self.assertTrue(any("fixture-password" in url for url in navigations))
                    self.assertTrue(any("fixture-check" in url for url in navigations))
                finally:
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    await browser.close()
            engine.dispose()
