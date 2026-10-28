import copy,json,os,unittest
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from app.config import settings
from app.main import templates
from app.services.editing import EditingOptions


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TIKTOK_TESTS')=='1','Opt-in browser fixture')
class EditingUITests(unittest.TestCase):
    def test_automatic_audio_without_music_controls_and_save_before_render(self):
        root=Path(__file__).resolve().parents[1]/'app'
        html=templates.get_template('index.html').render(youtube_configured=True,tiktok_configured=False)
        fixture={'accounts':[],'sources':[{'id':'fixture-source','status':'ready','title':'Bread video'}],
            'clips':[],'jobs':[],'automation':{'enabled':False,'status':'stopped','config':{}},
            'editing':{'settings':EditingOptions().model_dump(),'tracks':[]}}
        calls=[]
        with sync_playwright() as p:
            options={'headless':True,'chromium_sandbox':True}
            if settings.browser_executable_path:options['executable_path']=settings.browser_executable_path
            browser=p.chromium.launch(**options)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def respond(route):
                    req=route.request;path=req.url.split('https://clipforge.test',1)[-1].split('?',1)[0]
                    if path=='/api/state':route.fulfill(json=fixture)
                    elif path=='/api/editing':
                        calls.append('save');fixture['editing']['settings']=json.loads(req.post_data)
                        route.fulfill(json=fixture['editing'])
                    elif path=='/api/render':calls.append('render');route.fulfill(json={'job_id':'fixture-render'})
                    elif path=='/api/account-logins':route.fulfill(json={'records':[],'running':False,'waiting':False,'message':''})
                    elif path.startswith('/api/'):route.fulfill(json={})
                    elif path.startswith('/static/'):
                        file=root/path.lstrip('/');route.fulfill(body=file.read_bytes(),content_type='text/javascript' if file.suffix=='.js' else 'text/css' if file.suffix=='.css' else 'image/svg+xml')
                    else:route.fulfill(body=html,content_type='text/html')
                page.route('**/*',respond);page.goto('https://clipforge.test/')
                expect(page.locator('#editStart')).to_have_value('0')
                page.locator('#editStart').fill('2');page.locator('#editEnd').fill('12')
                page.locator('#editSpeed').select_option('1.5');page.locator('#editQuality').select_option('720p')
                page.evaluate('refresh()');expect(page.locator('#editStart')).to_have_value('2')
                self.assertEqual(page.locator('#musicFile,#musicUpload,#editMusic,#musicPreview').count(),0)
                page.locator('#editQuality').select_option('480p')
                page.locator('#renderForm button').click()
                expect(page.locator('#editStatus')).to_contain_text('Настройки сохранены')
                expect(page.locator('#toast')).to_contain_text('очередь')
                self.assertEqual(calls,['save','render'])
                self.assertEqual(fixture['editing']['settings'],{'automatic':False,'start_seconds':2,'end_seconds':12,'speed':1.5,'quality':'480p','music_id':None})
                page.reload();expect(page.locator('#editEnd')).to_have_value('12')
                page.locator('#editAutomatic').check()
                expect(page.locator('#manualEditingFields')).to_be_hidden()
                expect(page.locator('#editAutomaticSummary')).to_contain_text('0,95–1,05× без 1×')
                page.locator('#editSave').click()
                expect(page.locator('#editStatus')).to_contain_text('Загружать или выбирать музыку не нужно')
                page.reload();expect(page.locator('#editAutomatic')).to_be_checked()
                expect(page.locator('#manualEditingFields')).to_be_hidden()
                page.set_viewport_size({'width':390,'height':844})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),391)
                self.assertEqual(errors,[])
            finally:browser.close()
