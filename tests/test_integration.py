import copy
import queue
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from chess_board import NAME_TO_ID, PIECE_NAMES, board_to_fen, parse_uci_move
from camera_receiver import CameraReceiver
from chess_engine import PikafishEngine
from chess_pipeline import ChessPipeline
from config import ENGINE_PATH, PROJECT_DIR
from game_controller import GameController, Turn
from physical_game import PhysicalGameSession
from robot_controller import RobotController
from vision_processor import BoardDetector, detections_to_board, find_player_move, read_image

START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
CORNERS = [(50, 50), (850, 50), (850, 950), (50, 950)]
NAMES = dict(enumerate(PIECE_NAMES))


def initial_board():
    board = [[0] * 9 for _ in range(10)]
    board[0] = [9, 12, 10, 11, 13, 11, 10, 12, 9]
    board[2][1] = board[2][7] = 8
    board[3] = [14, 0, 14, 0, 14, 0, 14, 0, 14]
    board[9] = [2, 5, 3, 4, 6, 4, 3, 5, 2]
    board[7][1] = board[7][7] = 1
    board[6] = [7, 0, 7, 0, 7, 0, 7, 0, 7]
    return board


def box(x, y, piece):
    return SimpleNamespace(xyxy=np.array([[x - 15, y - 15, x + 15, y + 15]]), cls=[piece - 1])


def initial_boxes():
    return [box(50 + col * 100, 50 + (9 - row) * 100, piece)
            for row, rank in enumerate(initial_board()) for col, piece in enumerate(rank) if piece]


class BoardTests(unittest.TestCase):
    def test_starting_fen_and_side(self):
        self.assertEqual(board_to_fen(initial_board()), START_FEN)
        self.assertEqual(board_to_fen(initial_board(), "b"), START_FEN.replace(" w ", " b "))

    def test_move_coordinates(self):
        self.assertEqual(parse_uci_move("b2e2"), ((1, 2), (4, 2)))
        self.assertEqual(parse_uci_move("i9a0"), ((8, 9), (0, 0)))
        for move in ("j0a0", "a0a0", "e2e4q", "0000", "(none)"):
            with self.subTest(move=move), self.assertRaises(ValueError):
                parse_uci_move(move)

    def test_invalid_recognition_is_not_sent_to_engine(self):
        bad_boards = []
        missing_king = initial_board()
        missing_king[0][4] = 0
        bad_boards.append(missing_king)
        wrong_king = initial_board()
        wrong_king[0][4], wrong_king[4][4] = 0, 13
        bad_boards.append(wrong_king)
        extra_rook = initial_board()
        extra_rook[1][1] = 9
        bad_boards.append(extra_rook)
        bad_boards.append([[0] * 8 for _ in range(10)])
        bad_boards.append([[99] * 9 for _ in range(10)])
        facing = [[0] * 9 for _ in range(10)]
        facing[0][4], facing[9][4] = 13, 6
        bad_boards.append(facing)
        engine = Mock()
        for board in bad_boards:
            with self.subTest(board=board), self.assertRaises(ValueError):
                ChessPipeline(engine=engine).suggest_move(board)
        engine.start.assert_not_called()

    def test_player_capture_and_identity(self):
        previous = initial_board()
        current = copy.deepcopy(previous)
        current[2][1], current[7][1] = 0, 8
        self.assertEqual(find_player_move(previous, current), ((1, 2), (1, 7)))
        current[7][1] = 9
        with self.assertRaises(ValueError):
            find_player_move(previous, current)


