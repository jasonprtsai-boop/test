"""Exercise history repair at the service/vision boundary, without hardware."""
import unittest
from pathlib import Path

from tests.fakes import make_service, starting_board, wait_for
from tmchess.board import Move, apply_move, clone_board
from tmchess.vision import BoardObservationError, CaptureResult


class TemporalRecoveryTests(unittest.TestCase):
    def run_analysis(self, observed, *, legal=True, validation_error=False):
        outcome = observed
        if validation_error:
            result = CaptureResult('test-capture', observed, Path('v1.jpg'), Path('v2.jpg'))
            outcome = BoardObservationError('raw board invalid', result)
        service, robot, _, engine = make_service([outcome], legal=legal)
        self.addCleanup(service.close)
        service.submit_start_mode('analysis')
        wait_for(service, 'PLAYER_TURN')
        service.submit_player_done()
        return service, robot, engine

    def test_unique_move_repairs_missing_king_extra_piece_and_wrong_color(self):
        expected = apply_move(starting_board(), Move((0, 3), (0, 4), 0, 'a3a4'))
        noisy = clone_board(expected)
        noisy[9][4] = 0  # Missing black general: raw validation must fail.
        noisy[4][8] = 6  # Extra general outside palace.
        noisy[0][1] = 5  # Red horse misclassified as black horse.
        service, robot, engine = self.run_analysis(noisy, validation_error=True)
        state = wait_for(service, 'PLAYER_TURN')
        self.assertEqual(service._board, expected)
        self.assertEqual(state['side_to_move'], 'b')
        self.assertEqual(set(state['last_move']['vision_repaired_cells']), {'e9', 'i4', 'b0'})
        record = service._capture_history[-1]
        self.assertEqual(record['observed_board'], noisy)
        self.assertEqual(len(record['history_repairs']), 3)
        self.assertEqual(robot.moves, [])
        self.assertEqual(engine.legal_calls, 1)

    def test_ambiguous_same_piece_origins_are_rejected(self):
        noisy = apply_move(starting_board(), Move((0, 3), (0, 4), 0, 'a3a4'))
        noisy[3][2] = 0  # Either soldier could have been moved to a4.
        service, robot, _ = self.run_analysis(noisy)
        wait_for(service, 'ERROR')
        self.assertEqual(service._board, starting_board())
        self.assertEqual(service._move_history, [])
        self.assertEqual(robot.moves, [])

    def test_illegal_unique_move_never_commits(self):
        noisy = apply_move(starting_board(), Move((0, 3), (0, 5), 0, 'a3a5'))
        noisy[0][1] = 5
        service, robot, engine = self.run_analysis(noisy, legal=False)
        wait_for(service, 'ERROR')
        self.assertEqual(service._board, starting_board())
        self.assertEqual(service._move_history, [])
        self.assertEqual(engine.legal_calls, 1)
        self.assertEqual(robot.moves, [])

    def test_unique_capture_preserves_captured_piece(self):
        before = starting_board()
        before[4][0] = before[6][0]; before[6][0] = 0
        move = Move((0, 3), (0, 4), 7, 'a3a4')
        expected = apply_move(before, move)
        noisy = clone_board(expected); noisy[0][1] = 5
        service, *_ = make_service([])
        self.addCleanup(service.close)
        repaired = service._reconcile_player_observation(before, noisy, None, 'w')
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired[0].captured_piece, 7)
        self.assertEqual(repaired[1], expected)

    def test_ai_verification_repairs_noise_without_repeating_motion(self):
        initial = starting_board()
        move = Move((0, 3), (0, 4), 0, 'a3a4')
        expected = apply_move(initial, move)
        noisy = clone_board(expected); noisy[0][1] = 5; noisy[9][4] = 0
        outcome = BoardObservationError('missing general', CaptureResult('capture', noisy, Path('v1'), Path('v2')))
        service, robot, *_ = make_service([initial, outcome], [move])
        self.addCleanup(service.close)
        service.submit_start('ai')
        state = wait_for(service, 'PLAYER_TURN')
        self.assertEqual(service._board, expected)
        self.assertEqual(len(robot.moves), 1)
        self.assertEqual(state['last_move']['uci'], 'a3a4')

    def test_new_board_without_baseline_still_rejects_invalid_observation(self):
        invalid = starting_board(); invalid[9][4] = 0
        outcome = BoardObservationError('missing general', CaptureResult('capture', invalid, Path('v1'), Path('v2')))
        service, robot, *_ = make_service([outcome])
        self.addCleanup(service.close)
        service.submit_start_current('red', 'player')
        wait_for(service, 'ERROR')
        self.assertIsNone(service._board)
        self.assertEqual(robot.moves, [])
