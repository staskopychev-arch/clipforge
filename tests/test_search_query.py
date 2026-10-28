"""The current query reaches YouTube; only license-verified results return."""
import unittest
from unittest.mock import patch

import httpx

from app.config import settings
from app.services.youtube import search_creative_commons, search_youtube, relevance_score, YouTubeSearchError


class SearchQueryTests(unittest.TestCase):
    def test_metadata_score_uses_full_words_and_unicode_case(self):
        self.assertEqual(relevance_score('хлеб', {'title': 'ХЛЕБ'}), 120)
        self.assertEqual(relevance_score('bread', {'title': 'breadcrumb'}), 0)

    def test_metadata_score_rewards_query_coverage_and_exact_phrase(self):
        score = lambda title: relevance_score('bread recipe', {'title': title})
        self.assertGreater(score('Bread recipe'), score('Recipe for bread'))
        self.assertGreater(score('Recipe for bread'), score('Recipe'))

    def test_description_breaks_title_tie_without_outweighing_title_match(self):
        relevant = {'title': 'Recipe', 'description': 'Homemade bread recipe'}
        partial = {'title': 'Recipe', 'description': 'A tasty dish'}
        self.assertGreater(relevance_score('bread recipe', relevant), relevance_score('bread recipe', partial))
        self.assertGreater(relevance_score('bread recipe', {'title': 'Bread recipe'}), relevance_score('bread recipe', relevant))

    def test_repeating_keywords_does_not_boost_score(self):
        self.assertEqual(relevance_score('bread', {'title': 'bread'}),
                         relevance_score('bread', {'title': 'bread bread bread'}))

    def test_search_page_preserves_next_token_and_sends_requested_page_token(self):
        response = httpx.Response(200, json={'items': [], 'nextPageToken': 'following-page'})
        with patch.object(settings, 'youtube_api_key', 'fixture-key'), patch('app.services.youtube.httpx.get', return_value=response) as request:
            result = search_youtube('bread recipe', 50, page_token='current-page')
        self.assertEqual(request.call_args.kwargs['params']['pageToken'], 'current-page')
        self.assertEqual(result.next_page_token, 'following-page')

    def test_author_permission_search_does_not_filter_or_verify_cc(self):
        response = httpx.Response(200, json={'items': [
            {'id': {'videoId': 'recipe00001'}, 'snippet': {'title': 'Bread &amp; butter', 'channelTitle': 'Cooking fixture'}},
            {'id': {'videoId': 'recipe00002'}, 'snippet': {'title': 'Second recipe', 'channelTitle': 'Another author'}},
        ]})
        with patch.object(settings, 'youtube_api_key', 'fixture-key'), \
                patch('app.services.youtube.httpx.get', return_value=response) as request:
            result = search_youtube('  homemade bread recipe  ', 2, 'medium')
        request.assert_called_once()
        params = request.call_args.kwargs['params']
        self.assertEqual(params['q'], 'homemade bread recipe')
        self.assertEqual(params['videoDuration'], 'medium')
        self.assertEqual(params['order'], 'relevance')
        self.assertNotIn('videoLicense', params)
        self.assertEqual([item['video_id'] for item in result], ['recipe00001', 'recipe00002'])
        self.assertEqual(result[0]['title'], 'Bread & butter')
        self.assertTrue(all(item['rights_basis'] == 'author_permission' for item in result))

    def test_author_permission_empty_search_does_not_make_up_results(self):
        with patch.object(settings, 'youtube_api_key', 'fixture-key'), \
                patch('app.services.youtube.httpx.get', return_value=httpx.Response(200, json={'items': []})) as request:
            self.assertEqual(search_youtube('homemade bread', 20), [])
        request.assert_called_once()

    def test_blank_query_never_searches_youtube(self):
        with patch('app.services.youtube.httpx.get') as request:
            with self.assertRaisesRegex(YouTubeSearchError, 'Укажите поисковый запрос'):
                search_youtube('   ', 20)
        request.assert_not_called()

    def test_permission_mode_reports_youtube_errors(self):
        with patch.object(settings, 'youtube_api_key', 'fixture-key'), \
                patch('app.services.youtube.httpx.get', return_value=httpx.Response(403, json={'error': {'message': 'Quota exhausted'}})):
            with self.assertRaisesRegex(YouTubeSearchError, 'Quota exhausted'):
                search_youtube('homemade bread', 20)

    def test_current_cooking_query_and_license_check_reach_youtube(self):
        responses = [
            httpx.Response(200, json={'items': [
                {'id': {'videoId': 'cooking0001'}, 'snippet': {'title': 'Tortang talong', 'channelTitle': 'Cooking fixture'}},
                {'id': {'videoId': 'cooking0002'}, 'snippet': {'title': 'Another recipe', 'channelTitle': 'Cooking fixture'}},
            ]}),
            httpx.Response(200, json={'items': [
                {'id': 'cooking0001', 'status': {'license': 'creativeCommon'}},
                {'id': 'cooking0002', 'status': {'license': 'youtube'}},
            ]}),
        ]
        with patch.object(settings, 'youtube_api_key', 'fixture-key'), \
                patch('app.services.youtube.httpx.get', side_effect=responses) as request:
            results = search_creative_commons('tortang talong cooking', 20)
        self.assertEqual(request.call_args_list[0].kwargs['params']['q'], 'tortang talong cooking')
        self.assertEqual(request.call_args_list[0].kwargs['params']['videoLicense'], 'creativeCommon')
        self.assertEqual(request.call_args_list[1].kwargs['params']['id'], 'cooking0001,cooking0002')
        self.assertEqual([item['video_id'] for item in results], ['cooking0001'])

    def test_empty_youtube_response_stays_empty(self):
        with patch.object(settings, 'youtube_api_key', 'fixture-key'), \
                patch('app.services.youtube.httpx.get', return_value=httpx.Response(200, json={'items': []})) as request:
            self.assertEqual(search_creative_commons('tortang talong cooking', 20), [])
        self.assertEqual(request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
