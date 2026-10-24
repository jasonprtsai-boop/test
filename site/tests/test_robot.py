from dataclasses import replace
import random
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from tmchess.board import Move
from tmchess.config import CONFIG, SITE_ROOT
from tmchess.robot import HardwareLease, GripperStateUnknown, ListenModeUnavailable, StopRequested, TMRobotAdapter
from tmchess.service import PreflightChecker
from tests.fakes import FakeEngine, FakePreflight, FakeRobot, FakeVision


class RobotProtocolTests(unittest.TestCase):
    def test_tcp_connect_retries_before_any_script_is_sent(self):
        adapter = TMRobotAdapter(CONFIG.network, CONFIG.motion)
        sock = Mock()
        with patch.object(adapter._hardware_lease, 'acquire'), patch('tmchess.robot.time.sleep'), patch(
            'tmchess.robot.socket.create_connection', side_effect=[TimeoutError('timed out'), sock]
        ) as connect:
            self.assertIs(adapter._connect(), sock)
        self.assertEqual(connect.call_count, 2)
        sock.sendall.assert_not_called()

    def test_tcp_connect_failure_is_bounded_and_descriptive(self):
        adapter = TMRobotAdapter(CONFIG.network, CONFIG.motion)
        with patch.object(adapter._hardware_lease, 'acquire'), patch('tmchess.robot.time.sleep'), patch(
            'tmchess.robot.socket.create_connection', side_effect=ConnectionRefusedError('refused')
        ) as connect:
            with self.assertRaisesRegex(ConnectionError, '已嘗試 3 次，尚未送出命令'):
                adapter._connect()
        self.assertEqual(connect.call_count, 3)

    def test_stop_during_connection_prevents_capture_command(self):
        adapter = TMRobotAdapter(CONFIG.network, CONFIG.motion)
        stop = threading.Event()
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        def connected():
            stop.set()
            return sock, (0,) * 6
        with patch.object(adapter, '_open_listen_session', side_effect=connected):
            with self.assertRaises(StopRequested):
                adapter.trigger_capture(stop)
        sock.sendall.assert_not_called()

    def test_hardware_lease_rejects_second_controller_process(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as temporary:
            path = Path(temporary) / "robot.lock"
            first = HardwareLease(path)
            second = HardwareLease(path)
            first.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_configured_fixed_orientation(self):
        self.assertEqual(CONFIG.motion.orientation_rxyz, (-180.0, 0.0, 80.0))

    def test_board_matrix_has_expected_shape_and_measured_references(self):
        matrix = CONFIG.motion.player_board_xy_matrix()
        self.assertEqual((len(matrix), len(matrix[0])), (10, 9))
        expected = {
            (0, 0): (192.85, -162.24),
            (4, 8): (365.90, 98.95),
            (5, 0): (362.25, -182.89),
            (9, 8): (535.80, 75.80),
        }
        for (row, col), xy in expected.items():
            self.assertAlmostEqual(matrix[row][col][0], xy[0], places=6)
            self.assertAlmostEqual(matrix[row][col][1], xy[1], places=6)

    def test_logical_board_matrix_reverses_player_view_for_engine_coordinates(self):
        player_matrix = CONFIG.motion.player_board_xy_matrix()
        logical_matrix = CONFIG.motion.board_xy_matrix()
        for row in range(10):
            for col in range(9):
                self.assertEqual(logical_matrix[row][col], player_matrix[9 - row][8 - col])
        expected = {
            (9, 8): (192.85, -162.24),
            (5, 0): (365.90, 98.95),
            (4, 8): (362.25, -182.89),
            (0, 0): (535.80, 75.80),
        }
        for (row, col), xy in expected.items():
            self.assertAlmostEqual(logical_matrix[row][col][0], xy[0], places=6)
            self.assertAlmostEqual(logical_matrix[row][col][1], xy[1], places=6)

    def test_operator_confirmed_translation_moves_all_90_points_equally(self):
        old_motion = replace(CONFIG.motion, board_reference_xy=(
            (191.65, -196.18),
            (364.70, 65.01),
            (361.05, -216.83),
            (534.60, 41.86),
        ))
        old_matrix = old_motion.player_board_xy_matrix()
        new_matrix = CONFIG.motion.player_board_xy_matrix()

        for row in range(10):
            for col in range(9):
                self.assertAlmostEqual(new_matrix[row][col][0] - old_matrix[row][col][0], 1.20, places=6)
                self.assertAlmostEqual(new_matrix[row][col][1] - old_matrix[row][col][1], 33.94, places=6)

    def test_robot_indexes_board_matrix_as_col_row(self):
        adapter = TMRobotAdapter(CONFIG.network, CONFIG.motion)
        self.assertEqual(adapter._xy_for_cell((0, 0)), CONFIG.motion.board_xy_matrix()[0][0])
        self.assertEqual(adapter._xy_for_cell((8, 9)), CONFIG.motion.board_xy_matrix()[9][8])
        with self.assertRaises(ValueError):
            adapter._xy_for_cell((9, 0))

    def test_tmsct_packet_checksum_and_shape(self):
        packet = TMRobotAdapter.make_packet("CMD00001", "ScriptExit()")
        self.assertTrue(packet.startswith(b"$TMSCT,"))
        self.assertTrue(packet.endswith(b"\r\n"))
        body, checksum = packet[1:-2].rsplit(b"*", 1)
        expected = 0
        for value in body:
            expected ^= value
        self.assertEqual(checksum, f"{expected:02X}".encode("ascii"))

    def test_parse_binary_tmsta_pose(self):
        expected = (1.25, -2.5, 300.0, 4.0, 5.0, 6.0)
        payload = b"90," + struct.pack("<6f", *expected)
        packet = b"noise$TMSTA," + str(len(payload)).encode() + b"," + payload + b",*00\r\n"
        actual = TMRobotAdapter.parse_pose_packet(packet)
        self.assertIsNotNone(actual)
        for left, right in zip(expected, actual):
            self.assertAlmostEqual(left, right, places=4)

    def test_pose_parser_ignores_wrong_data_id(self):
        payload = b"91," + struct.pack("<6f", 1, 2, 3, 4, 5, 6)
        packet = b"$TMSTA," + str(len(payload)).encode() + b"," + payload
        self.assertIsNone(TMRobotAdapter.parse_pose_packet(packet))

    def test_f1_is_classified_as_listen_mode_unavailable(self):
        class FakeSocket:
            def sendall(self, _packet): pass
            def recv(self, _size): return b"$CPERR,2,F1,*3F\r\n"

        adapter = TMRobotAdapter(CONFIG.network, CONFIG.motion)
        with self.assertRaisesRegex(ListenModeUnavailable, "尚未進入 Listen"):
            adapter._read_pose(FakeSocket())

    def test_closed_connection_fails_immediately_for_script_and_pose(self):
        class ClosedSocket:
            def sendall(self, _packet): pass
            def recv(self, _size): return b""

        adapter = TMRobotAdapter(CONFIG.network, CONFIG.motion)
        with self.assertRaisesRegex(ConnectionError, "關閉 Listen 連線"):
            adapter._send_script(ClosedSocket(), "ScriptExit()", "EXIT")
        with self.assertRaisesRegex(ConnectionError, "等待 Pose"):
            adapter._read_pose(ClosedSocket())

    def test_open_listen_session_reconnects_after_f1(self):
        class FakeSocket:
            def __init__(self): self.closed = False
            def close(self): self.closed = True

        motion = replace(
            CONFIG.motion,
            listen_ready_timeout_seconds=1.0,
            listen_retry_seconds=0.0,
        )

        class RetryingAdapter(TMRobotAdapter):
            def __init__(self):
                super().__init__(CONFIG.network, motion)
                self.sockets = []
                self.reads = 0
            def _connect(self):
                sock = FakeSocket()
                self.sockets.append(sock)
                return sock
            def _read_pose(self, _sock):
                self.reads += 1
                if self.reads == 1:
                    raise ListenModeUnavailable("not ready")
                return (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)

        adapter = RetryingAdapter()
        sock, pose = adapter._open_listen_session()

        self.assertEqual(adapter.reads, 2)
        self.assertTrue(adapter.sockets[0].closed)
        self.assertIs(sock, adapter.sockets[1])
        self.assertEqual(pose, (1.0, 2.0, 3.0, 4.0, 5.0, 6.0))

    def test_orientation_error_uses_shortest_distance_across_angle_wrap(self):
        target = (0.0, 0.0, 0.0, 179.0, -179.0, 0.0)
        actual = (0.0, 0.0, 0.0, -179.0, 179.0, 0.5)
        self.assertAlmostEqual(TMRobotAdapter._orientation_error(actual, target), 2.0)

    def test_wait_until_pose_rejects_wrong_orientation_when_xyz_matches(self):
        target = (10.0, 20.0, 30.0, 177.0, 2.0, 71.0)
        wrong_orientation = (10.0, 20.0, 30.0, 182.0, 2.0, 71.0)
        motion = replace(
            CONFIG.motion,
            stable_samples=1,
            pose_poll_seconds=0.0,
            move_timeout_seconds=1.0,
        )

        class FixedPoseAdapter(TMRobotAdapter):
            def _read_pose(self, _sock):
                return wrong_orientation

        adapter = FixedPoseAdapter(CONFIG.network, motion)
        with patch("tmchess.robot.time.monotonic", side_effect=(0.0, 0.1, 0.2, 1.1)):
            with patch("tmchess.robot.time.sleep"):
                with self.assertRaisesRegex(TimeoutError, "姿態最大誤差 5.000°"):
                    adapter._wait_until_pose(object(), target)

    def test_wait_until_pose_accepts_stable_pose_with_wrapped_angles(self):
        target = (10.0, 20.0, 30.0, 179.0, 0.0, -179.0)
        wrapped_equivalent = (10.0, 20.0, 30.0, -179.0, 0.0, 179.0)
        motion = replace(
            CONFIG.motion,
            orientation_tolerance_deg=2.0,
            stable_samples=1,
            pose_poll_seconds=0.0,
            move_timeout_seconds=1.0,
        )

        class FixedPoseAdapter(TMRobotAdapter):
            def _read_pose(self, _sock):
                return wrapped_equivalent

        adapter = FixedPoseAdapter(CONFIG.network, motion)
        with patch("tmchess.robot.time.monotonic", side_effect=(0.0, 0.1, 0.2)):
            with patch("tmchess.robot.time.sleep"):
                adapter._wait_until_pose(object(), target)

    def test_wait_until_pose_requires_orientation_to_settle(self):
        target = (10.0, 20.0, 30.0, 177.0, 2.0, 71.0)
        poses = iter(
            (
                (10.0, 20.0, 30.0, 177.5, 2.0, 71.0),
                (10.0, 20.0, 30.0, 176.5, 2.0, 71.0),
                (10.0, 20.0, 30.0, 177.5, 2.0, 71.0),
                (10.0, 20.0, 30.0, 177.5, 2.0, 71.0),
                (10.0, 20.0, 30.0, 177.5, 2.0, 71.0),
            )
        )
        motion = replace(
            CONFIG.motion,
            stable_samples=2,
            pose_poll_seconds=0.0,
            move_timeout_seconds=1.0,
        )

        class SequencePoseAdapter(TMRobotAdapter):
            def __init__(self):
                super().__init__(CONFIG.network, motion)
                self.reads = 0

            def _read_pose(self, _sock):
                self.reads += 1
                return next(poses)

        adapter = SequencePoseAdapter()
        with patch("tmchess.robot.time.monotonic", side_effect=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5)):
            with patch("tmchess.robot.time.sleep"):
                adapter._wait_until_pose(object(), target)
        self.assertEqual(adapter.reads, 5)

    def test_invalid_arrival_tolerances_fail_static_configuration(self):
        motion = replace(CONFIG.motion, orientation_tolerance_deg=0.0)
        self.assertIn("位置與姿態到點容差必須大於 0", motion.missing_fields())

    def test_gripper_is_turned_off_after_motion_failure(self):
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *args): pass

        class FailingAdapter(TMRobotAdapter):
            def __init__(self):
                super().__init__(CONFIG.network, CONFIG.motion)
                self.gripper = []
            def _connect(self): return FakeSocket()
            def _open_listen_session(self):
                return FakeSocket(), (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            def _xy_for_cell(self, _position): return (0.0, 0.0)
            def _set_gripper(self, _sock, enabled, *args, **kwargs): self.gripper.append(enabled)
            def _enter_safe_plane(self, _sock, _current, _stop_event): pass
            def _transfer(self, sock, source, destination, destination_heights=None, stop_event=None):
                self._set_gripper(sock, True)
                raise ConnectionResetError("reset")

        adapter = FailingAdapter()
        with self.assertRaises(ConnectionResetError):
            adapter.execute_move(Move((0, 0), (0, 1), 0, "a0a1"), threading.Event())
        self.assertEqual(adapter.gripper, [True, False])

    def test_graveyard_scatter_stays_inside_safe_opening(self):
        adapter = TMRobotAdapter(CONFIG.network, CONFIG.motion, random_source=random.Random(7))
        for _ in range(100):
            x, y = adapter._graveyard_drop_xy()
            self.assertLessEqual(((x - 128.0) ** 2 + (y + 265.0) ** 2) ** 0.5, 35.0)

    def test_graveyard_uses_independent_release_heights(self):
        motion = replace(
            CONFIG.motion,
            orientation_rxyz=(180.0, 0.0, 90.0),
            vacuum_settle_seconds=0.0,
            release_settle_seconds=0.0,
        )

        class RecordingAdapter(TMRobotAdapter):
            def __init__(self):
                super().__init__(CONFIG.network, motion)
                self.poses = []
            def _move_to(self, _sock, pose, **_kwargs): self.poses.append(pose)
            def _set_gripper(self, _sock, _enabled, *_args, **_kwargs): pass

        adapter = RecordingAdapter()
        adapter._transfer(
            object(),
            (10.0, 20.0),
            (128.0, -265.0),
            (motion.graveyard_z_safe, motion.graveyard_z_approach, motion.graveyard_z_release),
        )
        destination_z = [pose[2] for pose in adapter.poses if pose[:2] == (128.0, -265.0)]
        self.assertEqual(destination_z, [525.0, 350.0, 340.0, 350.0, 525.0])

    def test_board_transfer_uses_fixed_288_for_pick_and_release(self):
        motion = replace(
            CONFIG.motion,
            vacuum_settle_seconds=0.0,
            release_settle_seconds=0.0,
        )

        class RecordingAdapter(TMRobotAdapter):
            def __init__(self):
                super().__init__(CONFIG.network, motion)
                self.poses = []
            def _move_to(self, _sock, pose, **_kwargs): self.poses.append(pose)
            def _set_gripper(self, _sock, _enabled, *_args, **_kwargs): pass

        adapter = RecordingAdapter()
        adapter._transfer(object(), (10.0, 20.0), (30.0, 40.0))

        source_z = [pose[2] for pose in adapter.poses if pose[:2] == (10.0, 20.0)]
        destination_z = [pose[2] for pose in adapter.poses if pose[:2] == (30.0, 40.0)]
        self.assertEqual(source_z, [420.0, 355.0, 288.0, 355.0, 420.0])
        self.assertEqual(destination_z, [420.0, 355.0, 288.0, 355.0, 420.0])

    def test_safe_plane_entry_lifts_before_changing_orientation(self):
        class RecordingAdapter(TMRobotAdapter):
            def __init__(self):
                super().__init__(CONFIG.network, CONFIG.motion)
                self.poses = []
            def _move_to(self, _sock, pose, **_kwargs):
                self.poses.append(pose)

        adapter = RecordingAdapter()
        current = (250.0, -50.0, 300.0, 170.0, 5.0, 45.0)
        adapter._enter_safe_plane(object(), current, threading.Event())

        self.assertEqual(adapter.poses[0], (250.0, -50.0, 420.0, 170.0, 5.0, 45.0))
        self.assertEqual(adapter.poses[1], (250.0, -50.0, 420.0, -180.0, 0.0, 80.0))

    def test_camera_pose_enters_safe_plane_before_board_transfer(self):
        class RecordingAdapter(TMRobotAdapter):
            def __init__(self):
                super().__init__(CONFIG.network, CONFIG.motion)
                self.poses = []
            def _move_to(self, _sock, pose, **_kwargs):
                self.poses.append(pose)
            def _set_gripper(self, *_args, **_kwargs): pass

        adapter = RecordingAdapter()
        current = (330.08, 80.60, 578.92, -180.0, 0.0, 80.0)
        adapter._enter_safe_plane(object(), current, threading.Event())
        adapter._transfer(object(), (501.324, -198.949), (278.075, -65.085))
        self.assertEqual(adapter.poses[0], (330.08, 80.60, 420.0, -180.0, 0.0, 80.0))
        self.assertEqual(adapter.poses[1], (501.324, -198.949, 420.0, -180.0, 0.0, 80.0))

    def test_safe_plane_failure_prevents_board_transfer(self):
        class FakeSocket:
            def __enter__(self): return self
            def __exit__(self, *_args): pass

        class FailingEntryAdapter(TMRobotAdapter):
            def _open_listen_session(self):
                return FakeSocket(), (330.08, 80.60, 578.92, -180.0, 0.0, 80.0)
            def _move_to(self, *_args, **_kwargs):
                raise ConnectionError("entry failed")
            def _execute_move_in_session(self, *_args, **_kwargs):
                raise AssertionError("board transfer must not start")

        adapter = FailingEntryAdapter(CONFIG.network, CONFIG.motion)
        with self.assertRaisesRegex(ConnectionError, "entry failed"):
            adapter.execute_move(Move((0, 0), (0, 1), 0, "a0a1"), threading.Event())

    def test_stop_prevents_every_command_after_current_low_level_command(self):
        stop = threading.Event()

        class StopAfterFirstCommandAdapter(TMRobotAdapter):
            def __init__(self):
                super().__init__(CONFIG.network, replace(
                    CONFIG.motion, vacuum_settle_seconds=0.0, release_settle_seconds=0.0
                ))
                self.commands = 0
            def _send_line(self, _sock, _pose, **_kwargs):
                self.commands += 1
                stop.set()
            def _wait_until_pose(self, _sock, _target, stop_event=None):
                self._guard_not_stopped(stop_event)

        adapter = StopAfterFirstCommandAdapter()
        with self.assertRaises(StopRequested):
            adapter._transfer(object(), (10.0, 20.0), (30.0, 40.0), stop_event=stop)
        self.assertEqual(adapter.commands, 1)

    def test_failed_cleanup_reports_unknown_gripper_state(self):
        class FailingCleanupAdapter(TMRobotAdapter):
            def _transfer(self, *_args, **_kwargs):
                raise ConnectionResetError("reset")
            def _set_gripper(self, *_args, **_kwargs):
                raise ConnectionResetError("cannot send off")

        adapter = FailingCleanupAdapter(CONFIG.network, CONFIG.motion)
        move = Move((0, 0), (0, 1), 0, "a0a1")
        with self.assertRaises(GripperStateUnknown) as caught:
            adapter._execute_move_in_session(object(), move, threading.Event())
        self.assertIn("原始流程錯誤：ConnectionResetError: reset", str(caught.exception))
        self.assertIn("關閉失敗原因：ConnectionResetError: cannot send off", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, ConnectionResetError)

    def test_unsafe_graveyard_scatter_is_rejected(self):
        motion = replace(CONFIG.motion, graveyard_scatter_radius_mm=50.0)
        self.assertIn("落子箱散布半徑超出淨開口", motion.missing_fields())
        with self.assertRaisesRegex(RuntimeError, "散布半徑"):
            TMRobotAdapter(CONFIG.network, motion)._graveyard_drop_xy()


class PreflightTests(unittest.TestCase):
    def test_board_pick_height_and_retry_are_fixed_at_288(self):
        self.assertEqual(CONFIG.motion.z_pick, 288.0)
        self.assertEqual(CONFIG.motion.retry_pick_z_offset_mm, 0.0)
        self.assertEqual(CONFIG.motion.missing_fields(), [])

    def test_confirmed_configuration_passes_static_preflight(self):
        confirmed = replace(
            CONFIG,
            safety=replace(
                CONFIG.safety,
                hardware_enabled=True,
                board_coordinates_verified=True,
                base_tcp_runtime_guard_verified=True,
                graveyard_capacity_risk_accepted=True,
            ),
        )
        checker = PreflightChecker(confirmed, FakeRobot(), FakeVision([]), FakeEngine())
        report = checker.run(dynamic=False)
        self.assertTrue(report["ready"])
        failed = {item["key"] for item in report["checks"] if not item["ok"]}
        self.assertNotIn("hardware_enabled", failed)
        self.assertNotIn("coordinates", failed)
        self.assertNotIn("emergency_stop", failed)
        self.assertNotIn("motion_parameters", failed)
        self.assertNotIn("dual_view_upload", failed)
        self.assertNotIn("board", failed)
        self.assertNotIn("base", failed)
        self.assertNotIn("tcp", failed)
        self.assertNotIn("z", failed)
        self.assertNotIn("graveyard", failed)
        self.assertNotIn("base_tcp_runtime_guard", failed)
        self.assertNotIn("graveyard_capacity", failed)

    def test_translated_board_matrix_blocks_preflight_until_reverified(self):
        unverified = replace(
            CONFIG,
            safety=replace(CONFIG.safety, board_coordinates_verified=False),
        )
        checker = PreflightChecker(unverified, FakeRobot(), FakeVision([]), FakeEngine())

        report = checker.run(dynamic=False)

        self.assertFalse(report["ready"])
        board = next(item for item in report["checks"] if item["key"] == "board")
        self.assertFalse(board["ok"])
        self.assertEqual(board["detail"], "尚未確認")

    def test_only_unverified_board_blocks_static_preflight(self):
        unverified = replace(
            CONFIG,
            safety=replace(CONFIG.safety, board_coordinates_verified=False),
        )
        checker = PreflightChecker(unverified, FakeRobot(), FakeVision([]), FakeEngine())

        report = checker.run(dynamic=False)
        failed = {item["key"] for item in report["checks"] if not item["ok"]}

        self.assertIn("board", failed)
        self.assertNotIn("base_tcp_runtime_guard", failed)
        self.assertNotIn("graveyard_capacity", failed)
        self.assertFalse(report["ready"])

    def test_dynamic_preflight_requires_per_start_base_tcp_confirmation(self):
        confirmed = replace(
            CONFIG,
            safety=replace(
                CONFIG.safety,
                hardware_enabled=True,
                board_coordinates_verified=True,
            ),
        )
        checker = PreflightChecker(confirmed, FakeRobot(), FakeVision([]), FakeEngine())

        blocked = checker.run(dynamic=True)
        self.assertFalse(blocked["ready"])
        runtime = next(
            item for item in blocked["checks"]
            if item["key"] == "base_tcp_runtime_confirmation"
        )
        self.assertFalse(runtime["ok"])

        checker.confirm_base_tcp("RobotBase", "NOTOOL")
        self.assertTrue(checker.run(dynamic=True)["ready"])

    def test_invalid_orientation_tolerance_blocks_static_preflight(self):
        config = replace(
            CONFIG,
            motion=replace(CONFIG.motion, orientation_tolerance_deg=0.0),
        )
        checker = PreflightChecker(config, FakeRobot(), FakeVision([]), FakeEngine())
        report = checker.run(dynamic=False)
        self.assertFalse(report["ready"])
        coordinates = next(item for item in report["checks"] if item["key"] == "coordinates")
        self.assertFalse(coordinates["ok"])
        self.assertIn("位置與姿態到點容差必須大於 0", coordinates["detail"])


if __name__ == "__main__":
    unittest.main()
