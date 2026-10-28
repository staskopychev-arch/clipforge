import random,subprocess,unittest
from pathlib import Path
from unittest.mock import patch
from app.services import auto_montage as montage
from app.services.editing import resolve_automatic,parse_editing


class MontageTests(unittest.TestCase):
    def test_new_profile_has_full_hd_and_keeps_both_trimmed_edges_out(self):
        for total in (10,10.1,11,12,15,60):
            for seed in range(80):
                plan=resolve_automatic(total,1920,1080,[],rng=random.Random(seed),profile_version=2)
                self.assertEqual(plan['quality'],'1080p')
                self.assertNotEqual(plan['speed'],1)
                self.assertTrue(.95<=plan['speed']<=1.05)
                self.assertGreaterEqual(abs(plan['speed']-1),.01-1e-8)
                self.assertTrue(10-1e-6<=plan['duration_seconds']<=15+1e-6)
                self.assertGreaterEqual(plan['start_seconds']+1e-6,plan['trim_head_seconds'])
                end=plan['start_seconds']+plan['duration_seconds']*plan['speed']
                self.assertLessEqual(end,total-plan['trim_tail_seconds']+1e-6)
                if total>=12:
                    self.assertTrue(1<=plan['trim_head_seconds']<=2)
                    self.assertTrue(1<=plan['trim_tail_seconds']<=2)

    def test_legacy_snapshot_keeps_old_render_profile(self):
        plan=resolve_automatic(60,1920,1080,[],rng=random.Random(1))
        self.assertNotIn('profile_version',plan)
        self.assertNotEqual(plan['quality'],'1080p')
        self.assertTrue(parse_editing({'automatic':True,'automatic_profile':2,'music_pool':[]}).automatic)

    def test_text_or_ocr_failure_disables_mirroring(self):
        plan={'start_seconds':2,'duration_seconds':12,'speed':1.02}
        for check in ('text','unknown','clear'):
            with patch.object(montage,'detect_excerpt_text',return_value=check):
                presentation=montage.resolve_presentation(Path('x.mp4'),plan,'Природа %{evil}: [test]',rng=random.Random(1))
            self.assertEqual(presentation['mirror'],check=='clear')
            self.assertEqual(presentation['text_check'],check)
            self.assertIn('%{evil}',presentation['heading'])
            self.assertTrue(1.01<=presentation['zoom']<=1.03)
        with patch.object(montage,'_run_media_command',side_effect=OSError('missing')):
            self.assertEqual(montage.detect_excerpt_text(Path('x'),0,10),'unknown')

    def test_titles_are_bounded_literal_text_with_no_control_characters(self):
        title=montage.title_card('  Привет\x00  мир\n '+('nature '*50))
        self.assertLessEqual(len(title),100)
        self.assertNotIn('\x00',title)
        self.assertLessEqual(len(title.splitlines()),3)
