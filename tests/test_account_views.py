import json,tempfile,unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.models import Account,AccountViews,AccountViewSample,Job,JobType,RecordStatus,utcnow
from app.services.account_views import run_due_once,views_state,aware
from app.services.tiktok_views import ViewPages,ViewsReadError
from app.services.tiktok_web import TikTokWebSessionError

def page(items,more=False,cursor=50,**extra):
    return {'status_code':0,'has_more':more,'cursor':cursor,'item_list':[{'item_id':str(i),'play_count':v} for i,v in items],**extra}

class ViewPageTests(unittest.TestCase):
    def test_pages_deduplicate_prefetch_repeated_responses_and_retain_exact_counts(self):
        pages=ViewPages();pages.accept(page([(7701067153495035158,'1234')],True,1))
        pages.accept(page([(7701067153495035158,'1234'),(7701067726542097686,'456')]))
        pages.accept(page([(7701067153495035158,'1234'),(7701067726542097686,'456')]))
        pages.accept(page([(7701067153495035158,'1234')],True,1))
        self.assertEqual(pages.result()['views'],1690);self.assertEqual(pages.result()['video_count'],2)
        self.assertTrue(pages.result()['complete'])
    def test_missing_counter_partial_page_and_known_empty_are_distinct(self):
        p=ViewPages()
        with self.assertRaises(ViewsReadError):p.result()
        p.accept(page([]));self.assertEqual(p.result()['views'],0);self.assertTrue(p.result()['complete'])
        p=ViewPages();p.accept(page([(7701067153495035158,'9'),(7701067726542097686,None)]))
        self.assertEqual(p.result()['views'],9);self.assertFalse(p.result()['complete'])
        p=ViewPages();p.accept(page([(7701067153495035158,'12')],True))
        self.assertFalse(p.result()['complete'])
    def test_restricted_or_unknown_reply_never_becomes_zero(self):
        for payload in ({'status_code':30522}, {'status_code':0,'item_list':[]},page([(7701067153495035158,'1.2K')])):
            p=ViewPages();p.accept(payload)
            with self.assertRaises(ViewsReadError):p.result()

class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.engine=create_engine('sqlite:///'+str(Path(self.temp.name)/'test.db'))
        Base.metadata.create_all(self.engine);self.sessions=sessionmaker(bind=self.engine,expire_on_commit=False)
        with self.sessions() as db:
            a=Account(platform='tiktok_web',external_id='stats-fixture',display_name='Fixture',access_token='secret-not-for-response',refresh_token='',status=RecordStatus.ready)
            db.add(a);db.commit();self.aid=a.id
        self.collect=Mock(return_value={'views':100,'video_count':2,'complete':True,'profile_url':'https://www.tiktok.com/@fixture'})
    def tearDown(self):self.engine.dispose();self.temp.cleanup()
    def test_hourly_persistence_no_publication_jobs_and_restart_does_not_repeat(self):
        now=utcnow()
        self.assertTrue(run_due_once(self.sessions,self.collect,now))
        self.assertFalse(run_due_once(self.sessions,self.collect,now+timedelta(minutes=59)))
        self.assertTrue(run_due_once(self.sessions,self.collect,now+timedelta(hours=1,seconds=1)))
        self.assertEqual(self.collect.call_count,2)
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(AccountViewSample)).all()),2)
            self.assertEqual(db.scalars(select(Job)).all(),[])
            state=views_state(db);self.assertEqual(state['accounts'][0]['views'],100)
            self.assertNotIn('secret-not-for-response',json.dumps(state));self.assertEqual(state['interval_seconds'],3600)
    def test_failed_refresh_keeps_last_success_and_requires_new_session_after_auth_error(self):
        now=utcnow();run_due_once(self.sessions,self.collect,now)
        self.collect.side_effect=TikTokWebSessionError('verification')
        run_due_once(self.sessions,self.collect,now+timedelta(hours=1,seconds=1))
        with self.sessions() as db:
            r=db.get(AccountViews,self.aid);self.assertEqual(r.views,100);self.assertEqual(r.status,'needs_login')
        self.assertFalse(run_due_once(self.sessions,self.collect,now+timedelta(hours=3)))
        with self.sessions() as db:db.get(Account,self.aid).access_token='new-session';db.commit()
        self.collect.side_effect=None
        self.assertTrue(run_due_once(self.sessions,self.collect,now+timedelta(hours=3)))
    def test_active_upload_defers_check_and_expired_lease_is_recovered(self):
        now=utcnow()
        with self.sessions() as db:
            job=Job(type=JobType.tiktok_web_publish,payload={'account_id':self.aid},status=RecordStatus.processing)
            db.add(job);db.commit();jid=job.id
        self.assertFalse(run_due_once(self.sessions,self.collect,now));self.collect.assert_not_called()
        with self.sessions() as db:
            db.get(Job,jid).status=RecordStatus.ready
            r=db.get(AccountViews,self.aid);r.status='checking';r.lease_token='old';r.lease_until=now-timedelta(minutes=1);r.next_check_at=now+timedelta(hours=1)
            db.commit()
        self.assertTrue(run_due_once(self.sessions,self.collect,now))
    def test_changed_cookie_during_check_discards_old_result(self):
        def replaced(token):
            with self.sessions() as db:db.get(Account,self.aid).access_token='replacement';db.commit()
            return {'views':99,'video_count':2,'complete':True}
        run_due_once(self.sessions,replaced)
        with self.sessions() as db:
            r=db.get(AccountViews,self.aid);self.assertIsNone(r.views);self.assertEqual(r.status,'pending')
    def test_live_lease_prevents_second_collector(self):
        now=utcnow()
        from app.services.account_views import ensure_rows
        with self.sessions() as db:
            ensure_rows(db);r=db.get(AccountViews,self.aid);r.status='checking';r.lease_until=now+timedelta(minutes=2);db.commit()
        self.assertFalse(run_due_once(self.sessions,self.collect,now));self.collect.assert_not_called()


import test_pipeline as baseline
class ViewsAPITests(unittest.TestCase):
    setUp=baseline.PipelineTests.setUp
    tearDown=baseline.PipelineTests.tearDown
    def test_read_and_refresh_are_authenticated_and_do_not_create_uploads(self):
        with self.sessions() as db:
            a=Account(platform='tiktok_web',external_id='views-api-fixture',display_name='Analytics account',
                access_token='never-return-this-session',refresh_token='',status=RecordStatus.ready)
            db.add(a);db.commit()
        response=self.client.get('/api/account-views');self.assertEqual(response.status_code,200)
        self.assertIsNone(response.json()['accounts'][0]['views'])
        response=self.client.post('/api/account-views/refresh');self.assertEqual(response.status_code,200)
        self.assertNotIn('never-return-this-session',response.text)
        with self.sessions() as db:self.assertEqual(db.scalars(select(Job)).all(),[])
        self.client.cookies.clear();self.assertEqual(self.client.get('/api/account-views').status_code,401)
