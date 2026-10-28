"""Exercise the actual 24/7 panel with a local browser and no external services."""
import copy,json,os,unittest
from pathlib import Path
from playwright.sync_api import sync_playwright
from app.config import settings
from app.main import templates


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TIKTOK_TESTS')=='1','Opt-in browser fixture')
class AutomationPanelTests(unittest.TestCase):
    def test_permission_label_is_visible_and_queries_stay_empty(self):
        root = Path(__file__).resolve().parents[1] / 'app'
        html = templates.get_template('index.html').render(youtube_configured=True, tiktok_configured=False)
        state = {'accounts': [], 'sources': [], 'clips': [], 'jobs': [],
                 'automation': {'enabled': False, 'status': 'paused', 'config': {'queries': [], 'topic': 'any'}}}
        with sync_playwright() as driver:
            options = {'headless': True, 'chromium_sandbox': True}
            if settings.browser_executable_path:
                options['executable_path'] = settings.browser_executable_path
            browser = driver.chromium.launch(**options)
            try:
                page = browser.new_page()
                mutations = []
                def respond(route):
                    request = route.request
                    path = request.url.split('https://clipforge.test', 1)[-1].split('?', 1)[0]
                    if request.method != 'GET':
                        mutations.append(path)
                    if path == '/api/state':
                        route.fulfill(json=state)
                    elif path.startswith('/static/'):
                        file = root / path.lstrip('/')
                        route.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else 'text/css')
                    else:
                        route.fulfill(body=html, content_type='text/html')
                page.route('**/*', respond)
                page.goto('https://clipforge.test/')
                page.wait_for_function('state.automation?.status === "paused"')
                self.assertTrue(page.locator('#searchRightsMode').is_visible())
                self.assertIn('Есть разрешение автора', page.locator('#searchRightsMode').inner_text())
                self.assertEqual(page.locator('#automationQueries').input_value(), '')
                self.assertEqual(page.locator('#discoveryQuery').input_value(), '')
                self.assertEqual(page.locator('#automationTopic option').inner_text(), 'Лучший из первых 20 результатов')
                self.assertIn('Первый результат тоже участвует', page.locator('#automationPanel').inner_text())
                self.assertEqual(mutations, [])
            finally:
                browser.close()

    def test_paste_cookie_dialog_updates_selected_account_and_clears_text(self):
        root=Path(__file__).resolve().parents[1]/'app'
        html=templates.get_template('index.html').render(youtube_configured=True,tiktok_configured=False)
        account={'id':'fixture-owned','platform':'tiktok_web','display_name':'@owned','status':'reauth_required'}
        state={'accounts':[account],'sources':[],'clips':[],'jobs':[],
               'automation':{'enabled':False,'status':'paused','config':{'platform':'tiktok_web','account_ids':[account['id']]}}}
        mutations=[]
        with sync_playwright() as driver:
            options={'headless':True,'chromium_sandbox':True}
            if settings.browser_executable_path: options['executable_path']=settings.browser_executable_path
            browser=driver.chromium.launch(**options)
            try:
                page=browser.new_page(viewport={'width':390,'height':844})
                def respond(route):
                    request=route.request
                    path=request.url.split('https://clipforge.test',1)[-1].split('?',1)[0]
                    if path=='/api/state': route.fulfill(json=state)
                    elif path=='/api/tiktok-web/cookies/paste':
                        payload=json.loads(request.post_data); mutations.append((path,payload))
                        if payload['text']=='invalid': route.fulfill(status=400,json={'detail':'Проверьте формат cookie'})
                        else: route.fulfill(json={'account_id':account['id'],'created':False,'message':'JSON создан, cookie сохранены'})
                    elif path==f"/api/accounts/{account['id']}/check-session":
                        mutations.append((path,{}))
                        account['status']='ready'
                        route.fulfill(json={'authenticated':True})
                    elif path.startswith('/api/'):
                        if request.method!='GET': mutations.append((path,{}))
                        route.fulfill(json={})
                    elif path.startswith('/static/'):
                        file=root/path.lstrip('/')
                        route.fulfill(body=file.read_bytes(),content_type='text/javascript' if file.suffix=='.js' else 'text/css' if file.suffix=='.css' else 'image/svg+xml')
                    else: route.fulfill(body=html,content_type='text/html')
                page.route('**/*',respond)
                page.goto('https://clipforge.test/')
                page.locator('#automationPanel [data-paste-tiktok-cookies]').click()
                dialog=page.locator('#ttCookieDialog')
                self.assertEqual(page.locator('#ttCookieAccount').input_value(),account['id'])
                page.locator('#ttCookieText').fill('invalid')
                page.locator('#ttCookieOwns').check()
                dialog.get_by_role('button',name='Создать JSON и сохранить',exact=True).click()
                page.get_by_role('alert').filter(has_text='Проверьте формат cookie').wait_for()
                self.assertEqual(page.locator('#ttCookieText').input_value(),'invalid')
                page.locator('#ttCookieText').fill('sessionid=FIXTURE_ONLY; sid_guard=abc%7C123')
                dialog.get_by_role('button',name='Создать JSON и сохранить',exact=True).click()
                dialog.wait_for(state='hidden')
                self.assertEqual(page.locator('#ttCookieText').input_value(),'')
                self.assertFalse(page.locator('#ttCookieOwns').is_checked())
                page.locator('#automationAccountStatus').get_by_text('Вход подтверждён',exact=True).wait_for()
                self.assertEqual(len(mutations),3)
                self.assertEqual(mutations[1][1]['account_id'],account['id'])
                self.assertTrue(mutations[1][1]['owns_account'])
                self.assertEqual(mutations[-1][0],f"/api/accounts/{account['id']}/check-session")
                self.assertNotIn('FIXTURE_ONLY',page.locator('#automationAccountStatus').inner_text())
                self.assertEqual(page.locator('#tiktokWebPanel').count(),0)
                self.assertEqual(page.locator('#ttCheckSession').count(),0)
                page.locator('#accountImportPanel [data-paste-tiktok-cookies]').click()
                page.locator('#ttCookieText').fill('sessionid=NOT_SAVED')
                page.keyboard.press('Escape')
                dialog.wait_for(state='hidden')
                self.assertEqual(page.locator('#ttCookieText').input_value(),'')
                self.assertEqual(len(mutations),3)
            finally: browser.close()

    def test_account_selection_checks_once_and_blocks_start_on_failed_login(self):
        root=Path(__file__).resolve().parents[1]/'app'
        html=templates.get_template('index.html').render(youtube_configured=True,tiktok_configured=False)
        account={'id':'unverified','platform':'tiktok_web','display_name':'@needs_login','status':'reauth_required'}
        plan={'enabled':False,'status':'stopped','config':{'platform':'tiktok_web','queries':['bread recipe'],'publish':True,'account_ids':[]}}
        state={'accounts':[account],'sources':[],'clips':[],'jobs':[],'automation':plan}
        requests=[]
        with sync_playwright() as driver:
            options={'headless':True,'chromium_sandbox':True}
            if settings.browser_executable_path: options['executable_path']=settings.browser_executable_path
            browser=driver.chromium.launch(**options)
            try:
                page=browser.new_page()
                def respond(route):
                    request=route.request
                    path=request.url.split('https://clipforge.test',1)[-1].split('?',1)[0]
                    if path=='/api/state': route.fulfill(json=state)
                    elif path=='/api/accounts/unverified/check-session':
                        requests.append(path)
                        route.fulfill(status=409,json={'detail':'Войдите в TikTok и обновите cookie'})
                    elif path.startswith('/api/'):
                        if request.method!='GET': requests.append(path)
                        route.fulfill(json={})
                    elif path.startswith('/static/'):
                        file=root/path.lstrip('/')
                        route.fulfill(body=file.read_bytes(),content_type='text/javascript' if file.suffix=='.js' else 'text/css' if file.suffix=='.css' else 'image/svg+xml')
                    else: route.fulfill(body=html,content_type='text/html')
                page.route('**/*',respond)
                page.goto('https://clipforge.test/')
                self.assertFalse(page.locator('#automationAccounts option[value="unverified"]').get_attribute('disabled'))
                page.locator('#automationAccounts').select_option('unverified')
                page.locator('#automationAccountStatus').get_by_text('Войдите в TikTok и обновите cookie',exact=True).wait_for()
                page.locator('#automationConsent').check()
                page.locator('#automationStart').click()
                page.locator('#toast').get_by_text('Вход не подтверждён. Причина указана рядом с аккаунтом в блоке 24/7.',exact=True).wait_for()
                page.evaluate('refresh()')
                self.assertEqual(requests,['/api/accounts/unverified/check-session'])
                self.assertFalse(plan['enabled'])
                self.assertEqual(page.locator('#ttDirectForm').count(),0)
            finally: browser.close()

    def test_content_settings_are_separate_persistent_and_do_not_start_a_plan(self):
        root=Path(__file__).resolve().parents[1]/'app'
        html=templates.get_template('index.html').render(youtube_configured=True,tiktok_configured=False)
        plan={'enabled':True,'status':'scheduled','config':{'platform':'tiktok_web','caption':'Saved caption','hashtags':'#saved',
              'account_ids':[],'interval_minutes':1,'daily_limit':0,'clip_count':1,'clip_duration':15,'publish':True},'last_error':None}
        state={'accounts':[],'sources':[],'clips':[],'jobs':[],'automation':plan}
        mutations=[]
        with sync_playwright() as driver:
            options={'headless':True,'chromium_sandbox':True}
            if settings.browser_executable_path: options['executable_path']=settings.browser_executable_path
            browser=driver.chromium.launch(**options)
            try:
                page=browser.new_page(viewport={'width':1280,'height':900})
                def respond(route):
                    request=route.request
                    path=request.url.split('https://clipforge.test',1)[-1].split('?',1)[0]
                    if path=='/api/state': route.fulfill(json=copy.deepcopy(state))
                    elif path=='/api/automation/content' and request.method=='PATCH':
                        content=json.loads(request.post_data); mutations.append(content)
                        plan['config'].update(content)
                        route.fulfill(json=copy.deepcopy(plan))
                    elif path.startswith('/api/'):
                        if request.method!='GET': mutations.append({'unexpected_request':path})
                        route.fulfill(json={})
                    elif path.startswith('/static/'):
                        file=root/path.lstrip('/')
                        kind='text/javascript' if file.suffix=='.js' else 'text/css' if file.suffix=='.css' else 'image/svg+xml'
                        route.fulfill(body=file.read_bytes(),content_type=kind)
                    else: route.fulfill(body=html,content_type='text/html')
                page.route('**/*',respond)
                page.goto('https://clipforge.test/')
                page.get_by_role('heading',name='TikTok · режим 24/7',exact=True).wait_for()
                page.get_by_role('button',name='Настроить описание и хештеги',exact=True).click()
                dialog=page.get_by_role('dialog')
                self.assertEqual(page.locator('#automationCaption').input_value(),'Saved caption')
                page.locator('#automationCaption').fill('Independent 24/7 caption')
                page.locator('#automationHashtags').fill('#new #test')
                page.locator('#discoveryCaption').evaluate('(el)=>el.value="Unrelated YouTube caption"')
                page.wait_for_timeout(3500)  # The live state poll must not erase typing.
                self.assertEqual(page.locator('#automationCaption').input_value(),'Independent 24/7 caption')
                dialog.get_by_role('button',name='Сохранить',exact=True).click()
                page.wait_for_function('!document.querySelector("#automationContentDialog").open')
                self.assertEqual(mutations,[{'caption':'Independent 24/7 caption','hashtags':'#new #test'}])
                self.assertTrue(plan['enabled'])
                page.reload()
                page.get_by_role('button',name='Настроить описание и хештеги',exact=True).click()
                self.assertEqual(page.locator('#automationCaption').input_value(),'Independent 24/7 caption')
                dialog.get_by_role('button',name='Отмена',exact=True).click()
                plan.update(enabled=False,status='paused',last_error='Login required')
                page.reload(); page.wait_for_function('document.querySelector("#automationStatus").textContent==="Приостановлен"')
                self.assertEqual(page.locator('#automationInterval').get_attribute('min'),'1')
                self.assertIsNone(page.locator('#automationInterval').get_attribute('max'))
                self.assertEqual(page.locator('#automationDailyLimit').input_value(),'0')
                page.locator('#automationInterval').fill('2')
                page.locator('#automationDailyLimit').fill('100')
                self.assertTrue(page.locator('#automationInterval').evaluate('(el)=>el.checkValidity()'))
                self.assertTrue(page.locator('#automationDailyLimit').evaluate('(el)=>el.checkValidity()'))
                page.locator('#automationPlatform').select_option('youtube')
                self.assertEqual(page.locator('#automationTitle').inner_text(),'YouTube · режим 24/7')
                page.set_viewport_size({'width':390,'height':844})
                page.get_by_role('button',name='Настроить описание и хештеги',exact=True).click()
                box=dialog.bounding_box()
                self.assertGreaterEqual(box['x'],0)
                self.assertLessEqual(box['x']+box['width'],390)
                self.assertTrue(dialog.get_by_role('button',name='Сохранить',exact=True).is_visible())
            finally: browser.close()
