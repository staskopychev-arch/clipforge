import json,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from app import main
from app.config import settings
from app.db import Base,get_db
from app.models import Account,AutomationPlan,Job,JobType,RecordStatus,TikTokWebPublication
from app.security import create_session,decrypt
from app.services.cookie_import import parse_tiktok_cookie_text
from app.services.tiktok_web import TikTokWebSessionError,TikTokWebPolicyError


class CookiePasteTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.engine=create_engine(f'sqlite:///{Path(self.folder.name)/"test.db"}',connect_args={'check_same_thread':False})
        Base.metadata.create_all(self.engine)
        self.sessions=sessionmaker(self.engine,expire_on_commit=False)
        def database():
            with self.sessions() as db: yield db
        main.app.dependency_overrides[get_db]=database
        self.client=TestClient(main.app)
        self.client.cookies.set('clipforge_session',create_session(settings.admin_username))
        self.secret='FIXTURE_PRIVATE_VALUE'
        self.cookie={'name':'sessionid','value':self.secret,'domain':'.tiktok.com','sameSite':'no_restriction'}

    def tearDown(self):
        self.client.close(); main.app.dependency_overrides.clear()
        self.engine.dispose(); self.folder.cleanup()

    def save(self,text=None,**extra):
        return self.client.post('/api/tiktok-web/cookies/paste',json={'text':text or json.dumps([self.cookie]),'owns_account':True,**extra})

    def test_supported_pasted_formats_preserve_values_and_host_scopes(self):
        for value in ([self.cookie],{'cookies':[self.cookie]}, {'accounts':[{'cookies':[self.cookie]}]}):
            parsed=parse_tiktok_cookie_text(json.dumps(value))
            self.assertEqual(parsed[0]['value'],self.secret)
        self.assertEqual(parse_tiktok_cookie_text('Cookie: sessionid=a=b%3D; sid_guard=abc%7C123')[0]['value'],'a=b%3D')
        cookies=parse_tiktok_cookie_text('# Netscape HTTP Cookie File\n#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tsecret')
        self.assertTrue(cookies[0]['httpOnly']); self.assertNotIn('expires',cookies[0])
        self.assertEqual(len(parse_tiktok_cookie_text(json.dumps([self.cookie,{**self.cookie,'domain':'tiktok.com'}]))),2)

    def test_invalid_domains_and_malformed_text_are_rejected_without_echo(self):
        for text in ('bad '+self.secret,'[{"value":"'+self.secret+'"}]',
                     json.dumps([{**self.cookie,'domain':'tiktok.com.attacker.test'}]),
                     json.dumps([self.cookie,{**self.cookie,'domain':'.google.com'}]),
                     json.dumps([{**self.cookie,'expirationDate':'NaN'}]),
                     json.dumps([self.cookie,self.cookie])):
            response=self.save(text)
            self.assertEqual(response.status_code,400,response.text)
            self.assertNotIn(self.secret,response.text)

    def test_create_encrypts_session_and_duplicate_paste_uses_same_record(self):
        first=self.save(display_name='Owned fixture')
        self.assertEqual(first.status_code,200,first.text)
        second=self.save()
        self.assertEqual(first.json()['account_id'],second.json()['account_id'])
        self.assertFalse(second.json()['created'])
        self.assertNotIn(self.secret,first.text)
        with self.sessions() as db:
            accounts=db.scalars(select(Account)).all()
            self.assertEqual(len(accounts),1)
            self.assertNotIn(self.secret,accounts[0].access_token)
            self.assertEqual(json.loads(decrypt(accounts[0].access_token))['cookies'][0]['value'],self.secret)
            self.assertEqual(accounts[0].status,RecordStatus.reauth_required)
            self.assertEqual(db.scalars(select(Job)).all(),[])
        self.assertNotIn(self.secret,self.client.get('/api/state').text)

    def test_refresh_keeps_account_id_plan_and_receipts(self):
        aid=self.save().json()['account_id']
        with self.sessions() as db:
            job=Job(type=JobType.tiktok_web_publish,status=RecordStatus.ready,payload={'account_id':aid})
            db.add(job); db.flush()
            receipt=TikTokWebPublication(account_id=aid,job_id=job.id,file_hash='retained-hash',stage='published',video_url='https://www.tiktok.com/@fixture/video/1234567890123456789')
            plan=AutomationPlan(id=1,enabled=False,status='paused',config={'account_ids':[aid],'account_index':0,'query_index':4},last_pipeline_id=job.id,last_error='Login required')
            db.add_all([receipt,plan]); db.commit(); rid=receipt.id; jid=job.id
        response=self.save('sessionid=NEW_FIXTURE_VALUE',account_id=aid)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['account_id'],aid)
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(Account)).all()),1)
            self.assertEqual(db.get(TikTokWebPublication,rid).file_hash,'retained-hash')
            plan=db.get(AutomationPlan,1)
            self.assertFalse(plan.enabled); self.assertEqual(plan.status,'paused')
            self.assertEqual(plan.last_pipeline_id,jid)
            self.assertEqual(plan.config['query_index'],4)
            self.assertEqual(db.get(Account,aid).display_name,'TikTok · новый аккаунт')

    def test_active_target_is_not_replaced(self):
        aid=self.save().json()['account_id']
        with self.sessions() as db:
            db.add(Job(type=JobType.render,payload={'tiktok_web_account_id':aid},status=RecordStatus.processing)); db.commit()
            previous=db.get(Account,aid).access_token
        self.assertEqual(self.save('sessionid=REPLACEMENT',account_id=aid).status_code,409)
        with self.sessions() as db: self.assertEqual(db.get(Account,aid).access_token,previous)

    def test_validation_ownership_and_authentication_do_not_echo_input(self):
        for extra in ({'owns_account':False},{'account_id':'missing'},{'display_name':'x'*256}):
            response=self.save(**extra)
            self.assertIn(response.status_code,(400,404)); self.assertNotIn(self.secret,response.text)
        self.client.cookies.clear()
        self.assertEqual(self.save().status_code,401)

    def start_config(self,aid):
        return {'platform':'tiktok_web','topic':'any','queries':['bread recipe'],'account_ids':[aid],
                'interval_minutes':1,'daily_limit':0,'publish':True,'consent':True,'owns_channels':True}

    def test_247_start_checks_unverified_session_before_enabling_without_publishing(self):
        aid=self.save().json()['account_id']
        with patch.object(main.settings,'youtube_api_key','fixture-key'),patch.object(main,'check_tiktok_web_session',return_value={'authenticated':True}) as check:
            response=self.client.post('/api/automation/start',json=self.start_config(aid))
        self.assertEqual(response.status_code,200,response.text)
        check.assert_called_once()
        with self.sessions() as db:
            self.assertEqual(db.get(Account,aid).status,RecordStatus.ready)
            self.assertTrue(db.get(AutomationPlan,1).enabled)
            self.assertEqual(db.scalars(select(Job)).all(),[])

    def test_247_failed_check_preserves_pause_and_never_queues_uploads(self):
        for failure in (TikTokWebSessionError('Login required'),TikTokWebPolicyError('30522')):
            aid=self.save().json()['account_id']
            with self.sessions() as db:
                plan=db.get(AutomationPlan,1)
                if plan is None: db.add(AutomationPlan(id=1,enabled=False,status='paused',config={'account_index':2,'query_index':8},last_error='Preserved pause'))
                db.commit()
            with patch.object(main.settings,'youtube_api_key','fixture-key'),patch.object(main,'check_tiktok_web_session',side_effect=failure):
                response=self.client.post('/api/automation/start',json=self.start_config(aid))
            self.assertEqual(response.status_code,409,response.text)
            with self.sessions() as db:
                plan=db.get(AutomationPlan,1)
                self.assertFalse(plan.enabled); self.assertEqual(plan.status,'paused')
                self.assertEqual(plan.config['query_index'],8)
                self.assertEqual(db.scalars(select(Job)).all(),[])

    def test_stale_check_cannot_mark_replaced_cookie_ready(self):
        aid=self.save().json()['account_id']
        def replace_during_check(_):
            with self.sessions() as db:
                account=db.get(Account,aid)
                account.access_token='REPLACED_ENCRYPTED_FIXTURE'
                account.status=RecordStatus.reauth_required
                db.commit()
            return {'authenticated':True}
        with patch.object(main,'check_tiktok_web_session',side_effect=replace_during_check):
            response=self.client.post(f'/api/accounts/{aid}/check-session')
        self.assertEqual(response.status_code,409,response.text)
        with self.sessions() as db:
            self.assertEqual(db.get(Account,aid).status,RecordStatus.reauth_required)
            self.assertEqual(db.get(Account,aid).access_token,'REPLACED_ENCRYPTED_FIXTURE')
