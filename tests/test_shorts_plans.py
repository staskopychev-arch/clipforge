import unittest
from app.services.media import clip_start_times


class ClipPlanTests(unittest.TestCase):
    def test_non_overlapping_when_source_is_long_enough(self):
        starts = clip_start_times(59.6, 2, 20)
        self.assertGreaterEqual(starts[1], starts[0] + 20)
        self.assertLessEqual(starts[-1] + 20, 59.6)

    def test_three_exact_clips(self):
        self.assertEqual(clip_start_times(60, 3, 20), [0, 20, 40])
