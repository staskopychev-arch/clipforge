"""Only the converted file is downloaded; installer adverts are never selected."""
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright
from app.config import settings
from app.services.y2save import Y2SaveDownloader,Y2SaveError


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TIKTOK_TESTS')=='1','Opt-in browser fixture')
class DownloadControlTests(unittest.TestCase):
    def test_closes_installer_ad_and_waits_for_actual_converted_file(self):
        html='''<input placeholder="Paste YouTube link"><input type="checkbox">
        <button type="submit" onclick="document.querySelector('.s4k-ov').hidden=false;setTimeout(()=>{let a=document.createElement('a');a.download='video.mp4';a.href='#actual-video';a.onclick=()=>window.actualClicked=true;a.textContent='Download';document.querySelector('#result').append(a)},400)">Convert</button>
        <div class="s4k-ov" hidden>Install Save4k to download
        <button class="s4k-x" aria-label="Close" onclick="this.parentElement.remove()">×</button>
        <button class="s4k-pri" onclick="window.adClicked=true">Download</button></div><div id="result"></div>'''
        with sync_playwright() as p,tempfile.TemporaryDirectory() as temp:
            options={'headless':True,'chromium_sandbox':True}
            if settings.browser_executable_path:options['executable_path']=settings.browser_executable_path
            browser=p.chromium.launch(**options)
            try:
                context=browser.new_context(accept_downloads=True)
                page=context.new_page()
                page.route('https://fixture.test/**',lambda route:route.fulfill(body=b'video-fixture'*200,headers={'Content-Disposition':'attachment; filename=video.mp4'},content_type='video/mp4') if route.request.url.endswith('/video.mp4') else route.fulfill(body=html,content_type='text/html'))
                page.goto('https://fixture.test/en')
                downloader=Y2SaveDownloader()
                page.get_by_role('button',name='Convert',exact=True).click()
                control=downloader._wait_download_control(page,5000)
                self.assertEqual(control.get_attribute('download'),'video.mp4')
                self.assertEqual(control.get_attribute('href'),'#actual-video')
                control.click()
                self.assertTrue(page.evaluate('Boolean(window.actualClicked)'))
                self.assertFalse(page.evaluate('Boolean(window.adClicked)'))
                self.assertEqual(page.locator('.s4k-ov').count(),0)
                context.close()
            finally:browser.close()

    def test_ad_without_close_is_never_selected_as_video(self):
        with sync_playwright() as p:
            options={'headless':True,'chromium_sandbox':True}
            if settings.browser_executable_path:options['executable_path']=settings.browser_executable_path
            browser=p.chromium.launch(**options)
            try:
                page=browser.new_page();page.set_content('<div class="s4k-ov"><button class="s4k-pri">Download</button></div>')
                with self.assertRaisesRegex(Y2SaveError,'рекламное окно'):
                    Y2SaveDownloader()._download_control(page)
            finally:browser.close()