class VisionTests(unittest.TestCase):
    def test_grid_intersections_and_fen(self):
        board = detections_to_board(initial_boxes(), NAMES, CORNERS)
        self.assertEqual(board, initial_board())
        self.assertEqual(board_to_fen(board), START_FEN)

    def test_rotated_image(self):
        rotated = [box(850 - col * 100, 50 + row * 100, piece)
                   for row, rank in enumerate(initial_board()) for col, piece in enumerate(rank) if piece]
        self.assertEqual(detections_to_board(rotated, NAMES, CORNERS, False), initial_board())

    def test_perspective_and_class_name_mapping(self):
        import cv2
        corners = np.float32([[100, 100], [800, 50], [900, 900], [50, 950]])
        transform = cv2.getPerspectiveTransform(np.float32([[0, 0], [8, 0], [8, 9], [0, 9]]), corners)
        points = np.float32([[[0, 9]], [[4, 0]]])
        warped = cv2.perspectiveTransform(points, transform)
        boxes = [box(*warped[0, 0], 1), box(*warped[1, 0], 2)]
        board = detections_to_board(boxes, {0: "chess-red-car", 1: "chess-black-king"}, corners)
        self.assertEqual(board[0][0], 9)
        self.assertEqual(board[9][4], 6)

    def test_outside_pieces_ignored_and_ambiguity_rejected(self):
        board = detections_to_board([box(1200, 500, 9)], NAMES, CORNERS)
        self.assertFalse(any(piece for row in board for piece in row))
        for boxes in ([box(50, 950, 9), box(51, 949, 2)], [box(90, 950, 9)]):
            with self.assertRaises(ValueError):
                detections_to_board(boxes, NAMES, CORNERS)

    def test_bad_corners(self):
        for corners in (None, CORNERS[:3], [CORNERS[i] for i in (0, 2, 1, 3)], [(0, 0)] * 4):
            with self.subTest(corners=corners), self.assertRaises(ValueError):
                detections_to_board([], NAMES, corners)

    def test_chinese_image_path_and_detector_reuse(self):
        import cv2
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as directory:
            path = Path(directory) / "棋盤圖片.png"
            cv2.imencode(".png", np.zeros((1000, 900, 3), dtype=np.uint8))[1].tofile(str(path))
            self.assertEqual(read_image(path).shape, (1000, 900, 3))
            detector = BoardDetector()
            detector._model = Mock(names=NAMES)
            detector._model.predict.return_value = [SimpleNamespace(boxes=initial_boxes())]
            for _ in range(2):
                self.assertEqual(detector.detect(path, CORNERS), initial_board())
            self.assertEqual(detector._model.predict.call_count, 2)
            self.assertEqual(detector._model.predict.call_args.kwargs['device'], 'cpu')


class PipelineTests(unittest.TestCase):
    def test_detection_to_engine_and_cleanup(self):
        detector = Mock()
        detector.detect.return_value = initial_board()
        engine = Mock()
        engine.best_move.return_value = "b2e2"
        pipeline = ChessPipeline(detector, engine)
        board = pipeline.recognize("board.png", CORNERS)
        result = pipeline.suggest_move(board)
        self.assertEqual(result.source, (1, 2))
        self.assertEqual(result.destination, (4, 2))
        self.assertEqual(board, initial_board())
        engine.best_move.assert_called_once_with("fen " + START_FEN, 4, 500)
        engine.stop.assert_called_once()

    def test_engine_failure_stops_process_without_changing_board(self):
        engine = Mock()
        engine.best_move.side_effect = TimeoutError("timeout")
        board = initial_board()
        with self.assertRaises(TimeoutError):
            ChessPipeline(engine=engine).suggest_move(board)
        engine.stop.assert_called_once()
        self.assertEqual(board, initial_board())

    def test_end_of_game_and_invalid_engine_move(self):
        for move in ("0000", "(none)"):
            engine = Mock()
            engine.best_move.return_value = move
            self.assertIsNone(ChessPipeline(engine=engine).suggest_move(initial_board()).source)
        for move in ("a1a2", "a9a8", "a0b0", "bad"):
            engine = Mock()
            engine.best_move.return_value = move
            with self.subTest(move=move), self.assertRaises(ValueError):
                ChessPipeline(engine=engine).suggest_move(initial_board())


