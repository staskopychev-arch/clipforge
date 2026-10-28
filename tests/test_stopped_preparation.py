"""Stopped work is terminal; explicit resume may restart untouched preparation."""
import unittest
from datetime import timedelta
from unittest.mock import Mock,patch
from sqlalchemy import select
from app import worker
from app.models import AutomationPlan,Job,JobType,RecordStatus,SourceVideo,SourceClipUse,utcnow
from app.config import settings
from app.services.source_identity import unavailable_source_keys,source_key
from app.services.scheduler import pipeline_jobs,stop_plan
from app.services.five_videos import start_batch,retryable_search
import test_pipeline as pipeline
import test_five_search as five


class StoppedWorkerTests(unittest.TestCase):
    setUp=pipeline.PipelineTests.setUp
    tearDown=pipeline.PipelineTests.tearDown

    def test_stopped_download_does_not_retry_or_call_downloader(self):
        with self.sessions() as db:
            db.add(AutomationPlan(id=1,enabled=False,status='paused',config={}))
            job=Job(type=JobType.download,payload={'automation_plan_id':1,'source_id':'unused'})
            db.add(job);db.commit()
        downloader=Mock()
        with patch.object(worker,'process_download') as download:
            self.assertTrue(worker.run_once(downloader))
            download.assert_not_called()
        with self.sessions() as db:
            saved=db.get(Job,job.id)
            self.assertEqual(saved.status,RecordStatus.error)
            self.assertEqual(saved.result['error_kind'],'automation_stopped')
            self.assertIsNone(saved.next_attempt_at)
        self.assertFalse(worker.run_once(downloader))

    def test_failure_after_plan_stop_has_no_new_retry(self):
        with self.sessions() as db:
            db.add(AutomationPlan(id=1,enabled=False,status='stopped',config={}))
            job=Job(type=JobType.download,status=RecordStatus.processing,attempts=1,payload={'automation_plan_id':1})
            db.add(job);db.commit()
        worker.fail_job(job.id,'Temporary connection failure')
        with self.sessions() as db:
            saved=db.get(Job,job.id)
            self.assertEqual(saved.status,RecordStatus.error)
            self.assertIsNone(saved.next_attempt_at)

    def test_archived_waiting_jobs_do_not_block_active_pipeline(self):
        with self.sessions() as db:
            root=Job(type=JobType.discovery,status=RecordStatus.ready,payload={})
            db.add(root);db.flush()
            db.add(Job(type=JobType.download,archived=True,payload={'pipeline_id':root.id}))
            db.commit()
            self.assertEqual([j.id for j in pipeline_jobs(db,root.id)],[root.id])

    def test_exhausted_download_is_skipped_temporarily_in_new_search(self):
        with self.sessions() as db:
            source=SourceVideo(source_url='https://youtu.be/failed-source');db.add(source);db.flush()
            job=Job(type=JobType.download,status=RecordStatus.error,attempts=settings.max_job_attempts,
                payload={'source_id':source.id},result={'error_kind':'download_failed'},finished_at=utcnow())
            db.add(job);db.commit()
            self.assertIn(source_key(source.source_url),unavailable_source_keys(db))
            job.finished_at=utcnow()-timedelta(minutes=31);db.commit()
            self.assertNotIn(source_key(source.source_url),unavailable_source_keys(db))


class PreparationRestartTests(unittest.TestCase):
    setUp=five.FiveSearchTests.setUp
    tearDown=five.FiveSearchTests.tearDown
    draft=five.FiveSearchTests.draft
    prepare=five.FiveSearchTests.prepare

    def failed_download(self,db,kind='automation_stopped',legacy=False):
        plan,root=self.prepare(db)
        source=SourceVideo(source_url='https://youtu.be/untouched-fixture',rights_confirmed=True)
        db.add(source);db.flush()
        root.status=RecordStatus.ready;root.result={'created_source_ids':[source.id]}
        job=Job(type=JobType.download,status=RecordStatus.error,
            payload={'pipeline_id':root.id,'source_id':source.id,'auto_render':{'editing':{}}},
            result={'error_kind':kind} if kind else {},
            error='Режим 24/7 остановлен; новая операция не запущена' if legacy else 'Stopped')
        db.add(job);plan.enabled=False;plan.status='paused';db.commit()
        return plan,root,job,source

    def test_cancelled_download_allows_explicit_new_preparation_and_preserves_evidence(self):
        with self.sessions() as db:
            plan,root,job,source=self.failed_download(db)
            batch_id=plan.config['five_video_batch']['id']
            start_batch(db,**self.options)
            self.assertEqual(plan.config['five_video_batch']['id'],batch_id)
            self.assertIsNone(plan.config['five_video_batch']['prepare_pipeline_id'])
            self.assertEqual(db.get(Job,job.id).status,RecordStatus.error)
            self.assertEqual(len(db.scalars(select(Job)).all()),2)

    def test_legacy_stop_error_without_kind_is_retryable(self):
        with self.sessions() as db:
            plan,root,job,source=self.failed_download(db,kind=None,legacy=True)
            self.assertTrue(retryable_search(db,plan.config['five_video_batch']))

    def test_exhausted_provider_failure_allows_explicit_new_preparation(self):
        with self.sessions() as db:
            plan,root,job,source=self.failed_download(db,kind=None)
            job.attempts=settings.max_job_attempts
            job.error='Ни один загрузчик не выдал корректное видео. Попытки: cnvmp3=error, y2save=error'
            db.commit()
            self.assertTrue(retryable_search(db,plan.config['five_video_batch']))
            start_batch(db,**self.options)
            self.assertIsNone(plan.config['five_video_batch']['prepare_pipeline_id'])

    def test_unusable_excerpt_can_restart_after_editing_settings_change(self):
        from app.services.editing import update_editing,EditingOptions
        with self.sessions() as db:
            plan,root,job,source=self.failed_download(db,kind='first_result_unusable')
            self.assertFalse(retryable_search(db,plan.config['five_video_batch']))
            update_editing(db,EditingOptions(automatic=True))
            self.assertTrue(retryable_search(db,plan.config['five_video_batch']))

    def test_security_failure_is_not_cleared_by_cancel_message(self):
        with self.sessions() as db:
            plan,root,job,source=self.failed_download(db,kind='manual_verification',legacy=True)
            self.assertFalse(retryable_search(db,plan.config['five_video_batch']))
            with self.assertRaises(ValueError):start_batch(db,**self.options)

    def test_reserved_source_cannot_restart_preparation(self):
        with self.sessions() as db:
            plan,root,job,source=self.failed_download(db)
            db.add(SourceClipUse(source_key='youtube:untouched-fixture',source_id=source.id,job_id=job.id));db.commit()
            self.assertFalse(retryable_search(db,plan.config['five_video_batch']))

    def test_in_progress_download_still_blocks_start(self):
        with self.sessions() as db:
            plan,root,job,source=self.failed_download(db)
            job.status=RecordStatus.processing;db.commit()
            with self.assertRaisesRegex(ValueError,'текущей операции'):start_batch(db,**self.options)
