"""Best-of-twenty result selection and durable source ownership, without network."""
import unittest
from unittest.mock import patch
from sqlalchemy import select
from app import worker
from app.models import Clip, Job, JobType, RecordStatus, SourceVideo, SourceClipUse, utcnow
from app.services.source_identity import FirstResultError, source_key
from app.services.youtube import SearchResults, YouTubeNoResultsError
import test_pipeline as baseline


class FirstResultTests(unittest.TestCase):
    setUp = baseline.PipelineTests.setUp
    tearDown = baseline.PipelineTests.tearDown

    def discovery(self):
        with self.sessions() as db:
            job = Job(type=JobType.discovery, payload={'query': 'bread recipe', 'clip_duration': 5})
            db.add(job); db.commit()
            return job

    def used(self, url, archived=False):
        with self.sessions() as db:
            source = SourceVideo(source_url=url, archived=archived, rights_confirmed=True)
            db.add(source); db.flush()
            db.add(Clip(source_id=source.id, start_seconds=0, duration_seconds=5))
            db.commit()

    def test_best_title_wins_over_first_result(self):
        job = self.discovery()
        found = [{'url': 'https://youtu.be/first-result', 'title': 'First', 'channel': 'Author'},
                 {'url': 'https://youtu.be/second-result', 'title': 'Bread recipe', 'channel': 'Author'}]
        with patch.object(worker, 'search_youtube', return_value=found) as search:
            worker.process_discovery(job)
            search.assert_called_once_with('bread recipe', 20, 'any')
        with self.sessions() as db:
            self.assertEqual([s.source_url for s in db.scalars(select(SourceVideo))], [found[1]['url']])
            self.assertNotIn('fallback_candidates', db.scalar(select(Job).where(Job.type == JobType.download)).payload)

    def test_used_first_result_is_skipped_for_fresh_second(self):
        self.used('https://youtube.com/shorts/first-result', archived=True)
        job = self.discovery()
        found = [{'url': 'https://youtu.be/first-result', 'title': 'First', 'channel': 'Author'},
                 {'url': 'https://youtu.be/fresh', 'title': 'Fresh', 'channel': 'Author'}]
        with patch.object(worker, 'search_youtube', return_value=found):
            result = worker.process_discovery(job)
        self.assertEqual(result['selected_position'], 2)
        with self.sessions() as db:
            source = db.get(SourceVideo, result['created_source_ids'][0])
            self.assertEqual(source.source_url, found[1]['url'])

    def test_equivalent_urls_have_same_identity(self):
        urls = ['https://youtu.be/abcdefghijk?t=4', 'https://www.youtube.com/watch?v=abcdefghijk&list=xyz',
                'https://m.youtube.com/shorts/abcdefghijk', 'https://youtube.com/embed/abcdefghijk']
        self.assertEqual({source_key(u) for u in urls}, {'youtube:abcdefghijk'})

    def test_successive_jobs_select_first_second_third_and_do_not_repeat_pending_downloads(self):
        found = [{'url': f'https://youtu.be/ordered-{i}', 'title': f'Recipe {i}', 'channel': 'Author'} for i in range(3)]
        selected = []
        for position in range(1, 4):
            job = self.discovery()
            with patch.object(worker, 'search_youtube', return_value=found):
                result = worker.process_discovery(job)
            self.assertEqual(result['selected_position'], position)
            with self.sessions() as db:
                selected.append(db.get(SourceVideo, result['created_source_ids'][0]).source_url)
        self.assertEqual(selected, [item['url'] for item in found])

    def test_exclusions_apply_to_equivalent_urls_before_selecting_third(self):
        self.used('https://youtube.com/shorts/ordered-1')
        job = self.discovery()
        job.payload = {**job.payload, 'exclude_source_urls': ['https://youtu.be/ordered-0']}
        found = [{'url': f'https://www.youtube.com/watch?v=ordered-{i}', 'title': 'Recipe', 'channel': 'Author'} for i in range(3)]
        with patch.object(worker, 'search_youtube', return_value=found):
            result = worker.process_discovery(job)
        self.assertEqual(result['selected_position'], 3)
        self.assertEqual(result['search_attempts'][0]['skipped'], 2)

    def test_first_result_can_win_and_is_not_automatically_excluded(self):
        found = [{'url': 'https://youtu.be/first', 'title': 'Bread recipe', 'channel': 'Author'},
                 {'url': 'https://youtu.be/second', 'title': 'Other recipe', 'channel': 'Author'}]
        with patch.object(worker, 'search_youtube', return_value=found):
            result = worker.process_discovery(self.discovery())
        self.assertEqual(result['selected_position'], 1)
        self.assertEqual(result['selection_policy'], 'best_of_20')

    def test_only_first_twenty_are_compared_without_pagination(self):
        for i in range(20): self.used(f'https://youtu.be/used-{i}', archived=True)
        found = SearchResults([{'url': f'https://youtu.be/used-{i}', 'title': 'Old', 'channel': 'Author'} for i in range(20)] +
                              [{'url': 'https://youtu.be/new-21', 'title': 'Bread recipe', 'channel': 'Author'}], 'next-page')
        with patch.object(worker, 'search_youtube', return_value=found) as search:
            with self.assertRaises(YouTubeNoResultsError):
                worker.process_discovery(self.discovery())
        search.assert_called_once_with('bread recipe', 20, 'any')

    def test_twentieth_result_can_beat_the_previous_nineteen(self):
        found = [{'url': f'https://youtu.be/rank-{i}', 'title': 'Recipe', 'channel': 'Author'} for i in range(19)]
        found.append({'url': 'https://youtu.be/best-20', 'title': 'Bread recipe', 'channel': 'Author'})
        with patch.object(worker, 'search_youtube', return_value=found):
            result = worker.process_discovery(self.discovery())
        self.assertEqual(result['selected_position'], 20)

    def test_exhausted_results_stop_without_creating_another_download(self):
        self.used('https://youtu.be/used-up')
        job = self.discovery()
        with patch.object(worker, 'search_youtube', return_value=[{'url': 'https://youtu.be/used-up', 'title': 'Recipe', 'channel': 'Author'}]):
            with self.assertRaisesRegex(YouTubeNoResultsError, 'Все результаты'):
                worker.process_discovery(job)
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(Job)).all()), 1)

    def test_render_retry_keeps_one_clip_and_another_job_cannot_claim_it(self):
        path = self.root / 'bread.mp4'; path.write_bytes(b'source')
        with self.sessions() as db:
            source = SourceVideo(source_url='https://youtu.be/one-source', local_path=str(path),
                duration_seconds=20, rights_confirmed=True, status=RecordStatus.ready)
            db.add(source); db.flush()
            job = Job(type=JobType.render, payload={'source_id': source.id, 'duration': 5, 'count': 1})
            db.add(job); db.commit()
        def render(source, destination, start, duration):
            destination.write_bytes(b'clip')
        with patch.object(worker, 'render_vertical_clip', side_effect=render) as renderer:
            first = worker.process_render(job)
            self.assertEqual(first, worker.process_render(job))
            renderer.assert_called_once()
            with self.sessions() as db:
                another = Job(type=JobType.render, payload=job.payload)
                db.add(another); db.commit()
            with self.assertRaises(FirstResultError): worker.process_render(another)
        with self.sessions() as db:
            self.assertEqual(len(db.scalars(select(Clip)).all()), 1)
            self.assertEqual(len(db.scalars(select(SourceClipUse)).all()), 1)

    def test_technical_retry_is_same_job_after_sixty_seconds(self):
        with self.sessions() as db:
            job = Job(type=JobType.download, payload={'source_id': 'same'}, attempts=1)
            db.add(job); db.commit()
        worker.fail_job(job.id, 'Temporary connection failure')
        with self.sessions() as db:
            saved = db.get(Job, job.id)
            self.assertEqual(saved.status, RecordStatus.queued)
            self.assertAlmostEqual((saved.next_attempt_at - utcnow().replace(tzinfo=None)).total_seconds(), 60, delta=2)
            self.assertEqual(len(db.scalars(select(Job)).all()), 1)
        worker.fail_job(job.id, 'Manual verification', error_kind='manual_verification')
        with self.sessions() as db:
            self.assertEqual(db.get(Job, job.id).status, RecordStatus.error)
            self.assertIsNone(db.get(Job, job.id).next_attempt_at)

    def test_archived_content_hidden_and_cannot_be_retried_or_rendered(self):
        self.used('https://youtu.be/archived', archived=True)
        with self.sessions() as db:
            source = db.scalar(select(SourceVideo))
            clip = db.scalar(select(Clip))
            job = Job(type=JobType.download, archived=True, status=RecordStatus.error, payload={'source_id': source.id})
            db.add(job); db.commit()
        state = self.client.get('/api/state').json()
        self.assertEqual(state['sources'], [])
        self.assertEqual(state['clips'], [])
        self.assertEqual(state['jobs'], [])
        self.assertEqual(self.client.post(f'/api/jobs/{job.id}/retry').status_code, 404)
        self.assertEqual(self.client.post('/api/render', json={'source_id': source.id}).status_code, 404)
        self.assertEqual(self.client.get(f'/api/clips/{clip.id}/preview').status_code, 404)