class GameControllerTests(unittest.TestCase):
    def setUp(self):
        self.moves = []
        self.last_suggest_side = None
        self.suggestion = SimpleNamespace(
            source=(1, 2), destination=(4, 2), captured_piece=0
        )
        def mock_suggest(board, side, depth):
            self.last_suggest_side = side
            return self.suggestion

        self.controller = GameController(
            mock_suggest,
            lambda source, destination, captured: self.moves.append(
                (source, destination, captured)
            ),
        )

    def test_player_first_waits_without_robot_move(self):
        self.assertEqual(self.controller.start(initial_board(), "PLAYER"), Turn.PLAYER)
        self.assertEqual(self.controller.player_side, "w")
        self.assertEqual(self.controller.robot_side, "b")
        self.assertEqual(self.controller.side, "w")
        self.assertFalse(self.moves)

        # Player moves Red cannon from (1, 2) to (4, 2)
        new_board = copy.deepcopy(initial_board())
        new_board[2][4], new_board[2][1] = new_board[2][1], 0
        suggestion = self.controller.player_finished(new_board)
        self.assertEqual(self.last_suggest_side, "b")  # Robot plays Black
        self.assertEqual(self.controller.turn, Turn.PLAYER)
        self.assertEqual(self.controller.side, "w")  # Next is Player (Red)
        self.assertEqual(len(self.moves), 1)

    def test_player_moving_opponent_piece_raises(self):
        self.controller.start(initial_board(), "PLAYER")  # Player is Red
        # Player mistakenly moves a Black cannon at row 7
        bad_board = copy.deepcopy(initial_board())
        bad_board[7][4], bad_board[7][1] = bad_board[7][1], 0
        with self.assertRaisesRegex(ValueError, "請移動己方棋子"):
            self.controller.player_finished(bad_board)

    def test_robot_first_executes_one_move_and_passes_turn(self):
        self.assertEqual(self.controller.start(initial_board(), "ROBOT"), Turn.ROBOT)
        self.assertEqual(self.controller.robot_side, "w")
        self.assertEqual(self.controller.player_side, "b")
        self.assertEqual(self.controller.side, "w")
        self.controller.robot_turn()
        self.assertEqual(self.last_suggest_side, "w")  # Robot plays Red
        self.assertEqual(self.controller.side, "b")    # Next is Player (Black)
        self.assertEqual(self.controller.turn, Turn.PLAYER)
        self.assertEqual(self.moves, [((1, 2), (4, 2), None)])

    def test_rejects_turn_out_of_order(self):
        with self.assertRaises(RuntimeError):
            self.controller.robot_turn()


class CameraReceiverTests(unittest.TestCase):
    def test_receives_image_and_releases_waiter(self):
        import cv2

        receiver = CameraReceiver(port=0)
        receiver.start()
        try:
            image = np.zeros((8, 9, 3), dtype=np.uint8)
            success, encoded = cv2.imencode(".jpg", image)
            self.assertTrue(success)
            request = urllib.request.Request(
                f"http://127.0.0.1:{receiver.port}/api/DET",
                data=encoded.tobytes(),
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 200)
            received = receiver.wait_for_image(timeout=2)
            self.assertEqual(received.shape, image.shape)
        finally:
            receiver.stop()

    def test_clear_resets_cached_image(self):
        import cv2

        receiver = CameraReceiver(port=0)
        receiver.start()
        try:
            image = np.zeros((8, 9, 3), dtype=np.uint8)
            success, encoded = cv2.imencode(".jpg", image)
            self.assertTrue(success)
            request = urllib.request.Request(
                f"http://127.0.0.1:{receiver.port}/api/DET",
                data=encoded.tobytes(),
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 200)
            receiver.clear()
            with self.assertRaises(TimeoutError):
                receiver.wait_for_image(timeout=0.05)
        finally:
            receiver.stop()


class RobotControllerTests(unittest.TestCase):
    def test_robot_rpc_capture_and_move_are_separate(self):
        robot = RobotController()
        with patch.object(robot, "send_script") as send_script:
            robot.trigger_capture()
        send_script.assert_called_once_with("ScriptExit()")

    def test_script_packet_has_calculated_checksum(self):
        robot = RobotController()
        robot.simulation = False
        robot.sock = Mock()
        robot.sock.recv.return_value = b"OK"
        robot.send_script("ScriptExit()")
        packet = robot.sock.sendall.call_args.args[0].decode("utf-8")
        body = "TMSCT,12,ScriptExit(),"
        checksum = 0
        for byte in body.encode("utf-8"):
            checksum ^= byte
        self.assertEqual(packet, f"${body}*{checksum:02X}\r\n")

    def test_send_script_reconnects_on_broken_pipe(self):
        robot = RobotController()
        robot.simulation = False
        broken_sock = Mock()
        broken_sock.sendall.side_effect = BrokenPipeError("socket dead")
        working_sock = Mock()
        working_sock.recv.return_value = b"OK"
        robot.sock = broken_sock
        with patch.object(robot, "_ensure_connected") as mock_ensure:
            def swap_sock(*args, **kwargs):
                robot.sock = working_sock
            mock_ensure.side_effect = swap_sock
            res = robot.send_script("Line()")
            self.assertEqual(res, b"OK")
            working_sock.sendall.assert_called_once()

    def test_remove_grid_piece_high_altitude_drop(self):
        from config import GRAVEYARD_DROP_POS, Z_SAFE
        robot = RobotController()
        robot.simulation = True
        with patch.object(robot, "_line") as mock_line, patch.object(
            robot, "set_gripper"
        ) as mock_gripper:
            robot.remove_grid_piece((0, 0), 1)
            mock_line.assert_any_call(GRAVEYARD_DROP_POS, Z_SAFE)
            self.assertEqual(
                mock_gripper.call_args_list,
                [unittest.mock.call(True), unittest.mock.call(False)],
            )


