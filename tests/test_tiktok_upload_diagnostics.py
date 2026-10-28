import unittest
import json
from types import SimpleNamespace
from contextlib import contextmanager
from unittest.mock import Mock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.services import tiktok_web


class TikTokUploadDiagnosticsTests(unittest.TestCase):
    def test_challenge_on_login_page_is_not_treated_as_expired_session(self):
        page=Mock(url='https://www.tiktok.com/login')
        page.locator.return_value.inner_text.return_value='Verify to continue'
        with self.assertRaises(tiktok_web.TikTokWebSessionError) as caught:
            tiktok_web.check_challenge(page)
        self.assertNotIsInstance(caught.exception,tiktok_web.TikTokWebExpiredSessionError)

    def test_post_error_21_remains_separate_from_protected_rate_or_verification_errors(self):
        for message, owner_review in [('Request failed', False), ('Rate limit exceeded', True), ('Complete verification', True), ('Policy restriction', True)]:
            page = Mock()
            response = Mock(url='https://www.tiktok.com/tiktok/web/project/post/v1/?token=DO_NOT_EXPOSE',
                headers={'content-type':'application/json'},status=200,request=SimpleNamespace(method='POST'))
            response.json.return_value = {'status_code':21,'status_msg':message,'secret':'DO_NOT_EXPOSE'}
            with tiktok_web.monitor_upload_policy(page):
                page.on.call_args.args[1](response)
                refusal = tiktok_web._post_refusals[page]
                self.assertEqual(refusal, {'code':21,'requires_owner_review':owner_review})
                self.assertNotIn('DO_NOT_EXPOSE', json.dumps(tiktok_web._upload_responses[page]))
            self.assertNotIn(page, tiktok_web._post_refusals)

    def test_post_metadata_excludes_tokens_headers_and_response_bodies(self):
        page = Mock()
        response = Mock(url='https://www.tiktok.com/fixture-post?token=DO_NOT_EXPOSE',
            headers={'content-type': 'application/json'}, status=200,
            request=SimpleNamespace(method='POST'))
        response.json.return_value = {'status_code': 12345, 'data': {'error_code': 7, 'token': 'DO_NOT_EXPOSE'},
            'cookies': 'DO_NOT_EXPOSE', 'message': 'DO_NOT_EXPOSE'}
        with tiktok_web.monitor_upload_policy(page):
            listener = page.on.call_args.args[1]
            listener(response)
            metadata = tiktok_web._upload_responses[page]
            self.assertEqual(metadata[0]['path'], '/fixture-post')
            self.assertEqual(metadata[0]['codes'], [{'field': 'status_code', 'value': 12345}, {'field': 'error_code', 'value': 7}])
            self.assertNotIn('DO_NOT_EXPOSE', json.dumps(metadata))
        self.assertNotIn(page, tiktok_web._upload_responses)

    def test_policy_error_does_not_claim_session_is_invalid(self):
        self.assertFalse(issubclass(tiktok_web.TikTokWebPolicyError, tiktok_web.TikTokWebSessionError))

    def test_session_check_waits_for_late_permission_rejection(self):
        page = Mock()
        @contextmanager
        def fixture(_):
            yield page
        with patch.object(tiktok_web, "tiktok_page", fixture), patch.object(tiktok_web, "open_upload"), \
             patch.object(tiktok_web, "check_challenge", side_effect=tiktok_web.TikTokWebPolicyError("30522")):
            with self.assertRaises(tiktok_web.TikTokWebPolicyError):
                tiktok_web.check_session("LOCAL FIXTURE")
        page.wait_for_timeout.assert_called_once_with(500)

    def test_200_policy_error_is_sanitized_and_listener_removed(self):
        page = Mock()
        response = Mock(url="https://www.tiktok.com/top/v1?Signature=DO_NOT_EXPOSE", headers={"content-type": "application/json"})
        response.json.return_value = {"auth": "DO_NOT_EXPOSE", "ResponseMetadata": {"Error": {"CodeN": 30522, "Message": "DO_NOT_EXPOSE"}}}
        with tiktok_web.monitor_upload_policy(page), self.assertLogs(tiktok_web.log, level="WARNING") as logs:
            listener = page.on.call_args.args[1]
            listener(response)
            with self.assertRaises(tiktok_web.TikTokWebPolicyError) as caught:
                tiktok_web.check_challenge(page)
            self.assertIn("30522", str(caught.exception))
            self.assertNotIn("DO_NOT_EXPOSE", str(caught.exception))
        self.assertNotIn("DO_NOT_EXPOSE", str(logs.output))
        page.remove_listener.assert_called_once_with("response", listener)
        self.assertNotIn(page, tiktok_web._policy_blocks)

    def test_unrelated_hosts_paths_and_non_error_responses_are_ignored(self):
        for url, body in (("https://evil.example/top/v1", {"ResponseMetadata": {"Error": {"CodeN": 30522}}}),
                          ("https://www.tiktok.com/unrelated", {"ResponseMetadata": {"Error": {"CodeN": 30522}}}),
                          ("https://www.tiktok.com/top/v1", {"ResponseMetadata": {"Result": {}}}),
                          ("https://www.tiktok.com/top/v1", [])):
            page = Mock()
            response = Mock(url=url, headers={"content-type": "application/json"})
            response.json.return_value = body
            with tiktok_web.monitor_upload_policy(page):
                page.on.call_args.args[1](response)
                self.assertFalse(tiktok_web._policy_blocks[page])

    def test_late_upload_error_takes_priority_over_visibility_timeout(self):
        page = Mock()
        with patch.object(tiktok_web, "check_challenge"), patch.object(tiktok_web, "dismiss_editor_tip"), \
             patch.object(tiktok_web, "caption_field"), patch.object(tiktok_web, "set_visibility", side_effect=PlaywrightTimeoutError("overlay")), \
             patch.object(tiktok_web, "check_upload_failure", side_effect=[None, tiktok_web.TikTokWebTransferError("Upload failed")]):
            progress = Mock()
            with self.assertRaises(tiktok_web.TikTokWebTransferError):
                tiktok_web._post(page, "Fixture", "private", progress)
            progress.assert_not_called()
