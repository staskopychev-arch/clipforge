import copy
import json
import os
import unittest
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from app.config import settings
from app.main import templates


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TIKTOK_TESTS') == '1', 'Opt-in browser fixture')
class FiveVideoUITests(unittest.TestCase):
    def test_five_descriptions_save_without_start_survive_poll_and_reload(self):
        root = Path(__file__).resolve().parents[1] / 'app'
        html = templates.get_template('index.html').render(youtube_configured=True, tiktok_configured=False)
        accounts = [{'id': f'fixture-{i}', 'platform': 'tiktok_web', 'display_name': f'TikTok TXT20 {i+1:02d}', 'status': 'ready'} for i in range(20)]
        draft = {'account_ids': [a['id'] for a in accounts], 'videos': [{'description': '', 'clip_id': None} for _ in range(5)]}
        fixture = {'accounts': accounts, 'sources': [], 'clips': [], 'jobs': [], 'automation': {'enabled': False, 'status': 'stopped', 'config': {'platform': 'tiktok_web', 'queries': ['bread recipe'], 'topic': 'any', 'five_video_draft': draft}}}
        mutations = []
        starts = []
        with sync_playwright() as driver:
            options = {'headless': True, 'chromium_sandbox': True}
            if settings.browser_executable_path: options['executable_path'] = settings.browser_executable_path
            browser = driver.chromium.launch(**options)
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                def respond(route):
                    req = route.request
                    path = req.url.split('https://clipforge.test', 1)[-1].split('?', 1)[0]
                    if path == '/api/state': route.fulfill(json=fixture)
                    elif path == '/api/automation/five-videos':
                        payload = json.loads(req.post_data); mutations.append(path)
                        fixture['automation']['config']['five_video_draft'] = payload
                        route.fulfill(json=fixture['automation'])
                    elif path == '/api/automation/five-videos/start':
                        starts.append(json.loads(req.post_data))
                        route.fulfill(json=fixture['automation'])
                    elif path == '/api/account-logins': route.fulfill(json={'records': [], 'running': False, 'waiting': False, 'message': ''})
                    elif path.startswith('/api/'):
                        if req.method != 'GET': mutations.append(path)
                        route.fulfill(json={})
                    elif path.startswith('/static/'):
                        file = root / path.lstrip('/')
                        route.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else 'text/css' if file.suffix == '.css' else 'image/svg+xml')
                    else: route.fulfill(body=html, content_type='text/html')
                page.route('**/*', respond)
                page.goto('https://clipforge.test/')
                expect(page.locator('#fiveVideoCards textarea')).to_have_count(5)
                expect(page.locator('#fiveTarget')).to_have_text('20 аккаунтов × 5 роликов = 100 публикаций')
                for i in range(5): page.locator(f'#fiveDescription{i}').fill(f'Maikling sipi ng laro {i+1}. #Pilipinas')
                page.evaluate('refresh()')
                expect(page.locator('#fiveDescription3')).to_have_value('Maikling sipi ng laro 4. #Pilipinas')
                expect(page.locator('#fiveStart')).to_be_disabled()
                page.locator('#fiveSave').click()
                expect(page.locator('#fiveSaveState')).to_have_text('Описания и поиск сохранены на сервере')
                self.assertEqual(mutations, ['/api/automation/five-videos'])
                self.assertFalse(fixture['automation']['enabled'])
                page.reload()
                expect(page.locator('#fiveDescription4')).to_have_value('Maikling sipi ng laro 5. #Pilipinas')
                expect(page.locator('#fiveAccountRows tr')).to_have_count(20)
                expect(page.locator('#fiveStart')).to_be_disabled()
                page.locator('#fiveConsent').check()
                expect(page.locator('#fiveStart')).to_be_enabled()
                # No start is clicked in this fixture or against the real panel.
                screenshots = Path(os.environ.get('QA_OUTPUT_DIR', '/tmp'))
                screenshots.mkdir(parents=True, exist_ok=True)
                page.locator('#fiveVideoPanel').screenshot(path=str(screenshots / 'clipforge-five-editor-desktop.png'))
                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('#fiveVideoPanel').screenshot(path=str(screenshots / 'clipforge-five-editor-mobile.png'))
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)
                self.assertEqual(errors, [])
                self.assertEqual(mutations, ['/api/automation/five-videos'])
                # Explicit skips advance rotation but must never appear as created posts.
                fixture['automation']['enabled'] = True
                fixture['automation']['config']['five_video_batch'] = {
                    'id': 'fixture-batch', 'status': 'running', 'position': 23,
                    'account_ids': draft['account_ids'], 'clip_ids': [None] * 5,
                    'completed': [f'fixture-confirmed-{i}' for i in range(22)],
                    'skipped': [{'job_id': 'fixture-unknown', 'position': 22,
                        'account_id': accounts[2]['id'], 'round': 2}],
                }
                page.evaluate('refresh()')
                expect(page.locator('#fiveBatchStatus')).to_contain_text('Создано: 22. Пропущено: 1. Обработано 23 / 100.')
                expect(page.locator('#fiveAccountRows tr').nth(2)).to_contain_text('1 / 5 · пропущено: 1')
                expect(page.locator('#fiveProgress1')).to_contain_text('2 / 20 аккаунтов · пропущено: 1')
                expect(page.locator('#fiveAccountRows tr').nth(1)).to_contain_text('2 / 5')
                self.assertEqual(mutations, ['/api/automation/five-videos'])
                expect(page.locator('#fiveAccounts input').first).to_be_disabled()
                fixture['automation']['enabled'] = False
                fixture['automation']['status'] = 'stopped'
                saved_batch = copy.deepcopy(fixture['automation']['config']['five_video_batch'])
                page.evaluate('refresh()')
                page.locator('.five-account-picker summary').click()
                expect(page.locator('#fiveAccounts input').first).to_be_enabled()
                page.locator('#fiveAccounts input').first.uncheck()
                page.locator('#fiveSave').click()
                expect(page.locator('#fiveSaveState')).to_have_text('Описания и поиск сохранены на сервере')
                self.assertEqual(len(fixture['automation']['config']['five_video_draft']['account_ids']), 19)
                self.assertEqual(fixture['automation']['config']['five_video_batch'], saved_batch)
                self.assertFalse(fixture['automation']['enabled'])
                page.reload()
                expect(page.locator('#fiveAccounts input').first).to_be_enabled()
                expect(page.locator('#fiveAccounts input:checked')).to_have_count(19)
                self.assertEqual(mutations, ['/api/automation/five-videos'] * 2)
                fixture['jobs'] = [
                    {'id':'public-post','type':'tiktok_web_publish','status':'ready','attempts':1,'account_id':accounts[0]['id'],'account_name':'Account 1',
                     'result':{'confirmed':True,'video_url':'https://www.tiktok.com/@fixture/video/1234567890123456789','visibility':'public'}},
                    {'id':'reviewed-post','type':'tiktok_web_publish','status':'error','attempts':1,'account_id':accounts[1]['id'],'account_name':'Account 2','error':'Created but visibility not confirmed',
                     'result':{'confirmed':True,'video_url':'https://www.tiktok.com/@fixture/video/1234567890123456790','visibility':'private','moderation_status':'under_review'}},
                ]
                page.evaluate('refresh()')
                public_row=page.locator('[data-job-id="public-post"]')
                reviewed_row=page.locator('[data-job-id="reviewed-post"]')
                expect(public_row.get_by_role('link',name='Открыть TikTok-видео')).to_have_attribute('href',fixture['jobs'][0]['result']['video_url'])
                expect(reviewed_row.get_by_role('link',name='Открыть TikTok-видео')).to_have_attribute('href',fixture['jobs'][1]['result']['video_url'])
                expect(public_row.get_by_role('link',name='@fixture')).to_have_attribute('href','https://www.tiktok.com/@fixture')
                expect(reviewed_row).to_contain_text('Создано · на проверке')
                expect(reviewed_row.locator('.retry')).to_have_count(0)
                self.assertNotIn('video_url',public_row.inner_text())
                # Query edits are saved together with descriptions without changing
                # the search associated with the existing stopped batch.
                page.locator('#automationQueries').fill('bread baking\npizza recipe')
                page.evaluate('refresh()')
                expect(page.locator('#automationQueries')).to_have_value('bread baking\npizza recipe')
                expect(page.locator('#fiveStart')).to_be_disabled()
                page.locator('#fiveSave').click()
                expect(page.locator('#fiveSaveState')).to_have_text('Описания и поиск сохранены на сервере')
                self.assertEqual(fixture['automation']['config']['five_video_draft']['queries'], ['bread baking', 'pizza recipe'])
                self.assertEqual(fixture['automation']['config']['queries'], ['bread recipe'])
                page.reload()
                expect(page.locator('#automationQueries')).to_have_value('bread baking\npizza recipe')
                # External saved changes refresh a clean field but preserve local edits.
                fixture['automation']['config']['five_video_draft']['queries'] = ['external saved query']
                page.evaluate('refresh()')
                expect(page.locator('#automationQueries')).to_have_value('external saved query')
                page.locator('#automationQueries').fill('local unsaved query')
                fixture['automation']['config']['five_video_draft']['queries'] = ['another external query']
                page.evaluate('refresh()')
                expect(page.locator('#automationQueries')).to_have_value('local unsaved query')
                page.locator('#fiveSave').click()
                expect(page.locator('#fiveSaveState')).to_have_text('Описания и поиск сохранены на сервере')
                page.evaluate('window.automationCheckAccounts = async () => true')
                page.locator('#fiveConsent').check()
                page.locator('#fiveStart').click()
                expect(page.locator('#fiveStart')).to_be_enabled()
                self.assertEqual(len(starts), 1)
                self.assertEqual(starts[0]['queries'], ['local unsaved query'])
                self.assertEqual(starts[0]['topic'], 'any')
                self.assertEqual(errors, [])
            finally:
                browser.close()


if __name__ == '__main__': unittest.main()
