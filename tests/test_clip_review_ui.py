import json
import os
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from app.config import settings
from app.main import templates


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TIKTOK_TESTS') == '1', 'Opt-in browser fixture')
class ClipReviewUITests(unittest.TestCase):
    def test_review_is_explicit_preserves_playback_and_never_starts_queue(self):
        root = Path(__file__).resolve().parents[1] / 'app'
        html = templates.get_template('index.html').render(youtube_configured=True, tiktok_configured=False)
        account = {'id': 'owned', 'platform': 'tiktok_web', 'display_name': 'Owned account', 'status': 'ready'}
        draft = {'account_ids': ['owned'], 'queries': ['bread recipe'], 'topic': 'any',
                 'videos': [{'description': f'Caption {i}', 'clip_id': None} for i in range(5)]}
        batch = {'id': 'review-batch', 'status': 'awaiting_review', 'account_ids': ['owned'],
                 'position': 0, 'clip_ids': ['clip-a', None, None, None, None], 'completed': []}
        fixture = {'accounts': [account], 'sources': [{'id': 'source-a', 'title': 'Example source'}],
                   'clips': [{'id': 'clip-a', 'source_id': 'source-a', 'status': 'ready', 'duration_seconds': 15, 'start_seconds': 0},
                             {'id': 'clip-b', 'source_id': 'source-a', 'status': 'ready', 'duration_seconds': 15, 'start_seconds': 15}],
                   'jobs': [], 'automation': {'enabled': False, 'status': 'review_required',
                       'config': {'platform': 'tiktok_web', 'five_video_draft': draft, 'five_video_batch': batch}}}
        mutations = []
        with sync_playwright() as driver:
            options = {'headless': True, 'chromium_sandbox': True}
            if settings.browser_executable_path:
                options['executable_path'] = settings.browser_executable_path
            browser = driver.chromium.launch(**options)
            try:
                page = browser.new_page(viewport={'width': 1280, 'height': 900})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def respond(route):
                    request = route.request
                    path = request.url.split('https://clipforge.test', 1)[-1].split('?', 1)[0]
                    if path == '/api/state':
                        route.fulfill(json=fixture)
                    elif path == '/api/automation/five-videos/review':
                        payload = json.loads(request.post_data)
                        mutations.append(payload)
                        batch.setdefault('clip_reviews', {})[payload['clip_id']] = {'decision': payload['decision'], 'sha256': 'fixture'}
                        if payload['decision'] == 'rejected':
                            batch['status'] = 'rejected'
                            fixture['automation']['status'] = 'stopped'
                        route.fulfill(json=fixture['automation'])
                    elif path.startswith('/api/clips/'):
                        route.fulfill(body=b'fixture-media', content_type='video/mp4')
                    elif path.startswith('/api/'):
                        if request.method != 'GET':
                            mutations.append({'unexpected': path})
                        route.fulfill(json={})
                    elif path.startswith('/static/'):
                        file = root / path.lstrip('/')
                        route.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else 'text/css' if file.suffix == '.css' else 'image/svg+xml')
                    else:
                        route.fulfill(body=html, content_type='text/html')
                page.route('**/*', respond)
                page.goto('https://clipforge.test/')
                expect(page.locator('#fiveReview')).to_be_visible()
                expect(page.locator('#fiveReviewVideo')).to_have_attribute('src', '/api/clips/clip-a/preview')
                expect(page.locator('#fiveReviewSource')).to_contain_text('Example source')
                page.locator('#fiveConsent').check()
                expect(page.locator('#fiveStart')).to_be_disabled()
                expect(page.locator('#fiveReviewApprove')).to_be_disabled()
                page.locator('#fiveReviewWatched').check()
                page.evaluate('refresh()')
                expect(page.locator('#fiveReviewWatched')).to_be_checked()
                page.locator('#fiveReviewApprove').click()
                expect(page.locator('#fiveReviewStatus')).to_contain_text('Фрагмент подтверждён')
                expect(page.locator('#fiveStart')).to_be_enabled()
                self.assertFalse(fixture['automation']['enabled'])
                self.assertEqual(len(mutations), 1)
                self.assertEqual(mutations[0], {'batch_id': 'review-batch', 'clip_id': 'clip-a', 'decision': 'approved', 'watched': True})
                batch['clip_ids'][0] = 'clip-b'
                page.evaluate('refresh()')
                expect(page.locator('#fiveReviewWatched')).not_to_be_checked()
                expect(page.locator('#fiveReviewVideo')).to_have_attribute('src', '/api/clips/clip-b/preview')
                expect(page.locator('#fiveStart')).to_be_disabled()
                screenshots = Path(os.environ.get('QA_OUTPUT_DIR', '/tmp'))
                screenshots.mkdir(parents=True, exist_ok=True)
                page.locator('#fiveReview').screenshot(path=str(screenshots / 'clip-review-desktop.png'))
                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('#fiveReview').screenshot(path=str(screenshots / 'clip-review-mobile.png'))
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)
                page.locator('#fiveReviewReject').click()
                expect(page.locator('#fiveReviewStatus')).to_contain_text('Отклонён')
                expect(page.locator('#fiveStart')).to_have_text('Начать новую партию')
                expect(page.locator('#fiveReviewApprove')).to_be_disabled()
                self.assertFalse(fixture['automation']['enabled'])
                self.assertEqual(len(mutations), 2)
                self.assertEqual(mutations[1]['decision'], 'rejected')
                self.assertEqual(errors, [])
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
