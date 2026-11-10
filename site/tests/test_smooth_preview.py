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

    def test_geometry_or_disabled_radius_prevents_blending(self):
        cases = (
            (0, {}), (1, {}), (10, {}),
            (100, {"safe": 365}), (100, {"radius": 0}),
        )
        for distance, options in cases:
            with self.subTest(distance=distance, options=options):
                result = preview((0, 0), (distance, 0), **options)
                self.assertFalse(result["smoothing_candidate"])

    def test_invalid_inputs_rejected(self):
        for kwargs in ({"radius": -1}, {"radius": 6}, {"radius": float("nan")},
                       {"safe": 355}, {"safe": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                preview((0, 0), (100, 0), **kwargs)
