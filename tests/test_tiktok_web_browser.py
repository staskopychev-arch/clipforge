"""Real browser + LOCAL TikTok-like form. Every request is intercepted."""
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from app.config import settings
from app.services import tiktok_web

HTML = '''<!doctype html><html><body>
<input type="file"><div contenteditable="true" role="textbox">filename-should-be-removed</div><p>Uploaded (10 MB)</p>
<label for="privacy">Who can watch this video</label><select id="privacy"><option value="public">Everyone</option><option value="private">Only you</option></select>
<button id="post" onclick="window.postClicks=(window.postClicks||0)+1; window.lastVisibility=document.querySelector('select').value; document.body.innerHTML='<div role=dialog>Video posted <a href=/@fixture/video/1234567890123456789>Open video</a></div>'">Post</button>
</body></html>'''


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TIKTOK_TESTS") == "1", "Opt-in browser fixture")
class TikTokBrowserTests(unittest.TestCase):
    def run_fixture(self, html, *, error=None, policy=None, expected_flags=(), success_receipt_html=None):
        with tempfile.TemporaryDirectory() as folder, sync_playwright() as driver:
            options = {"headless": True, "chromium_sandbox": True}
            if settings.browser_executable_path:
                options["executable_path"] = settings.browser_executable_path
            browser = driver.chromium.launch(**options)
            context = browser.new_context(locale="en-US")
            content_visits = 0
            def respond(route):
                nonlocal content_visits
                if '/tiktokstudio/content' in route.request.url:
                    content_visits += 1
                if "/top/v1" in route.request.url and policy:
                    route.fulfill(status=200, json=policy)
                elif success_receipt_html and '/tiktokstudio/content' in route.request.url and content_visits > 1:
                    route.fulfill(status=200, content_type='text/html; charset=utf-8', body=success_receipt_html)
                else:
                    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            context.route("**/*", respond)
            page = context.new_page()
            @contextmanager
            def fixture_session(_):
                with tiktok_web.monitor_upload_policy(page):
                    yield page
            stages = []
            def progress(stage, url):
                if stage == "submitting":
                    self.assertIsNone(page.evaluate("window.postClicks"))
                stages.append(stage)
            path = Path(folder) / "fixture.mp4"
            path.write_bytes(b"FIXTURE_NOT_REAL_MEDIA")
            try:
                with patch.object(tiktok_web, "tiktok_page", fixture_session), patch.object(tiktok_web, "validate_shorts_file"):
                    args = dict(encrypted_session="LOCAL_FIXTURE", video_path=path, description="Fixture description #test", visibility="private", on_progress=progress)
                    if error:
                        with self.assertRaises(error):
                            tiktok_web.upload_video(**args)
                        self.assertIsNone(page.evaluate("window.postClicks"))
                        self.assertNotIn("submitting", stages)
                    else:
                        result = tiktok_web.upload_video(**args)
                        self.assertTrue(result["confirmed"])
                        self.assertEqual(result["video_url"], "https://www.tiktok.com/@fixture/video/1234567890123456789")
                        self.assertEqual(page.evaluate("window.lastVisibility"), "private")
                        self.assertEqual(page.evaluate("window.postClicks"), 1)
                        self.assertEqual(stages, ["authenticated", "uploading", "submitting", "published"])
                        for flag in expected_flags:
                            self.assertTrue(page.evaluate('name => window[name]', flag), flag)
            finally:
                browser.close()

    def test_real_file_input_caption_visibility_post_and_receipt(self):
        self.run_fixture(HTML)

    def test_success_panel_without_permalink_requires_actual_studio_receipt(self):
        start = HTML.index('<button id="post"')
        html = HTML[:start] + '''<button id="post" onclick="sessionStorage.setItem('postClicks',String((Number(sessionStorage.getItem('postClicks'))||0)+1));sessionStorage.setItem('lastVisibility',document.querySelector('select').value);document.body.innerHTML='<section><h2>Your video has been uploaded</h2><p>You can manage your posts or upload another video.</p><button>Manage posts</button><button>Upload</button></section>'">Post</button></body></html>'''
        receipt = '''<!doctype html><html><body><script>window.postClicks=Number(sessionStorage.getItem('postClicks'));window.lastVisibility=sessionStorage.getItem('lastVisibility');</script><div><a href="/@fixture/video/1234567890123456789">Fixture description #test</a><p>Only me</p></div></body></html>'''
        self.run_fixture(html, success_receipt_html=receipt)

    def test_optional_cookie_overlay_arriving_after_file_selection_is_dismissed(self):
        overlay = "const overlay=document.createElement('div'); overlay.id='cookies'; overlay.style='position:fixed;inset:0;background:white;z-index:9999'; overlay.innerHTML='<button id=decline>Decline optional cookies</button>'; document.body.appendChild(overlay); document.getElementById('decline').onclick=()=>overlay.remove()"
        self.run_fixture(HTML.replace('<input type="file">', '<input type="file" onchange="' + overlay + '">'))

    def test_optional_check_setup_over_editor_tip_is_dismissed_in_order(self):
        script = "const tip=document.createElement('div');tip.style='position:fixed;inset:0;background:white;z-index:100';tip.innerHTML='<p>New editing features added</p><p>Now it’s easier than ever before to create professional and engaging videos.</p><div id=got>Got it</div>';document.body.appendChild(tip);window.tipShown=true;document.getElementById('got').onclick=()=>{window.tipDismissed=true;tip.remove()};const offer=document.createElement('div');offer.style='position:fixed;inset:0;background:white;z-index:200';offer.innerHTML='<p>Turn on automatic content checks?</p><p>Music copyright check</p><p>Content check lite</p><button id=cancel>Cancel</button><button>Turn on</button>';document.body.appendChild(offer);document.getElementById('cancel').onclick=()=>{window.offerDismissed=true;offer.remove()}"
        self.run_fixture(HTML.replace('<input type="file">', '<input type="file" onchange="' + script + '">'), expected_flags=['tipShown','tipDismissed','offerDismissed'])

    def test_current_optional_offer_without_music_heading_dismisses_only_cancel(self):
        script = "const tip=document.createElement('div');tip.style='position:fixed;inset:0;background:white;z-index:100';tip.innerHTML='<p>New editing features added</p><p>Now it’s easier than ever before to create professional and engaging videos.</p><div id=got>Got it</div>';document.body.appendChild(tip);document.getElementById('got').onclick=()=>{window.tipDismissed=true;tip.remove()};const offer=document.createElement('div');offer.style='position:fixed;inset:0;background:white;z-index:200';offer.innerHTML='<p>Turn on automatic content checks?</p><p>We’ll automatically check your video for copyright and other potential violations. You can adjust these settings anytime in Profile &gt; Settings</p><p>Content check lite</p><button id=cancel>Cancel</button><button id=enable>Turn on</button>';document.body.appendChild(offer);document.getElementById('cancel').onclick=()=>{window.offerDismissed=true;offer.remove()};document.getElementById('enable').onclick=()=>{throw new Error('Must not enable optional settings')}"
        self.run_fixture(HTML.replace('<input type="file">', '<input type="file" onchange="' + script + '">'), expected_flags=['tipDismissed','offerDismissed'])

    def test_challenge_over_optional_offer_is_not_dismissed(self):
        script = "document.body.insertAdjacentHTML('beforeend','<div style=position:fixed;inset:0;z-index:500;background:white><p>Turn on automatic content checks?</p><p>Music copyright check</p><p>Content check lite</p><p>Verify to continue</p><button onclick=throwError()>Cancel</button><button onclick=throwError()>Turn on</button></div>')"
        self.run_fixture(HTML.replace('<input type="file">', '<input type="file" onchange="' + script + '">'), error=tiktok_web.TikTokWebSessionError)

    def test_known_copyright_confirmation_is_completed_once(self):
        html = HTML.replace('onclick="window.postClicks=', 'onclick="window.postClicks=')
        start = html.index('<button id="post"')
        html = html[:start] + '''<button id="post" onclick="window.postClicks=1; window.lastVisibility=document.querySelector('select').value; document.body.innerHTML='<div role=dialog>Continue to post? The copyright check is incomplete.<button id=confirm>Post now</button></div>'; document.getElementById('confirm').onclick=()=>{document.body.innerHTML='<div role=dialog>Video posted <a href=/@fixture/video/1234567890123456789>Open video</a></div>'}">Post</button></body></html>'''
        self.run_fixture(html)

    def test_security_challenge_never_uploads_or_posts(self):
        self.run_fixture('<h1>Verify to continue</h1><input type=file><button>Post</button>', error=tiktok_web.TikTokWebSessionError)

    def test_signed_out_page_is_identified_before_file_selection(self):
        self.run_fixture('<h1>Log in to TikTok</h1><button>Log in</button>',error=tiktok_web.TikTokWebExpiredSessionError)

    def test_unknown_visibility_control_never_clicks_post(self):
        self.run_fixture('<input type=file><div contenteditable=true></div><button>Post</button>', error=tiktok_web.TikTokWebError)

    def test_current_post_label_custom_dropdown_with_unrelated_editor_combobox(self):
        html = '''<!doctype html><html><body><input type="file">
<div contenteditable="true" role="combobox"></div><p>Uploaded (10 MB)</p>
<div><span>Who can see this post</span><button id="privacy" role="combobox" onclick="document.getElementById('choices').hidden=false">Everyone</button></div>
<div id="choices" hidden><div role="option" onclick="document.getElementById('privacy').textContent='Only you'; document.getElementById('choices').hidden=true">Only you</div></div>
<button onclick="window.postClicks=(window.postClicks||0)+1; window.lastVisibility=document.getElementById('privacy').textContent==='Only you'?'private':'public'; document.body.innerHTML='<div role=dialog>Video posted <a href=/@fixture/video/1234567890123456789>Open video</a></div>'">Post</button>
</body></html>'''
        self.run_fixture(html)

    def test_explicit_transfer_failure_does_not_click_retry_or_post(self):
        self.run_fixture('<input type=file><div contenteditable=true></div><h2>Couldn’t upload</h2><p>Upload failed</p><button>Retry</button><button>Post</button>', error=tiktok_web.TikTokWebTransferError)

    def test_http_200_policy_block_never_clicks_post(self):
        html = HTML.replace('<input type="file">', '<input type="file" onchange="fetch(\'/top/v1?Action=ApplyUploadInner\')">').replace('<button id="post"', '<button disabled id="post"')
        self.run_fixture(html, error=tiktok_web.TikTokWebPolicyError, policy={"ResponseMetadata": {"Error": {"CodeN": 30522, "Message": "Policy requirements, request blocked"}}})
