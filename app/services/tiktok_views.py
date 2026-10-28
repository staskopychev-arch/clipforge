"""Read counts delivered to the ordinary Studio Posts screen; never publish.

Only the observed item-list response is inspected. No private endpoint calls,
signed request construction, raw response storage or session logging.
"""
import re
import time
from urllib.parse import urlsplit
from .tiktok_web import tiktok_page,check_challenge,TikTokWebError

CONTENT_URL='https://www.tiktok.com/tiktokstudio/content?lang=en'
ITEM_PATH='/tiktok/creator/manage/item_list/v1/'

class ViewsReadError(TikTokWebError):pass

class ViewPages:
    def __init__(self):
        self.items={};self.cursor=-1;self.complete=False;self.received=False;self.bad=False;self.updated=0
    def accept(self,data):
        if not isinstance(data,dict) or data.get('status_code') not in (0,'0'):
            self.bad=True;return
        more=data.get('has_more');cursor=data.get('cursor')
        if not isinstance(more,bool) or isinstance(cursor,bool) or not str(cursor).isdigit():
            self.bad=True;return
        items=data.get('item_list',[])
        if not isinstance(items,list):self.bad=True;return
        for item in items:
            if not isinstance(item,dict):self.bad=True;continue
            identity=str(item.get('item_id',''));value=item.get('play_count')
            if not re.fullmatch(r'[0-9]{10,25}',identity):self.bad=True;continue
            valid=not isinstance(value,bool) and str(value).isdigit() and int(value)<=9223372036854775807
            self.items[identity]=int(value) if valid else None
        if int(cursor)>=self.cursor:
            self.cursor=int(cursor);self.complete=not more and not data.get('is_limited',False)
        self.received=True;self.updated=time.monotonic()
    def result(self,profile=None):
        if not self.received or self.bad:raise ViewsReadError('Не удалось однозначно прочитать статистику Studio')
        known=[v for v in self.items.values() if v is not None]
        if self.items and not known:raise ViewsReadError('Studio не передал счётчики просмотров')
        return {'views':sum(known),'video_count':len(known),'approximate':False,
            'complete':self.complete and len(known)==len(self.items),'profile_url':profile}

def collect_account_views(encrypted_session,*,timeout_seconds=90):
    pages=ViewPages()
    with tiktok_page(encrypted_session) as page:
        def observe(response):
            parsed=urlsplit(response.url)
            if parsed.scheme!='https' or parsed.hostname not in {'www.tiktok.com','tiktok.com'} or parsed.path!=ITEM_PATH:return
            if 'json' not in response.headers.get('content-type',''):return
            try:pages.accept(response.json())
            except Exception:pages.bad=True
        page.on('response',observe)
        page.add_locator_handler(page.get_by_role('button',name='Decline optional cookies',exact=True),lambda element:element.click())
        try:
            page.goto(CONTENT_URL,wait_until='domcontentloaded')
            deadline=time.monotonic()+timeout_seconds
            while time.monotonic()<deadline:
                check_challenge(page)
                if pages.bad:raise ViewsReadError('Формат статистики Studio изменился')
                if pages.received and pages.complete and time.monotonic()-pages.updated>=2:break
                if pages.received and not pages.complete:
                    # Posts uses a virtualized scrolling list. Scroll its actual
                    # ancestors to let Studio request the following page itself.
                    page.locator('[data-tt="components_PostTable_Container"]').evaluate_all('''elements=>{
                        for(let e of elements){for(let p=e;p;p=p.parentElement){
                            if(p.scrollHeight>p.clientHeight+10)p.scrollTop=p.scrollHeight;
                        }} window.scrollTo(0,document.documentElement.scrollHeight);
                    }''')
                if len(pages.items)>10000:break
                page.wait_for_timeout(500)
            check_challenge(page)
            profiles=set()
            for href in page.locator('a[href*="/video/"]').evaluate_all('els=>els.map(e=>e.href)'):
                if re.fullmatch(r'https://www\.tiktok\.com/@[A-Za-z0-9_.]{1,64}/video/[0-9]{10,25}',href):profiles.add(href.split('/video/')[0])
            if len(profiles)>1:raise ViewsReadError('Studio показал неоднозначный профиль')
            return pages.result(next(iter(profiles),None))
        finally:page.remove_listener('response',observe)
