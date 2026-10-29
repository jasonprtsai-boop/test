"""Geometric checks only; these tests cannot validate controller blending."""
import unittest

from tools.preview_smooth_transfer import preview


class SmoothPreviewTests(unittest.TestCase):
    def test_regular_transfer_keeps_contact_outside_smoothing(self):
        p = preview((0, 0), (35, 0))
        self.assertTrue(p["smoothing_candidate"])
        self.assertEqual(p["start_stop_z"], 355)
        self.assertEqual(p["end_stop_z"], 355)
        self.assertFalse(p["low_contact_segment_smoothed"])
        self.assertFalse(p["controller_path_verified"])

    def test_short_or_zero_paths_do_not_blend(self):
        for distance in (0, 1, 10):
            with self.subTest(distance=distance):
                self.assertFalse(preview((0, 0), (distance, 0))["smoothing_candidate"])

    def test_insufficient_vertical_margin_does_not_blend(self):
        self.assertFalse(preview((0, 0), (100, 0), safe=365)["smoothing_candidate"])

    def test_disabled_radius(self):
        self.assertFalse(preview((0, 0), (100, 0), radius=0)["smoothing_candidate"])

    def test_invalid_inputs_rejected(self):
        for kwargs in ({"radius": -1}, {"radius": 6}, {"radius": float("nan")},
                       {"safe": 355}, {"safe": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                preview((0, 0), (100, 0), **kwargs)
