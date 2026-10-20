import io
import json
import queue
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tmchess.config import CONFIG, SITE_ROOT
from tmchess.board import standard_starting_board
from tmchess.vision import BoardObservationError, DualViewInbox, DualViewProcessor


class VisionMappingTests(unittest.TestCase):
    def setUp(self):
        self.processor = DualViewProcessor(CONFIG.vision)

    def test_v1_maps_calibrated_center(self):
        detection = {"piece": 13, "confidence": .9, "cx": 1355.5, "cy": 155.0}
        board = self.processor._map_v1([detection], CONFIG.vision.v1_reference)
        self.assertEqual(board[0][4], 13)

    def test_v1_does_not_publish_v2_takeover_cells(self):
        detection = {"piece": 14, "confidence": .9, "cx": 807.5, "cy": 295.5}
        board = self.processor._map_v1([detection], CONFIG.vision.v1_reference)
        self.assertEqual(board[1][0], 0)

    def test_v2_maps_into_main_board(self):
        detection = {"piece": 14, "confidence": .9, "cx": 1252.0, "cy": 929.0}
        mapped = self.processor._map_v2([detection], CONFIG.vision.v2_reference)
        self.assertEqual(mapped[(1, 0)], 14)

    def test_highest_confidence_wins_collision(self):
        low = {"piece": 14, "confidence": .5, "cx": 1355.5, "cy": 155.0}
        high = {"piece": 13, "confidence": .9, "cx": 1355.5, "cy": 155.0}
        board = self.processor._map_v1([low, high], CONFIG.vision.v1_reference)
        self.assertEqual(board[0][4], 13)

    def test_selected_detection_confidence_is_published_per_cell(self):
        low = {"piece": 14, "confidence": .26, "cx": 1355.5, "cy": 155.0}
        high = {"piece": 13, "confidence": .91, "cx": 1355.5, "cy": 155.0}

        board, confidence = self.processor._map_v1_with_confidence(
            [low, high], CONFIG.vision.v1_reference
        )

        self.assertEqual(board[0][4], 13)
        self.assertEqual(confidence[0][4], .91)
        self.assertIsNone(confidence[0][3])

    def test_impossible_third_horse_uses_same_cell_soldier_candidate(self):
        board = standard_starting_board()
        board[3][4] = 12
        candidates = {
            (3, 4): [
                {"piece": 12, "confidence": 0.2600},
                {"piece": 14, "confidence": 0.1856},
                {"piece": 13, "confidence": 0.1293},
            ]
        }

        corrections = self.processor._repair_piece_limit_misclassifications(
            board, candidates
        )

        self.assertEqual(board[3][4], 14)
        self.assertEqual(corrections, [((3, 4), 12, 14, 0.1856)])

    def test_impossible_count_is_not_hidden_without_same_color_candidate(self):
        board = standard_starting_board()
        board[3][4] = 12
        candidates = {
            (3, 4): [
                {"piece": 12, "confidence": 0.2600},
                {"piece": 7, "confidence": 0.2000},
            ]
        }

        corrections = self.processor._repair_piece_limit_misclassifications(
            board, candidates
        )

        self.assertEqual(corrections, [])
        self.assertEqual(board[3][4], 12)

    def test_invalid_model_class_contract_is_not_cached(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            model_path = Path(directory) / "bad.pt"
            model_path.write_bytes(b"placeholder")
            calls = []

            class FakeModel:
                names = {0: "unexpected"}

            def fake_yolo(path):
                calls.append(path)
                return FakeModel()

            processor = DualViewProcessor(replace(CONFIG.vision, model_path=model_path))
            with patch.dict("sys.modules", {"ultralytics": SimpleNamespace(YOLO=fake_yolo)}):
                with self.assertRaisesRegex(RuntimeError, "類別"):
                    processor._load_model()
                with self.assertRaisesRegex(RuntimeError, "類別"):
                    processor._load_model()

            self.assertEqual(len(calls), 2)

    def test_inference_timeout_terminates_isolated_worker(self):
        class SilentResponses:
            def get(self, timeout):
                raise queue.Empty

            def cancel_join_thread(self):
                pass

            def close(self):
                pass

        class FakeProcess:
            exitcode = None

            def __init__(self):
                self.alive = True
                self.terminated = False
                self.closed = False

            def is_alive(self):
                return self.alive

            def terminate(self):
                self.terminated = True
                self.alive = False

            def kill(self):
                self.alive = False

            def join(self, timeout):
                pass

            def close(self):
                self.closed = True

        processor = DualViewProcessor(replace(CONFIG.vision, inference_timeout_seconds=0.01))
        process = FakeProcess()
        processor._process = process
        processor._responses = SilentResponses()

        with self.assertRaisesRegex(TimeoutError, "已終止隔離程序"):
            processor._wait_worker_message_locked(0.01, operation="推論")

        self.assertTrue(process.terminated)
        self.assertTrue(process.closed)
        self.assertIsNone(processor._process)


class InboxTimeoutTests(unittest.TestCase):
    @staticmethod
    def jpeg(width=16, height=12):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (width, height), "white").save(output, format="JPEG")
        return output.getvalue()

    def test_view_identity_comes_from_resolution_not_arrival_order(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            inbox = DualViewInbox(
                Path(directory),
                "127.0.0.1",
                1024 * 1024,
                expected_view_sizes={"v1": (16, 12), "v2": (20, 14)},
            )
            inbox.set_receiver_ready(True)
            inbox.arm("20261010T010203Z-12345678")

            _capture, first_view, _size = inbox.accept(
                "127.0.0.1", "image/jpeg", self.jpeg(20, 14)
            )
            _capture, second_view, _size = inbox.accept(
                "127.0.0.1", "image/jpeg", self.jpeg(16, 12)
            )
            v1, v2 = inbox.wait(0.01)

            self.assertEqual((first_view, second_view), ("v2", "v1"))
            self.assertEqual(
                (v1.name, v2.name),
                (
                    "20261010T010203Z-12345678_v1.jpg",
                    "20261010T010203Z-12345678_v2.jpg",
                ),
            )

    def test_same_resolution_uses_tmflow_arrival_order_when_sizes_are_not_configured(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            inbox = DualViewInbox(
                Path(directory),
                "127.0.0.1",
                1024 * 1024,
            )
            inbox.set_receiver_ready(True)
            inbox.arm("20261010T010203Z-12345678")

            _capture, first_view, first_size = inbox.accept(
                "127.0.0.1", "image/jpeg", self.jpeg(20, 14)
            )
            _capture, second_view, second_size = inbox.accept(
                "127.0.0.1", "image/jpeg", self.jpeg(20, 14)
            )
            v1, v2 = inbox.wait(0.01)

            self.assertEqual((first_view, second_view), ("v1", "v2"))
            self.assertEqual((first_size, second_size), ((20, 14), (20, 14)))
            self.assertEqual(
                (v1.name, v2.name),
                (
                    "20261010T010203Z-12345678_v1.jpg",
                    "20261010T010203Z-12345678_v2.jpg",
                ),
            )

    def test_view_identity_configuration_requires_two_distinct_sizes(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            with self.assertRaisesRegex(ValueError, "同時包含"):
                DualViewInbox(
                    Path(directory), "127.0.0.1", 1024,
                    expected_view_sizes={"v1": (16, 12)},
                )
            with self.assertRaisesRegex(ValueError, "必須不同"):
                DualViewInbox(
                    Path(directory), "127.0.0.1", 1024,
                    expected_view_sizes={"v1": (16, 12), "v2": (16, 12)},
                )

    def test_duplicate_or_unknown_view_resolution_is_rejected(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            inbox = DualViewInbox(
                Path(directory),
                "127.0.0.1",
                1024 * 1024,
                expected_view_sizes={"v1": (16, 12), "v2": (20, 14)},
            )
            inbox.set_receiver_ready(True)
            inbox.arm("20261010T010203Z-12345678")
            inbox.accept("127.0.0.1", "image/jpeg", self.jpeg(16, 12))
            with self.assertRaisesRegex(RuntimeError, "重複視角"):
                inbox.accept("127.0.0.1", "image/jpeg", self.jpeg(16, 12))
            with self.assertRaisesRegex(ValueError, "無法識別視角"):
                inbox.accept("127.0.0.1", "image/jpeg", self.jpeg(18, 13))

    def test_two_view_timeout_clears_active_capture(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            inbox = DualViewInbox(Path(directory), "127.0.0.1", 1024)
            inbox.set_receiver_ready(True)
            inbox.arm("20261010T010203Z-12345678")
            with self.assertRaises(TimeoutError):
                inbox.wait(0.01)
            with self.assertRaises(RuntimeError):
                inbox.accept("127.0.0.1", "image/jpeg", self.jpeg())

    def test_second_view_gets_its_own_timeout_window(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            inbox = DualViewInbox(Path(directory), "127.0.0.1", 1024)
            inbox.set_receiver_ready(True)
            inbox.arm("20261010T010203Z-12345678")
            inbox.accept("127.0.0.1", "image/jpeg", self.jpeg())
            with self.assertRaisesRegex(TimeoutError, "已收到一個視角.*等待另一個視角"):
                inbox.wait(0.01)

    def test_selected_game_groups_both_views_under_one_game_folder(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            root = Path(directory)
            inbox = DualViewInbox(root, "127.0.0.1", 1024 * 1024)
            game_id = "20261012T010203Z-12345678"
            capture_id = "20261012T010204Z-87654321"
            inbox.set_receiver_ready(True)
            inbox.select_game(game_id)
            inbox.arm(capture_id)
            inbox.accept("127.0.0.1", "image/jpeg", self.jpeg())
            inbox.accept("127.0.0.1", "image/jpeg", self.jpeg())

            v1, v2 = inbox.wait(0.01)

            expected = root / game_id
            self.assertEqual(v1.parent, expected)
            self.assertEqual(v2.parent, expected)
            self.assertEqual(v1.name, f"{capture_id}_v1.jpg")
            self.assertEqual(v2.name, f"{capture_id}_v2.jpg")

    def test_round_and_capture_number_are_in_flat_filenames(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            root = Path(directory)
            inbox = DualViewInbox(root, "127.0.0.1", 1024 * 1024)
            game_id = "20261012T010203Z-12345678"
            capture_id = "20261012T010204Z-87654321"
            inbox.set_receiver_ready(True)
            inbox.select_game(game_id)
            inbox.arm(capture_id, f"R003_C005_player_move_{capture_id}")
            inbox.accept("127.0.0.1", "image/jpeg", self.jpeg())
            inbox.accept("127.0.0.1", "image/jpeg", self.jpeg())

            v1, v2 = inbox.wait(0.01)

            self.assertEqual(v1.parent, root / game_id)
            self.assertEqual(v1.name, f"R003_C005_player_move_{capture_id}_v1.jpg")
            self.assertEqual(v2.name, f"R003_C005_player_move_{capture_id}_v2.jpg")

    def test_capture_result_json_is_saved_beside_images(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            root = Path(directory)
            board = standard_starting_board()
            confidence = [[None for _ in range(9)] for _ in range(10)]

            result = DualViewProcessor._write_result(
                root, "20261012T010204Z-87654321", board, confidence
            )
            payload = json.loads(result.read_text(encoding="utf-8"))

            self.assertEqual(result, root / "result.json")
            self.assertEqual(payload["board"], board)
            self.assertEqual(payload["capture_id"], "20261012T010204Z-87654321")

    def test_processor_keeps_outputs_flat_with_same_round_prefix(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            root = Path(directory)
            prefix = "R004_C007_ai_verify_20261012T010204Z-87654321"
            v1 = root / f"{prefix}_v1.jpg"
            v2 = root / f"{prefix}_v2.jpg"
            v1.write_bytes(self.jpeg())
            v2.write_bytes(self.jpeg())
            board = standard_starting_board()
            confidence = [[None for _ in range(9)] for _ in range(10)]
            takeover = {
                (row, col): (board[row][col], 0.9)
                for row, col in ((3, 0), (3, 2))
            }
            processor = DualViewProcessor(CONFIG.vision)

            def fake_detect(_input, output, **_kwargs):
                output.write_bytes(self.jpeg())
                return [], (16, 12)

            with (
                patch.object(processor, "_detect", side_effect=fake_detect),
                patch.object(processor, "_map_v1_with_confidence", return_value=(board, confidence)),
                patch.object(processor, "_map_v2_with_confidence", return_value=takeover),
            ):
                result = processor.process("20261012T010204Z-87654321", v1, v2)

            self.assertEqual(result.v1_path, root / f"{prefix}_v1_yolo.jpg")
            self.assertEqual(result.v2_path, root / f"{prefix}_v2_yolo.jpg")
            self.assertEqual(result.result_path, root / f"{prefix}_result.json")
            self.assertTrue(result.result_path.is_file())

    def test_invalid_observation_keeps_images_and_board_for_contextual_repair(self):
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as directory:
            root = Path(directory)
            v1, v2 = root / 'capture_v1.jpg', root / 'capture_v2.jpg'
            board = standard_starting_board()
            board[9][4] = 0
            takeover = {(row, col): (board[row][col], 0.9) for row in range(1, 5) for col in range(4)}
            confidence = [[None] * 9 for _ in range(10)]
            processor = DualViewProcessor(CONFIG.vision)
            def detect(_input, output, **kwargs):
                if kwargs.get('save_output', True):
                    output.write_bytes(self.jpeg())
                return [], (16, 12)
            with patch.object(processor, '_detect', side_effect=detect), \
                 patch.object(processor, '_map_v1_with_confidence', return_value=(board, confidence)), \
                 patch.object(processor, '_map_v2_with_confidence', return_value=takeover):
                with self.assertRaises(BoardObservationError) as raised:
                    processor.process('capture', v1, v2)
            result = raised.exception.result
            self.assertEqual(result.board[9][4], 0)
            self.assertTrue(result.v1_path.is_file())
            self.assertTrue(result.v2_path.is_file())
            self.assertEqual(json.loads(result.result_path.read_text(encoding='utf-8'))['board'], result.board)


if __name__ == "__main__":
    unittest.main()