class PhysicalGameTests(unittest.TestCase):
    def test_robot_first_captures_moves_and_verifies(self):
        board = initial_board()
        after = copy.deepcopy(board)
        after[2][1], after[4][1] = 0, 8
        suggestion = SimpleNamespace(
            source=(1, 2), destination=(1, 4), captured_piece=0
        )
        pipeline = Mock()
        pipeline.suggest_move.return_value = suggestion
        pipeline.recognize_image.side_effect = [board, after]
        robot = Mock()
        camera = Mock()
        session = PhysicalGameSession(pipeline, robot, camera, CORNERS)
        self.assertEqual(session.start("ROBOT"), Turn.PLAYER)
        self.assertEqual(robot.trigger_capture.call_count, 2)
        robot.execute_chess_move.assert_called_once_with((1, 2), (1, 4), None)

    def test_failed_verification_can_retry_same_move(self):
        board = initial_board()
        after = copy.deepcopy(board)
        after[2][1], after[4][1] = 0, 8
        pipeline = Mock()
        pipeline.suggest_move.return_value = SimpleNamespace(
            source=(1, 2), destination=(1, 4), captured_piece=0
        )
        pipeline.recognize_image.side_effect = [board, board, after]
        robot = Mock()
        camera = Mock()
        session = PhysicalGameSession(pipeline, robot, camera, CORNERS)
        with self.assertRaises(ValueError):
            session.start("ROBOT")
        session.retry_last_move()
        self.assertEqual(robot.execute_chess_move.call_count, 2)
        self.assertEqual(pipeline.suggest_move.call_count, 1)


class EngineTests(unittest.TestCase):
    def test_missing_weight_preflight(self):
        engine = PikafishEngine(nnue_path=PROJECT_DIR / "does-not-exist.nnue")
        with self.assertRaisesRegex(FileNotFoundError, "NNUE"):
            engine.start()
        self.assertIsNone(engine.process)

    def test_timeout_exit_error_and_exact_marker(self):
        engine = PikafishEngine(timeout=0.02)
        engine._lines.put("info string contains bestmove but is not a result")
        with self.assertRaises(TimeoutError):
            engine._wait_for("bestmove")
        engine._lines.put(None)
        with self.assertRaises(RuntimeError):
            engine._wait_for("bestmove")
        engine._lines.put("info string ERROR: incompatible network")
        with self.assertRaisesRegex(RuntimeError, "incompatible"):
            engine._wait_for("readyok")

    def test_protocol_with_real_subprocess_transport(self):
        script = '''import sys
for line in sys.stdin:
    line = line.strip()
    if line == "uci": print("uciok", flush=True)
    elif line == "isready": print("readyok", flush=True)
    elif line.startswith("position "):
        if line != "position fen " + sys.argv[1]:
            print("info string ERROR: wrong FEN", flush=True)
    elif line.startswith("go "):
        if line != "go depth 4 movetime 500":
            print("info string ERROR: wrong search limits", flush=True)
        else: print("bestmove b2e2", flush=True)
    elif line == "quit": break
'''
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as directory:
            directory = Path(directory)
            executable, net = directory / "pikafish.exe", directory / "pikafish.nnue"
            executable.write_bytes(b"test-only placeholder")
            net.write_bytes(b"test-only placeholder")
            transport = subprocess.Popen
            def launch(*args, **kwargs):
                return transport([sys.executable, "-u", "-c", script, START_FEN], **kwargs)
            engine = PikafishEngine(executable, net, timeout=3)
            with patch("chess_engine.subprocess.Popen", side_effect=launch):
                result = ChessPipeline(engine=engine).suggest_move(initial_board())
            self.assertEqual(result.move, "b2e2")
            self.assertIsNone(engine.process)

    @unittest.skipUnless(ENGINE_PATH.is_file(), "Pikafish binary not installed")
    def test_real_pikafish_accepts_generated_fen(self):
        result = subprocess.run([str(ENGINE_PATH)],
                                input=f"uci\nposition fen {board_to_fen(initial_board())}\nd\nquit\n",
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                cwd=ENGINE_PATH.parent, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("uciok", result.stdout)
        self.assertIn("Fen: " + START_FEN, result.stdout)


if __name__ == "__main__":
    unittest.main()
