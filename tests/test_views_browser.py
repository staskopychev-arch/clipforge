import contextlib,json,os,unittest
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright,expect
from app.config import settings
from app.main import templates
from app.services.editing import EditingOptions
from app.services import tiktok_views

@unittest.skipUnless(os.environ.get('RUN_BROWSER_TIKTOK_TESTS')=='1','Opt-in statistics browser fixtures')
class ViewsBrowserTests(unittest.TestCase):
    def browser(self,p):
        args={'headless':True,'chromium_sandbox':True}
        if settings.browser_executable_path:args['executable_path']=settings.browser_executable_path
        return p.chromium.launch(**args)
    def test_collector_uses_all_pages_deduplicates_and_never_posts(self):
        with sync_playwright() as p:
            browser=self.browser(p)
            try:
                page=browser.new_page();requests=[]
                html='''<div data-tt="components_PostTable_Container" style="height:100px;overflow:auto"><div style="height:800px"><a href="https://www.tiktok.com/@fixture/video/7701067153495035158">Video</a><p>Posts (Created on) Views Likes Comments</p></div></div>
                <script>let sent=false; const load=cursor=>fetch('/tiktok/creator/manage/item_list/v1/?cursor='+cursor).then(r=>r.json());load(0);document.querySelector('[data-tt]').addEventListener('scroll',()=>{if(!sent){sent=true;load(2);}});</script>'''
                def route(r):
                    requests.append((r.request.method,r.request.url))
                    if '/item_list/' in r.request.url:
                        second='cursor=2' in r.request.url
                        items=[{'item_id':'7701067153495035158','play_count':'1234'}, {'item_id':'7701067726542097686','play_count':'456'}] if not second else [{'item_id':'7701067726542097686','play_count':'456'},{'item_id':'7701067963054574851','play_count':'10'}]
                        r.fulfill(json={'status_code':0,'has_more':not second,'cursor':50 if second else 2,'item_list':items})
                    else:r.fulfill(body=html,content_type='text/html')
                page.route('**/*',route)
                @contextlib.contextmanager
                def fixture(token):yield page
                with patch.object(tiktok_views,'tiktok_page',fixture):result=tiktok_views.collect_account_views('private-fixture',timeout_seconds=10)
                self.assertEqual(result,{'views':1700,'video_count':3,'approximate':False,'complete':True,'profile_url':'https://www.tiktok.com/@fixture'})
                self.assertTrue(all(method=='GET' for method,url in requests))
            finally:browser.close()
    def test_tab_zero_missing_values_filter_refresh_and_mobile(self):
        root=Path(__file__).resolve().parents[1]/'app'
        html=templates.get_template('index.html').render(youtube_configured=True,tiktok_configured=False)
        state={'accounts':[],'sources':[],'clips':[],'jobs':[],'automation':{'enabled':False,'status':'stopped','config':{}},'editing':{'settings':EditingOptions().model_dump(),'tracks':[]}}
        data={'enabled':True,'checking':0,'accounts':[
            {'account_id':'a','display_name':'<img src=x onerror=alert(1)>','views':0,'video_count':2,'complete':True,'status':'ready','checked_at':'2026-10-27T12:00:00Z','next_check_at':'2026-10-27T13:00:00Z'},
            {'account_id':'b','display_name':'Waiting account','views':None,'video_count':None,'status':'needs_login'}]}
        calls=[]
        with sync_playwright() as p:
            browser=self.browser(p)
            try:
                page=browser.new_page(viewport={'width':1440,'height':1100});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                def respond(route):
                    path=route.request.url.split('https://clipforge.test',1)[-1].split('?',1)[0]
                    if path=='/api/state':route.fulfill(json=state)
                    elif path.startswith('/api/account-views'):
                        calls.append((route.request.method,path));route.fulfill(json=data)
                    elif path=='/api/account-logins':route.fulfill(json={'records':[],'running':False,'waiting':False,'message':''})
                    elif path.startswith('/api/'):route.fulfill(json={})
                    elif path.startswith('/static/'):
                        file=root/path.lstrip('/');route.fulfill(body=file.read_bytes(),content_type='text/javascript' if file.suffix=='.js' else 'text/css' if file.suffix=='.css' else 'image/svg+xml')
                    else:route.fulfill(body=html,content_type='text/html')
                page.route('**/*',respond);page.goto('https://clipforge.test/')
                expect(page.locator('#viewsPanel')).to_be_hidden();self.assertEqual(calls,[])
                page.locator('#viewsTab').click();expect(page.locator('#viewsPanel')).to_be_visible()
                expect(page.locator('#viewsRows tr')).to_have_count(2)
                expect(page.locator('#viewsRows tr').nth(0).locator('td').nth(1)).to_have_text('0')
                expect(page.locator('#viewsRows tr').nth(1).locator('td').nth(1)).to_have_text('—')
                self.assertEqual(page.locator('#viewsRows img').count(),0)
                page.locator('#viewsFilter').fill('Waiting');expect(page.locator('#viewsRows tr')).to_have_count(1)
                page.locator('#viewsFilter').fill('');page.locator('#viewsRefresh').click()
                expect(page.locator('#toast')).to_contain_text('поставлена в очередь')
                self.assertEqual(sum(method=='POST' for method,url in calls),1)
                page.set_viewport_size({'width':390,'height':844})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'),391)
                self.assertEqual(errors,[])
            finally:browser.close()
