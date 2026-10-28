"""The start button explains blockers, saves edits, and never bypasses login."""
import copy,json,os,unittest
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from app.config import settings
from app.main import templates


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TIKTOK_TESTS')=='1','Opt-in browser fixture')
class BatchStartUITests(unittest.TestCase):
    def test_thirty_accounts_autosave_start_and_clear_failure_states(self):
        root=Path(__file__).resolve().parents[1]/'app'
        html=templates.get_template('index.html').render(youtube_configured=True,tiktok_configured=False)
        accounts=[{'id':f'fixture-{i}','platform':'tiktok_web','display_name':f'Session 3 ({i+1})','status':'ready'} for i in range(30)]
        draft={'account_ids':[a['id'] for a in accounts],'videos':[{'description':f'Bread recipe {i}','clip_id':None} for i in range(5)],'queries':['bread recipe'],'topic':'any'}
        fixture={'accounts':accounts,'sources':[],'clips':[],'jobs':[], 'automation':{'enabled':False,'status':'stopped','config':{'platform':'tiktok_web','queries':['bread recipe'],'topic':'any','five_video_draft':draft}}}
        calls=[];flags={'fail_save':False}
        with sync_playwright() as p:
            options={'headless':True,'chromium_sandbox':True}
            if settings.browser_executable_path:options['executable_path']=settings.browser_executable_path
            browser=p.chromium.launch(**options)
            try:
                page=browser.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                def respond(route):
                    req=route.request;path=req.url.split('https://clipforge.test',1)[-1].split('?',1)[0]
                    if path=='/api/state':route.fulfill(json=fixture)
                    elif path=='/api/automation/five-videos':
                        payload=json.loads(req.post_data);calls.append(('save',payload))
                        if flags['fail_save']:route.fulfill(status=409,json={'detail':'Fixture save failed'})
                        else:
                            fixture['automation']['config']['five_video_draft']=copy.deepcopy(payload)
                            route.fulfill(json=fixture['automation'])
                    elif path=='/api/automation/five-videos/start':
                        calls.append(('start',json.loads(req.post_data)))
                        fixture['automation']['enabled']=True;fixture['automation']['status']='running'
                        route.fulfill(json=fixture['automation'])
                    elif path=='/api/account-logins':route.fulfill(json={'records':[],'running':False,'waiting':False,'message':''})
                    elif path.startswith('/api/'):route.fulfill(json={})
                    elif path.startswith('/static/'):
                        file=root/path.lstrip('/');route.fulfill(body=file.read_bytes(),content_type='text/javascript' if file.suffix=='.js' else 'text/css' if file.suffix=='.css' else 'image/svg+xml')
                    else:route.fulfill(body=html,content_type='text/html')
                page.route('**/*',respond);page.goto('https://clipforge.test/')
                start=page.locator('#fiveStart');hint=page.locator('#fiveStartHint')
                expect(page.locator('#fiveAccounts input:checked')).to_have_count(30)
                expect(start).to_be_disabled();expect(hint).to_contain_text('Поставьте галочку')
                self.assertEqual(start.evaluate('el => getComputedStyle(el).cursor'),'not-allowed')
                page.locator('#fiveConsent').check();expect(start).to_be_enabled()
                page.evaluate("() => {state.automation.last_pipeline_id='old-pipeline';state.jobs=[{id:'old-download',pipeline_id:'old-pipeline',type:'download',status:'queued'}];renderFiveVideos(state);}")
                expect(start).to_be_disabled();expect(hint).to_contain_text('Нажмите «Остановить 24/7»')
                page.evaluate("() => {state.jobs[0].status='processing';renderFiveVideos(state);}")
                expect(hint).to_contain_text('ещё выполняется')
                page.evaluate("() => {state.jobs[0].status='error';renderFiveVideos(state);}")
                expect(start).to_be_enabled()
                page.locator('#fiveDescription0').fill('');expect(hint).to_contain_text('Заполните описания роликов: 1')
                page.locator('#fiveDescription0').fill('Bread recipe 1');expect(hint).to_contain_text('пять разных описаний')
                page.locator('#fiveDescription0').fill('Edited bread recipe')
                page.locator('#automationQueries').fill('');expect(start).to_be_disabled();expect(hint).to_contain_text('Добавьте поисковый запрос')
                page.locator('#automationQueries').fill('bread baking')
                expect(start).to_be_enabled();expect(start).to_have_text('Сохранить и запустить партию из 5 видео')
                flags['fail_save']=True
                page.evaluate('() => { window.automationCheckAccounts = async () => { throw new Error("Login check must not run after failed save"); }; }')
                start.click();expect(page.locator('#fiveError')).to_have_text('Fixture save failed');expect(start).to_be_enabled()
                self.assertEqual([name for name,payload in calls],['save'])
                flags['fail_save']=False
                page.evaluate('() => { window.automationCheckAccounts = async () => false; }')
                start.click();expect(page.locator('#fiveError')).to_contain_text('Вход подтверждён не для всех')
                expect(start).to_be_enabled();self.assertEqual([name for name,payload in calls],['save','save'])
                page.locator('#automationQueries').fill('final bread recipe')
                page.evaluate('() => { window.automationCheckAccounts = ids => new Promise(resolve => {window.finishAccountCheck = resolve; window.checkedAccounts = ids;}); }')
                start.click();expect(start).to_have_text('Проверяем вход в аккаунты…');expect(start).to_have_attribute('aria-busy','true')
                self.assertEqual(start.evaluate('el => getComputedStyle(el).cursor'),'wait')
                self.assertEqual(page.evaluate('window.checkedAccounts.length'),30)
                page.dispatch_event('#fiveStart','click')
                self.assertEqual([name for name,payload in calls],['save','save','save'])
                page.evaluate('window.finishAccountCheck(true)')
                expect(hint).to_contain_text('Очередь уже включена');expect(start).to_have_attribute('aria-busy','false')
                self.assertEqual([name for name,payload in calls],['save','save','save','start'])
                self.assertEqual(calls[-2][1]['queries'],['final bread recipe'])
                self.assertEqual(calls[-2][1]['videos'][0]['description'],'Edited bread recipe')
                self.assertEqual(calls[-1][1]['queries'],['final bread recipe'])
                self.assertEqual(errors,[])
            finally:browser.close()
