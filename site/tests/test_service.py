import threading
import time
import tempfile
import unittest
from pathlib import Path

from tmchess.board import Move, apply_move
from tmchess.config import CONFIG, SITE_ROOT
from tmchess.engine import AnalysisLine, EngineAnalysis
from tmchess.robot import GripperStateUnknown
from tmchess.service import CommandConflict, GameService, StatePersistenceError
from tmchess.session import GameSessionStore
from tmchess.state import Phase
from tests.fakes import FakeEngine, FakePreflight, FakeRobot, FakeVision, make_service, starting_board, wait_for


class ServiceTests(unittest.TestCase):
    def test_capture_trigger_failure_records_stage_without_robot_move(self):
        class TriggerFailure(FakeRobot):
            def trigger_capture(self, stop_event):
                raise ConnectionError('TCP unavailable')
        robot = TriggerFailure()
        self.service, *_ = make_service([], robot=robot)
        self.service.submit_start('player')
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_player_done()
        wait_for(self.service, Phase.ERROR.value)
        record = self.service._capture_history[-1]
        self.assertEqual(record['failure_stage'], 'capture_trigger')
        self.assertGreaterEqual(record['trigger_seconds'], 0)
        self.assertEqual(robot.moves, [])

    def test_stationary_failure_records_full_workflow_duration(self):
        class StationaryFailure(FakeRobot):
            def confirm_stationary(self):
                raise TimeoutError('TCP unavailable')
        robot = StationaryFailure()
        observed = apply_move(starting_board(), Move((0, 3), (0, 4), 0, 'a3a4'))
        self.service, *_ = make_service([observed], robot=robot)
        self.service.submit_start('player')
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_player_done()
        wait_for(self.service, Phase.ERROR.value)
        record = self.service._capture_history[-1]
        self.assertEqual(record['failure_stage'], 'stationary_confirmation')
        self.assertGreaterEqual(record['workflow_seconds'], record['stationary_confirmation_seconds'])
        self.assertIsNotNone(record['observed_board'])
        self.assertEqual(robot.moves, [])

    def test_two_rounds_capture_only_player_moves(self):
        first_player = Move((0, 3), (0, 4), 0, "a3a4")
        first_ai = Move((0, 6), (0, 5), 0, "a6a5")
        second_player = Move((2, 3), (2, 4), 0, "c3c4")
        second_ai = Move((2, 6), (2, 5), 0, "c6c5")
        observed1 = apply_move(starting_board(), first_player)
        observed2 = apply_move(apply_move(observed1, first_ai), second_player)
        self.service, robot, vision, _ = make_service([observed1, observed2], [first_ai, second_ai])
        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)
        for _ in range(2):
            self.service.submit_player_done()
            wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertEqual(vision.calls, 2)
        self.assertEqual(robot.moves, [first_ai, second_ai])
        self.assertEqual(self.service._board, apply_move(observed2, second_ai))
        self.assertEqual(len(self.service._move_history), 4)
        self.assertTrue(all(m['verification_status'] == 'skipped' for m in self.service._motion_history))

    def tearDown(self):
        if hasattr(self, "service"):
            self.service.close()

    def test_player_first_full_round(self):
        initial = starting_board()
        player_move = Move((0, 3), (0, 4), 0, "a3a4")
        after_player = apply_move(initial, player_move)
        ai_move = Move((0, 6), (0, 5), 0, "a6a5")
        expected = apply_move(after_player, ai_move)
        self.service, robot, _vision, engine = make_service([after_player, expected], [ai_move])
        self.service.submit_start("player")
        state = wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertEqual(state["first_actor"], "player")
        self.assertTrue(state["controls"]["can_player_done"])
        self.service.submit_player_done()
        state = wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertEqual(state["last_move"]["uci"], "a6a5")
        self.assertEqual(engine.suggest_calls, 1)
        self.assertEqual(len(robot.moves), 1)

    def test_hint_returns_analysis_without_capture_or_robot_motion(self):
        hint = Move((1, 2), (4, 2), 0, "b2e2")
        line = AnalysisLine(
            multipv=1,
            depth=8,
            seldepth=10,
            score_type="cp",
            score=23,
            bound=None,
            wdl=(90, 900, 10),
            nodes=12000,
            nps=800000,
            time_ms=15,
            pv=("b2e2", "b9c7"),
        )
        self.service, robot, vision, engine = make_service(
            [], analysis=EngineAnalysis(hint, "b9c7", (line,))
        )
        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)

        state = self.service.submit_hint()
        self.assertEqual(state["analysis"]["status"], "calculating")
        state = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(state["analysis"]["status"], "ready")
        self.assertEqual(state["analysis"]["bestmove"]["uci"], "b2e2")
        self.assertEqual(state["analysis"]["lines"][0]["red_win_per_mille"], 90)
        self.assertEqual(state["analysis"]["lines"][0]["draw_per_mille"], 900)
        self.assertEqual(state["analysis"]["lines"][0]["black_win_per_mille"], 10)
        self.assertEqual(engine.analyze_calls, 1)
        self.assertEqual(vision.calls, 0)
        self.assertEqual(robot.moves, [])

    def test_hint_failure_does_not_end_the_game(self):
        self.service, robot, vision, _engine = make_service([], engine_error="analyze")
        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)

        self.service.submit_hint()
        state = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(state["analysis"]["status"], "error")
        self.assertEqual(state["analysis"]["error"]["type"], "RuntimeError")
        self.assertIsNone(state["error"])
        self.assertTrue(state["controls"]["can_hint"])
        self.assertEqual(vision.calls, 0)
        self.assertEqual(robot.moves, [])

    def test_analysis_mode_keeps_both_sides_human_and_never_moves_robot(self):
        initial = starting_board()
        red_move = Move((0, 3), (0, 4), 0, "a3a4")
        after_red = apply_move(initial, red_move)
        self.service, robot, vision, engine = make_service([after_red])

        self.service.submit_start_mode("analysis")
        state = wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertEqual(state["controllers"], {"w": "human", "b": "human"})
        self.assertEqual(state["side_to_move"], "w")

        self.service.submit_player_done()
        state = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(state["side_to_move"], "b")
        self.assertEqual(state["last_move"]["side"], "w")
        self.assertTrue(state["controls"]["can_hint"])
        self.assertEqual(vision.calls, 1)
        self.assertEqual(engine.suggest_calls, 0)
        self.assertEqual(robot.moves, [])

    def test_engine_vs_engine_requires_confirmation_for_every_robot_move(self):
        initial = starting_board()
        red_move = Move((0, 3), (0, 4), 0, "a3a4")
        after_red = apply_move(initial, red_move)
        black_move = Move((0, 6), (0, 5), 0, "a6a5")
        after_black = apply_move(after_red, black_move)
        next_red = Move((2, 3), (2, 4), 0, "c3c4")
        self.service, robot, vision, engine = make_service(
            [initial, after_red, after_black],
            [red_move, black_move, next_red],
        )

        self.service.submit_start_mode("engine_vs_engine")
        state = wait_for(self.service, Phase.ENGINE_READY.value)

        self.assertEqual(state["side_to_move"], "w")
        self.assertEqual(state["controllers"], {"w": "engine", "b": "engine"})
        self.assertTrue(state["controls"]["can_execute_engine_move"])
        self.assertEqual(robot.moves, [])

        self.service.submit_execute_engine_move()
        state = wait_for(self.service, Phase.ENGINE_READY.value)
        self.assertEqual(state["side_to_move"], "b")
        self.assertEqual(robot.moves, [red_move])

        self.service.submit_execute_engine_move()
        state = wait_for(self.service, Phase.ENGINE_READY.value)
        self.assertEqual(state["side_to_move"], "w")
        self.assertEqual(robot.moves, [red_move, black_move])
        self.assertEqual(engine.suggest_calls, 3)
        self.assertEqual(vision.calls, 1)

    def test_invalid_game_mode_is_rejected_before_any_hardware_work(self):
        self.service, robot, vision, engine = make_service([])

        with self.assertRaises(CommandConflict) as caught:
            self.service.submit_start_mode("continuous")

        self.assertEqual(caught.exception.code, "invalid_game_mode")
        self.assertEqual(vision.calls, 0)
        self.assertEqual(robot.moves, [])
        self.assertEqual(engine.suggest_calls, 0)

    def test_start_current_uses_captured_board_as_new_player_turn_baseline(self):
        current = apply_move(
            starting_board(), Move((0, 3), (0, 4), 0, "a3a4")
        )
        self.service, robot, vision, engine = make_service([current])

        self.service.submit_start_current("red", "player")
        state = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(state["current_turn"], "player")
        self.assertEqual(self.service._board, current)
        self.assertEqual(self.service._player_side, "w")
        self.assertEqual(vision.calls, 1)
        self.assertEqual(robot.trigger_calls, 1)
        self.assertEqual(robot.moves, [])
        self.assertEqual(engine.suggest_calls, 0)
        self.assertEqual(engine.has_legal_calls, 1)

    def test_start_current_finishes_when_player_has_no_legal_move(self):
        current = apply_move(
            starting_board(), Move((0, 3), (0, 4), 0, "a3a4")
        )
        self.service, robot, vision, engine = make_service(
            [current], has_legal=False
        )

        self.service.submit_start_current("red", "player")
        state = wait_for(self.service, Phase.FINISHED.value)

        self.assertIsNone(state["current_turn"])
        self.assertEqual(state["message"], "目前局面玩家無合法著法，AI 獲勝。")
        self.assertEqual(vision.calls, 1)
        self.assertEqual(robot.moves, [])
        self.assertEqual(engine.has_legal_calls, 1)

    def test_start_current_ai_turn_calculates_from_captured_board(self):
        current = apply_move(
            starting_board(), Move((0, 3), (0, 4), 0, "a3a4")
        )
        ai_move = Move((0, 6), (0, 5), 0, "a6a5")
        expected = apply_move(current, ai_move)
        self.service, robot, vision, engine = make_service(
            [current, expected], [ai_move]
        )

        self.service.submit_start_current("red", "ai")
        state = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(state["last_move"]["uci"], "a6a5")
        self.assertEqual(vision.calls, 1)
        self.assertEqual(engine.suggest_calls, 1)
        self.assertEqual(robot.moves, [ai_move])

    def test_start_current_rejects_invalid_color_without_capture(self):
        self.service, robot, vision, engine = make_service([starting_board()])

        with self.assertRaises(CommandConflict) as caught:
            self.service.submit_start_current("green", "player")

        self.assertEqual(caught.exception.code, "invalid_player_color")
        self.assertEqual(vision.calls, 0)
        self.assertEqual(robot.trigger_calls, 0)
        self.assertEqual(engine.suggest_calls, 0)

    def test_start_persistence_failure_does_not_leave_service_busy(self):
        class BrokenStore:
            def load_latest(self):
                return None

            def create(self, _game_id):
                raise OSError("disk unavailable")

        self.service, _robot, vision, _engine = make_service(
            [], session_store=BrokenStore()
        )

        with self.assertRaises(StatePersistenceError):
            self.service.submit_start("player")

        state = self.service.get_state()
        self.assertEqual(state["error"]["code"], "state_persistence_failed")
        self.assertIsNone(state["game"]["id"])
        self.assertTrue(state["controls"]["can_start"])
        self.assertFalse(state["controls"]["can_stop"])
        self.assertEqual(vision.calls, 0)

    def test_failed_safe_stop_persistence_restores_motion_lock(self):
        game_id = "20261012T010203Z-12345678"
        board = starting_board()

        class BrokenStore:
            def load_latest(self):
                return {
                    "schema_version": 3,
                    "game_id": game_id,
                    "saved_at": "2026-10-12T01:02:03+00:00",
                    "phase": Phase.ROBOT_MOVING.value,
                    "board": board,
                    "initial_board": board,
                    "initial_side_to_move": "w",
                    "side_to_move": "w",
                    "move_history": [],
                    "motion_state_uncertain": True,
                }

            def save(self, _game_id, _payload):
                raise OSError("disk unavailable")

        self.service, *_ = make_service([], session_store=BrokenStore())

        with self.assertRaises(StatePersistenceError):
            self.service.submit_confirm_safe_stop(True)

        state = self.service.get_state()
        self.assertTrue(state["robot"]["motion_may_continue"])
        self.assertFalse(state["controls"]["can_resume"])
        self.assertEqual(state["error"]["code"], "state_persistence_failed")

    def test_failed_new_game_creation_does_not_overwrite_previous_session(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        self.service, *_ = make_service([], session_store=sessions)
        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_stop()
        wait_for(self.service, Phase.STOPPED.value)
        previous = sessions.load_latest()

        class FailCreateStore:
            def create(self, _game_id):
                raise OSError("disk unavailable")

        self.service.session_store = FailCreateStore()
        with self.assertRaises(StatePersistenceError):
            self.service.submit_start("player")
        self.service.close()

        self.assertEqual(sessions.load_latest()["board"], previous["board"])

    def test_start_current_creates_new_folder_and_preserves_old_game(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        self.service, _robot, _vision, _engine = make_service(
            [starting_board()], session_store=sessions
        )

        self.service.submit_start("player")
        first = wait_for(self.service, Phase.PLAYER_TURN.value)
        first_game_id = first["game"]["id"]
        self.service.submit_stop()
        wait_for(self.service, Phase.STOPPED.value)

        self.service.submit_start_current("red", "player", continuation="new")
        second = wait_for(self.service, Phase.PLAYER_TURN.value)
        second_game_id = second["game"]["id"]

        self.assertNotEqual(first_game_id, second_game_id)
        folders = {path.name for path in Path(temporary.name).iterdir() if path.is_dir()}
        self.assertEqual(folders, {first_game_id, second_game_id})

    @staticmethod
    def confidence_grid(default=None):
        return [[default for _ in range(9)] for _ in range(10)]

    def test_player_move_restores_unrelated_low_confidence_same_color_label(self):
        initial = starting_board()
        player_move = Move((0, 3), (0, 4), 0, "a3a4")
        after_player = apply_move(initial, player_move)
        noisy = [row[:] for row in after_player]
        noisy[3][4] = 12  # Red soldier misclassified as a red horse.
        confidence = self.confidence_grid()
        confidence[3][4] = 0.26
        ai_move = Move((0, 6), (0, 5), 0, "a6a5")
        expected = apply_move(after_player, ai_move)
        self.service, robot, _vision, engine = make_service(
            [(noisy, confidence), expected], [ai_move]
        )

        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_player_done()
        state = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(state["last_move"]["uci"], "a6a5")
        self.assertEqual(engine.legal_calls, 2)  # Player move plus AI bestmove guard.
        self.assertEqual(len(robot.moves), 1)

    def test_player_move_restores_unrelated_label_after_unique_move_is_identified(self):
        initial = starting_board()
        after_player = apply_move(initial, Move((0, 3), (0, 4), 0, "a3a4"))
        noisy = [row[:] for row in after_player]
        noisy[3][4] = 12
        confidence = self.confidence_grid()
        confidence[3][4] = 0.90
        self.service, robot, _vision, engine = make_service([(noisy, confidence)])

        self.service.submit_start_mode("analysis")
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_player_done()
        state = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertIsNone(state["error"])
        self.assertEqual(engine.legal_calls, 1)
        self.assertEqual(self.service._board, after_player)
        self.assertEqual(state["last_move"]["vision_repaired_cells"], ["e3"])
        self.assertEqual(robot.moves, [])

    def test_player_move_does_not_hide_second_physical_move(self):
        initial = starting_board()
        observed = apply_move(initial, Move((0, 3), (0, 4), 0, "a3a4"))
        observed = apply_move(observed, Move((2, 3), (2, 4), 0, "c3c4"))
        confidence = self.confidence_grid(0.10)
        self.service, robot, _vision, engine = make_service([(observed, confidence)])

        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_player_done()
        state = wait_for(self.service, Phase.ERROR.value)

        self.assertEqual(state["error"]["code"], "workflow_failed")
        self.assertEqual(engine.legal_calls, 0)
        self.assertEqual(robot.moves, [])

    def test_ai_turn_does_not_consume_post_move_noisy_observation(self):
        initial = starting_board()
        ai_move = Move((0, 3), (0, 4), 0, "a3a4")
        expected = apply_move(initial, ai_move)
        noisy = [row[:] for row in expected]
        noisy[3][4] = 12
        confidence = self.confidence_grid()
        confidence[3][4] = 0.26
        self.service, robot, _vision, engine = make_service(
            [initial, (noisy, confidence)], [ai_move]
        )

        self.service.submit_start("ai")
        state = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(state["last_move"]["uci"], "a3a4")
        self.assertEqual(engine.suggest_calls, 1)
        self.assertEqual(len(robot.moves), 1)

    def test_start_uses_completed_preflight_without_running_it_again(self):
        preflight = FakePreflight(True)
        robot = FakeRobot()
        vision = FakeVision([])
        self.service = GameService(
            CONFIG,
            robot,
            vision,
            FakeEngine(),
            preflight_checker=preflight,
            start_preflight_monitor=False,
        )

        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(preflight.calls, [False])
        self.assertEqual(vision.calls, 0)
        self.assertEqual(robot.trigger_calls, 0)
        self.assertEqual(robot.moves, [])

    def test_startup_monitor_runs_dynamic_preflight_before_ready(self):
        preflight = FakePreflight(True)
        self.service = GameService(
            CONFIG,
            FakeRobot(),
            FakeVision([]),
            FakeEngine(),
            preflight_checker=preflight,
            start_preflight_monitor=True,
        )
        deadline = time.monotonic() + 1
        while True not in preflight.calls and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(preflight.calls, [False, True])
        self.assertTrue(self.service.get_state()["preflight"]["ready"])

    def test_close_waits_for_preflight_before_releasing_robot(self):
        entered = threading.Event()
        release = threading.Event()

        class BlockingPreflight(FakePreflight):
            def run(self, dynamic=True):
                if dynamic:
                    entered.set()
                    release.wait(timeout=2)
                return super().run(dynamic)

        class CloseTrackingRobot(FakeRobot):
            def __init__(self):
                super().__init__()
                self.closed = False

            def close(self):
                self.closed = True

        robot = CloseTrackingRobot()
        self.service = GameService(
            CONFIG,
            robot,
            FakeVision([]),
            FakeEngine(),
            preflight_checker=BlockingPreflight(True),
            start_preflight_monitor=True,
        )
        self.assertTrue(entered.wait(timeout=1))

        closer = threading.Thread(target=self.service.close)
        closer.start()
        time.sleep(0.05)

        self.assertFalse(robot.closed)
        release.set()
        closer.join(timeout=2)
        self.assertFalse(closer.is_alive())
        self.assertTrue(robot.closed)

    def test_player_first_start_does_not_capture_think_or_move(self):
        self.service, robot, vision, engine = make_service([])

        self.service.submit_start("player")
        state = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(state["current_turn"], "player")
        self.assertTrue(state["controls"]["can_player_done"])
        self.assertEqual(vision.calls, 0)
        self.assertEqual(robot.trigger_calls, 0)
        self.assertEqual(robot.moves, [])
        self.assertEqual(engine.suggest_calls, 0)

    def test_ai_first_means_ai_is_red(self):
        initial = starting_board()
        ai_move = Move((0, 3), (0, 4), 0, "a3a4")
        expected = apply_move(initial, ai_move)
        self.service, _robot, _vision, engine = make_service([initial, expected], [ai_move])
        self.service.submit_start("ai")
        state = wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertEqual(state["first_actor"], "ai")
        self.assertEqual(state["last_move"]["uci"], "a3a4")
        self.assertEqual(engine.suggest_calls, 1)

    def test_ai_first_rejects_valid_nonstandard_initial_board_before_motion(self):
        nonstandard = apply_move(
            starting_board(), Move((0, 3), (0, 4), 0, "a3a4")
        )
        self.service, robot, _vision, engine = make_service([nonstandard])

        self.service.submit_start("ai")
        state = wait_for(self.service, Phase.ERROR.value)

        self.assertIn("標準初始盤面", state["error"]["message"])
        self.assertEqual(engine.suggest_calls, 0)
        self.assertEqual(robot.moves, [])

    def test_illegal_sequence_rejected(self):
        self.service, *_ = make_service([starting_board()])
        with self.assertRaises(CommandConflict) as caught:
            self.service.submit_player_done()
        self.assertEqual(caught.exception.code, "not_player_turn")

    def test_recorded_player_point_uses_same_snake_order_as_90_point_inspection(self):
        self.service, *_ = make_service([])

        details = self.service._position_details((8, 0))

        self.assertEqual(details["player_matrix"], "R10C1")
        self.assertEqual(details["player_point"], 90)

    def test_duplicate_start_not_enqueued(self):
        blocker = threading.Event()
        self.service, *_ = make_service([starting_board()], blocker=blocker)
        self.service.submit_start("ai")
        with self.assertRaises(CommandConflict):
            self.service.submit_start("ai")
        blocker.set()
        wait_for(self.service, Phase.FINISHED.value)

    def test_no_legal_ai_move_finishes(self):
        initial = starting_board()
        self.service, *_ = make_service([initial], [None])
        self.service.submit_start("ai")
        state = wait_for(self.service, Phase.FINISHED.value)
        self.assertIn("無合法著法", state["message"])

    def test_verified_ai_move_finishes_when_player_has_no_legal_move(self):
        initial = starting_board()
        move = Move((0, 3), (0, 4), 0, "a3a4")
        expected = apply_move(initial, move)
        self.service, _robot, _vision, engine = make_service(
            [initial, expected], [move], has_legal=False
        )

        self.service.submit_start("ai")
        state = wait_for(self.service, Phase.FINISHED.value)

        self.assertEqual(engine.has_legal_calls, 1)
        self.assertIn("AI 獲勝", state["message"])

    def test_ai_turn_is_persisted_before_pikafish_failure(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        self.service, *_ = make_service(
            [starting_board()], engine_error="suggest", session_store=sessions
        )

        self.service.submit_start("ai")
        wait_for(self.service, Phase.ERROR.value)

        self.assertEqual(sessions.load_latest()["current_turn"], "ai")

    def test_capture_move_is_preserved_for_robot(self):
        board = [[0 for _ in range(9)] for _ in range(10)]
        board[0][4], board[9][4] = 13, 6
        board[4][0], board[5][0] = 9, 7
        move = Move((0, 4), (0, 5), 7, "a4a5")
        expected = apply_move(board, move)
        self.service, robot, *_ = make_service([expected], [move])
        self.service._board = board
        self.service._initial_board = [row[:] for row in board]
        self.service._initial_side_to_move = self.service._ai_side
        self.service._side_to_move = self.service._ai_side
        self.service._perform_ai_turn()
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertEqual(robot.moves[0].captured_piece, 7)

    def test_completed_motion_commits_without_second_capture(self):
        after_player = apply_move(starting_board(), Move((0, 3), (0, 4), 0, "a3a4"))
        ai_move = Move((0, 6), (0, 5), 0, "a6a5")
        self.service, robot, vision, engine = make_service(
            [after_player, AssertionError("second capture forbidden")], [ai_move])
        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_player_done()
        state = wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertEqual(vision.calls, 1)
        self.assertEqual(robot.moves, [ai_move])
        self.assertEqual(engine.suggest_calls, 1)
        self.assertEqual(self.service._board, apply_move(after_player, ai_move))
        self.assertIsNone(self.service._pending_move)
        self.assertFalse(state["controls"]["can_retry"])
        self.assertEqual(state["last_move"]["confirmation_method"], "motion_completed")
        motion = self.service._motion_history[-1]
        self.assertEqual(motion["verification_status"], "skipped")
        self.assertIsNone(motion["verification_capture_id"])
        self.assertIsNone(state["last_move"]["capture_id"])
        self.assertEqual(self.service._capture_history[-1]["accepted_uci"], "a3a4")
        self.assertEqual(self.service._capture_history[-1]["accepted_board"], after_player)

    def test_stop_after_completed_motion_allows_current_board_new_game(self):
        after_player = apply_move(starting_board(), Move((0, 3), (0, 4), 0, "a3a4"))
        ai_move = Move((0, 6), (0, 5), 0, "a6a5")
        actual = apply_move(after_player, ai_move)
        self.service, robot, vision, engine = make_service([after_player, actual], [ai_move])
        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_player_done()
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_stop()
        wait_for(self.service, Phase.STOPPED.value)
        self.service.submit_start_current("red", "player", continuation="new")
        recovered = wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertIsNone(recovered["error"])
        self.assertEqual(self.service._board, actual)
        self.assertEqual(robot.moves, [ai_move])
        self.assertEqual(vision.calls, 2)

    def test_stopped_verification_error_cannot_bypass_motion_lock(self):
        self.service, _robot, _vision, _engine = make_service([])
        self.service.store.update(lambda state: (
            state.update(phase=Phase.STOPPED.value, error={"retryable": True}),
            state["robot"].update(motion_may_continue=True),
        ))
        self.assertFalse(self.service.get_state()["controls"]["can_start_current"])
        with self.assertRaises(CommandConflict):
            self.service.submit_start_current("red", "player")

    def test_retry_commits_interrupted_move_if_already_done(self):
        initial = starting_board()
        move = Move((0, 3), (0, 4), 0, "a3a4")
        expected = apply_move(initial, move)
        robot = FakeRobot(error=ConnectionResetError("interrupted motion"))
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        self.service, _, vision, engine = make_service(
            [initial, initial, expected], [move], robot=robot, session_store=sessions)
        self.service.submit_start("ai")
        wait_for(self.service, Phase.ERROR.value)
        deadline = time.monotonic() + 5
        while not self.service.get_state()["controls"]["can_confirm_safe_stop"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.service.get_state()["controls"]["can_confirm_safe_stop"])
        robot.error = None
        self.service.submit_confirm_safe_stop(True)
        self.service.submit_resume("ai")
        wait_for(self.service, Phase.ERROR.value)
        self.service.submit_retry()
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertEqual(engine.suggest_calls, 1)
        self.assertEqual(robot.moves, [move])
        self.assertEqual(vision.calls, 3)

    def test_initial_capture_stationary_failure_still_blocks_motion(self):
        class UncertainCaptureRobot(FakeRobot):
            def confirm_stationary(self):
                raise TimeoutError("TMflow not back at Listen")
        self.service, robot, vision, engine = make_service(
            [starting_board()], [Move((0, 3), (0, 4), 0, "a3a4")], robot=UncertainCaptureRobot())
        self.service.submit_start("ai")
        state = wait_for(self.service, Phase.ERROR.value)
        self.assertTrue(state["robot"]["motion_may_continue"])
        self.assertFalse(state["controls"]["can_retry"])
        self.assertTrue(state["controls"]["can_confirm_safe_stop"])
        self.assertEqual(robot.moves, [])

    def test_yolo_failure_is_structured_error(self):
        self.service, *_ = make_service([RuntimeError("YOLO failed")])
        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_player_done()
        state = wait_for(self.service, Phase.ERROR.value)
        self.assertEqual(state["error"]["code"], "workflow_failed")
        self.assertFalse(state["error"]["retryable"])

    def test_failed_capture_records_its_own_id_and_reason(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        self.service, *_ = make_service(
            [RuntimeError("YOLO failed")],
            session_store=sessions,
        )
        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_player_done()
        state = wait_for(self.service, Phase.ERROR.value)

        snapshot = sessions.load_latest()
        attempt = snapshot["capture_history"][-1]
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(attempt["error_type"], "RuntimeError")
        self.assertEqual(attempt["error_message"], "YOLO failed")
        self.assertEqual(state["vision"]["last_attempt_id"], attempt["capture_id"])
        self.assertEqual(snapshot["event_history"][-1]["capture_id"], attempt["capture_id"])

    def test_pikafish_failure(self):
        initial = starting_board()
        self.service, *_ = make_service([initial], engine_error="suggest")
        self.service.submit_start("ai")
        state = wait_for(self.service, Phase.ERROR.value)
        self.assertIn("Pikafish", state["error"]["message"])

    def test_illegal_engine_suggestion_is_rejected_before_robot_motion(self):
        initial = starting_board()
        invalid = Move((0, 3), (1, 3), 0, "a3b3")
        self.service, robot, _vision, engine = make_service(
            [initial], [invalid], legal=False
        )

        self.service.submit_start("ai")
        state = wait_for(self.service, Phase.ERROR.value)

        self.assertIn("二次合法性驗證", state["error"]["message"])
        self.assertEqual(engine.legal_calls, 1)
        self.assertEqual(robot.moves, [])

    def test_engine_receives_committed_move_history(self):
        initial = starting_board()
        player_move = Move((0, 3), (0, 4), 0, "a3a4")
        after_player = apply_move(initial, player_move)
        ai_move = Move((0, 6), (0, 5), 0, "a6a5")
        expected = apply_move(after_player, ai_move)
        self.service, *_rest, engine = make_service([after_player, expected], [ai_move])

        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_player_done()
        wait_for(self.service, Phase.PLAYER_TURN.value)

        suggest_context = next(context for name, _side, context in engine.contexts if name == "suggest")
        self.assertEqual(suggest_context["moves"], ("a3a4",))
        self.assertEqual(suggest_context["initial_side"], "w")
        self.assertEqual(suggest_context["initial_board"], initial)

    def test_socket_reset_does_not_offer_blind_retry(self):
        initial = starting_board()
        move = Move((0, 3), (0, 4), 0, "a3a4")
        robot = FakeRobot(error=ConnectionResetError("socket reset"))
        self.service, _robot, vision, _engine = make_service([initial], [move], robot=robot)
        self.service.submit_start("ai")
        state = wait_for(self.service, Phase.ERROR.value)
        self.assertFalse(state["controls"]["can_retry"])
        self.assertFalse(state["controls"]["can_start"])
        self.assertFalse(state["controls"]["can_resume"])
        self.assertTrue(state["robot"]["motion_may_continue"])
        self.assertEqual(state["error"]["code"], "motion_state_unknown")

    def test_gripper_cleanup_failure_locks_all_automatic_motion(self):
        initial = starting_board()
        move = Move((0, 3), (0, 4), 0, "a3a4")
        robot = FakeRobot(error=GripperStateUnknown("vacuum state unknown"))
        self.service, *_ = make_service([initial], [move], robot=robot)

        self.service.submit_start("ai")
        state = wait_for(self.service, Phase.ERROR.value)

        self.assertEqual(state["error"]["code"], "gripper_state_unknown")
        self.assertTrue(state["robot"]["motion_may_continue"])
        self.assertFalse(state["controls"]["can_start"])
        self.assertFalse(state["controls"]["can_resume"])

    def test_stop_warns_motion_may_continue(self):
        release = threading.Event()
        robot = FakeRobot(block_move=release)
        initial = starting_board()
        move = Move((0, 3), (0, 4), 0, "a3a4")
        self.service, _robot, vision, _engine = make_service([initial], [move], robot=robot)
        self.service.submit_start("ai")
        self.assertTrue(robot.move_entered.wait(timeout=1))
        state = self.service.submit_stop()
        self.assertEqual(state["phase"], Phase.STOPPED.value)
        self.assertTrue(state["robot"]["motion_may_continue"])
        release.set()
        state = wait_for(self.service, Phase.STOPPED.value)
        self.assertEqual(vision.calls, 1)  # Initial capture only; no post-stop verification capture.
        self.assertTrue(state["robot"]["motion_may_continue"])
        self.assertFalse(state["controls"]["can_start"])

    def test_stop_during_capture_keeps_motion_lock(self):
        release = threading.Event()
        self.service, _robot, vision, _engine = make_service(
            [starting_board()], blocker=release
        )
        self.service.submit_start("ai")
        deadline = time.monotonic() + 1
        while vision.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)

        state = self.service.submit_stop()
        self.assertTrue(state["robot"]["motion_may_continue"])
        release.set()
        state = wait_for(self.service, Phase.STOPPED.value)
        self.assertTrue(state["robot"]["motion_may_continue"])

    def test_shutdown_during_robot_move_persists_motion_lock_before_waiting(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        release = threading.Event()
        robot = FakeRobot(block_move=release)
        initial = starting_board()
        move = Move((0, 3), (0, 4), 0, "a3a4")
        self.service, *_ = make_service(
            [initial], [move], robot=robot, session_store=sessions
        )
        self.service.submit_start("ai")
        self.assertTrue(robot.move_entered.wait(timeout=1))

        closer = threading.Thread(target=self.service.close)
        closer.start()
        deadline = time.monotonic() + 1
        snapshot = None
        while time.monotonic() < deadline:
            snapshot = sessions.load_latest()
            if snapshot and snapshot.get("motion_state_uncertain"):
                break
            time.sleep(0.01)

        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot["motion_state_uncertain"])
        release.set()
        closer.join(timeout=2)
        self.assertFalse(closer.is_alive())

    def test_temporal_repair_rejects_two_wrong_labels(self):
        expected = starting_board()
        observed = [row[:] for row in expected]
        observed[3][0] = 12
        observed[3][2] = 12
        confidence = self.confidence_grid(0.10)
        self.service, *_ = make_service([])

        self.assertIsNone(
            self.service._match_expected_board(expected, observed, confidence)
        )

    def test_player_button_only_enabled_on_player_turn(self):
        self.service, *_ = make_service([])
        self.assertFalse(self.service.get_state()["controls"]["can_player_done"])
        self.service.submit_start("player")
        self.assertTrue(wait_for(self.service, Phase.PLAYER_TURN.value)["controls"]["can_player_done"])

    def test_resume_reuses_game_folder_and_rechecks_board(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        first, *_ = make_service([], session_store=sessions)
        first.submit_start("player")
        original = wait_for(first, Phase.PLAYER_TURN.value)
        game_id = original["game"]["id"]
        first.close()

        self.service, _robot, vision, _engine = make_service(
            [starting_board()], session_store=sessions
        )
        available = self.service.get_state()
        self.assertTrue(available["resume"]["available"])
        self.assertEqual(available["resume"]["game_id"], game_id)

        self.service.submit_resume("player")
        resumed = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(resumed["game"]["id"], game_id)
        self.assertEqual(vision.selected_game, game_id)
        self.assertEqual(len([path for path in Path(temporary.name).iterdir() if path.is_dir()]), 1)

    def test_resume_board_mismatch_reports_difference_and_current_board_action(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        first, *_ = make_service([], session_store=sessions)
        first.submit_start("player")
        wait_for(first, Phase.PLAYER_TURN.value)
        first.close()
        observed = apply_move(
            starting_board(), Move((0, 3), (0, 4), 0, "a3a4")
        )
        self.service, robot, _vision, _engine = make_service(
            [observed], session_store=sessions
        )

        self.service.submit_resume("player")
        state = wait_for(self.service, Phase.ERROR.value)

        self.assertIn("有 2 格差異", state["error"]["message"])
        self.assertIn("從目前棋盤開始", state["error"]["message"])
        self.assertEqual(robot.moves, [])

    def test_non_motion_error_can_resume_in_same_process(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        self.service, _robot, vision, _engine = make_service(
            [RuntimeError("YOLO failed")], session_store=sessions
        )
        self.service.submit_start("player")
        started = wait_for(self.service, Phase.PLAYER_TURN.value)
        game_id = started["game"]["id"]

        self.service.submit_player_done()
        failed = wait_for(self.service, Phase.ERROR.value)
        self.assertTrue(failed["resume"]["available"])
        self.assertTrue(failed["controls"]["can_resume"])
        self.assertTrue(failed["controls"]["can_start"])
        self.assertTrue(failed["controls"]["can_start_current"])

        vision.outcomes.append(starting_board())
        self.service.submit_resume("player")
        resumed = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(resumed["game"]["id"], game_id)
        self.assertEqual(len([path for path in Path(temporary.name).iterdir() if path.is_dir()]), 1)

    def test_restart_preserves_motion_unknown_lock_until_operator_confirms_stop(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        game_id = "20261012T010203Z-12345678"
        sessions.create(game_id)
        sessions.save(game_id, {
            "schema_version": 3,
            "game_id": game_id,
            "saved_at": "2026-10-12T01:02:03+00:00",
            "phase": Phase.ERROR.value,
            "current_turn": "ai",
            "first_actor": "player",
            "player_side": "w",
            "ai_side": "b",
            "board": starting_board(),
            "initial_board": starting_board(),
            "initial_side_to_move": "b",
            "side_to_move": "b",
            "move_history": [],
            "pending_move": None,
            "pending_expected": None,
            "pending_before": None,
            "last_move": None,
            "motion_state_uncertain": True,
        })
        self.service, *_ = make_service([], session_store=sessions)

        locked = self.service.get_state()
        self.assertTrue(locked["resume"]["available"])
        self.assertTrue(locked["robot"]["motion_may_continue"])
        self.assertFalse(locked["controls"]["can_resume"])
        self.assertFalse(locked["controls"]["can_start"])

        self.service.refresh_preflight(dynamic=True)
        still_locked = self.service.get_state()
        self.assertTrue(still_locked["robot"]["motion_may_continue"])
        self.assertTrue(still_locked["controls"]["can_confirm_safe_stop"])

        unlocked = self.service.submit_confirm_safe_stop(True)
        self.assertFalse(unlocked["robot"]["motion_may_continue"])
        self.assertTrue(unlocked["controls"]["can_resume"])
        persisted = sessions.load_latest()
        self.assertEqual(persisted["board"], starting_board())
        self.assertFalse(persisted["motion_state_uncertain"])

    def test_legacy_snapshot_without_replay_history_requires_new_board_baseline(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        game_id = "20261012T010203Z-12345678"
        sessions.create(game_id)
        sessions.save(game_id, {
            "schema_version": 2,
            "game_id": game_id,
            "saved_at": "2026-10-12T01:02:03+00:00",
            "phase": Phase.PLAYER_TURN.value,
            "board": starting_board(),
            "motion_state_uncertain": False,
        })

        self.service, *_ = make_service([], session_store=sessions)
        state = self.service.get_state()

        self.assertFalse(state["resume"]["available"])
        self.assertFalse(state["controls"]["can_resume"])
        self.assertTrue(state["controls"]["can_start_current"])
        self.assertIn("完整著法歷史", state["message"])

    def test_snapshot_with_history_that_does_not_replay_to_board_is_not_resumable(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        game_id = "20261012T010203Z-12345678"
        initial = starting_board()
        changed = apply_move(initial, Move((0, 3), (0, 4), 0, "a3a4"))
        sessions.create(game_id)
        sessions.save(game_id, {
            "schema_version": 3,
            "game_id": game_id,
            "saved_at": "2026-10-12T01:02:03+00:00",
            "phase": Phase.PLAYER_TURN.value,
            "board": changed,
            "initial_board": initial,
            "initial_side_to_move": "w",
            "side_to_move": "b",
            "move_history": [],
            "motion_state_uncertain": False,
        })

        self.service, *_ = make_service([], session_store=sessions)
        state = self.service.get_state()

        self.assertFalse(state["resume"]["available"])
        self.assertFalse(state["controls"]["can_resume"])

    def test_new_game_creates_new_folder_after_resumed_game_is_stopped(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        first, *_ = make_service([], session_store=sessions)
        first.submit_start("player")
        wait_for(first, Phase.PLAYER_TURN.value)
        first.close()

        self.service, *_ = make_service([starting_board()], session_store=sessions)
        self.service.submit_resume("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.service.submit_stop()
        wait_for(self.service, Phase.STOPPED.value)
        self.service.submit_start("player")
        wait_for(self.service, Phase.PLAYER_TURN.value)

        directories = [path for path in Path(temporary.name).iterdir() if path.is_dir()]
        self.assertEqual(len(directories), 2)

    def test_resume_does_not_replay_interrupted_pending_robot_move(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        game_id = "20261012T010203Z-12345678"
        sessions.create(game_id)
        before = starting_board()
        move = Move((0, 3), (0, 4), 0, "a3a4")
        expected = apply_move(before, move)
        sessions.save(game_id, {
            "schema_version": 3,
            "game_id": game_id,
            "saved_at": "2026-10-12T01:02:03+00:00",
            "phase": Phase.ROBOT_MOVING.value,
            "current_turn": "ai",
            "first_actor": "ai",
            "player_side": "b",
            "ai_side": "w",
            "board": before,
            "initial_board": before,
            "initial_side_to_move": "w",
            "side_to_move": "w",
            "move_history": [],
            "pending_move": {
                "source": [0, 3], "destination": [0, 4],
                "captured_piece": 0, "uci": "a3a4",
            },
            "pending_expected": expected,
            "pending_before": before,
            "last_move": None,
            "motion_state_uncertain": True,
        })
        self.service, robot, _vision, engine = make_service(
            [before], [move], session_store=sessions
        )

        locked = self.service.get_state()
        self.assertTrue(locked["robot"]["motion_may_continue"])
        self.assertFalse(locked["controls"]["can_resume"])
        self.service.submit_confirm_safe_stop(True)
        self.service.submit_resume("ai")
        state = wait_for(self.service, Phase.ERROR.value)

        self.assertTrue(state["controls"]["can_retry"])
        self.assertIn("尚未完成", state["error"]["message"])
        self.assertEqual(robot.moves, [])
        self.assertEqual(engine.suggest_calls, 0)

        _vision.outcomes.append(before)
        self.service.submit_retry()
        wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertEqual(robot.moves, [move])
        self.assertEqual(_vision.calls, 2)
        self.assertEqual(engine.suggest_calls, 0)

    def test_resume_ai_turn_accepts_one_legal_player_move_then_runs_ai(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        first, *_ = make_service([], session_store=sessions)
        first.submit_start("player")
        wait_for(first, Phase.PLAYER_TURN.value)
        first.close()

        initial = starting_board()
        player_move = Move((0, 3), (0, 4), 0, "a3a4")
        after_player = apply_move(initial, player_move)
        ai_move = Move((0, 6), (0, 5), 0, "a6a5")
        expected = apply_move(after_player, ai_move)
        self.service, robot, _vision, engine = make_service(
            [after_player, expected], [ai_move], session_store=sessions
        )

        self.service.submit_resume("ai")
        state = wait_for(self.service, Phase.PLAYER_TURN.value)

        self.assertEqual(state["last_move"]["uci"], "a6a5")
        self.assertEqual(engine.legal_calls, 2)  # Recovered player move plus AI bestmove guard.
        self.assertEqual(engine.suggest_calls, 1)
        self.assertEqual(robot.moves, [ai_move])

    def test_corrupt_session_locks_motion_until_operator_confirmation(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        sessions = GameSessionStore(Path(temporary.name))
        game_id = "20261012T010203Z-12345678"
        sessions.create(game_id)
        (sessions.game_directory(game_id) / "game.json").write_text("{broken", encoding="utf-8")

        self.service, *_ = make_service([], session_store=sessions)
        state = self.service.get_state()

        self.assertEqual(state["error"]["code"], "session_corrupt")
        self.assertTrue(state["robot"]["motion_may_continue"])
        self.assertTrue(state["controls"]["can_confirm_safe_stop"])
        self.assertFalse(state["controls"]["can_start"])


if __name__ == "__main__":
    unittest.main()
