import threading
import unittest

from tmchess.calibration import BoardMatrixInspector, InspectionProfile, build_inspection_points
from tmchess.config import CONFIG


class BoardMatrixInspectionTests(unittest.TestCase):
    def test_path_contains_all_90_unique_matrix_points(self):
        points = build_inspection_points(CONFIG.motion)

        self.assertEqual(len(points), 90)
        self.assertEqual(len({(point.player_row, point.player_col) for point in points}), 90)
        self.assertEqual((points[0].player_row, points[0].player_col), (0, 0))
        self.assertEqual((points[8].player_row, points[8].player_col), (0, 8))
        self.assertEqual((points[9].player_row, points[9].player_col), (1, 8))
        self.assertEqual((points[-1].player_row, points[-1].player_col), (9, 0))

    def test_requested_z_profile_requires_explicit_below_safe_acceptance(self):
        profile = InspectionProfile()
        with self.assertRaisesRegex(ValueError, "低於設定 Z_SAFE"):
            profile.validate(CONFIG.motion, allow_below_safe_z=False)
        profile.validate(CONFIG.motion, allow_below_safe_z=True)

    def test_descent_speed_must_be_lower(self):
        profile = InspectionProfile(inspection_speed=20, approach_speed=10)
        with self.assertRaisesRegex(ValueError, "下降速度"):
            profile.validate(CONFIG.motion, allow_below_safe_z=True)

    def test_inspection_height_preserves_piece_clearance_and_tolerance(self):
        InspectionProfile(inspection_z=300.0).validate(
            CONFIG.motion, allow_below_safe_z=True,
        )
        with self.assertRaisesRegex(ValueError, "單層棋子頂面"):
            InspectionProfile(inspection_z=292.0).validate(
                CONFIG.motion, allow_below_safe_z=True,
            )

    def test_each_point_uses_requested_z_sequence_and_slow_descent(self):
        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeRobot:
            def __init__(self):
                self._lock = threading.Lock()
                self.current = (100.0, 100.0, 400.0, 1.0, 2.0, 3.0)
                self.moves = []
                self.gripper = []

            def _connect(self):
                return FakeSocket()

            def _set_gripper(self, _sock, enabled):
                self.gripper.append(enabled)

            def _read_pose(self, _sock):
                return self.current

            def _move_to(self, _sock, pose, *, speed=None, acceleration=None):
                self.current = pose
                self.moves.append((pose, speed, acceleration))

        robot = FakeRobot()
        points = build_inspection_points(CONFIG.motion)[:2]
        profile = InspectionProfile(dwell_seconds=0)
        records = []
        inspector = BoardMatrixInspector(
            robot, CONFIG.motion, profile, allow_below_safe_z=True,
        )

        inspector.run(points, records.append)

        self.assertEqual(robot.gripper, [False])
        self.assertEqual([move[0][2] for move in robot.moves], [
            420.0, 420.0, 420.0,
            380.0, 340.0, 300.0, 380.0,
            380.0, 340.0, 300.0, 380.0,
            420.0,
        ])
        inspection_moves = [move for move in robot.moves if move[0][2] == 300.0]
        self.assertEqual([move[1] for move in inspection_moves], [5, 5])
        self.assertEqual(len(records), 2)


if __name__ == "__main__":
    unittest.main()
