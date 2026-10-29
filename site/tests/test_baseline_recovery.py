import copy
import tempfile
import unittest
from pathlib import Path

from tmchess.board import Move, validate_pawn_positions
from tmchess.config import SITE_ROOT
from tmchess.session import GameSessionStore
from tmchess.state import Phase
from tests.fakes import make_service, starting_board, wait_for


class BaselineRecoveryTests(unittest.TestCase):
    def tearDown(self):
        if hasattr(self, 'service'):
            self.service.close()

    def test_invalid_baseline_pawn_never_reaches_engine_or_becomes_board(self):
        invalid = starting_board()
        invalid[2][4], invalid[3][4] = 14, 0
        self.service, robot, vision, engine = make_service([invalid])
        self.service.submit_start_current('red', 'ai', continuation='new', mode='human_red')
        state = wait_for(self.service, Phase.ERROR.value)
        self.assertIn('e2', state['message'])
        self.assertIsNone(self.service._board)
        self.assertEqual(engine.suggest_calls, 0)
        self.assertEqual(robot.moves, [])

    def test_pawn_river_boundary_for_both_colors(self):
        for piece, row, col, valid in [(14, 4, 0, True), (14, 4, 1, False),
                                       (14, 5, 1, True), (14, 2, 0, False),
                                       (7, 5, 0, True), (7, 5, 1, False),
                                       (7, 4, 1, True), (7, 7, 0, False)]:
            with self.subTest(piece=piece, row=row, col=col):
                board = starting_board()
                board[3 if piece == 14 else 6][0] = 0
                board[row][col] = piece
                if valid:
                    validate_pawn_positions(board)
                else:
                    with self.assertRaisesRegex(ValueError, '不可到達'):
                        validate_pawn_positions(board)

    def test_standard_dual_engine_restart_and_legacy_alias_migration(self):
        with tempfile.TemporaryDirectory(dir=SITE_ROOT/'runtime') as directory:
            sessions = GameSessionStore(Path(directory))
            move = Move((0, 3), (0, 4), 0, 'a3a4')
            self.service, *_ = make_service([starting_board()], [move], session_store=sessions)
            self.service.submit_start_mode('engine_vs_engine')
            wait_for(self.service, Phase.ENGINE_READY.value)
            self.assertEqual((self.service._player_side, self.service._ai_side), ('w', 'b'))
            self.service.submit_stop()
            wait_for(self.service, Phase.STOPPED.value)
            snapshot = copy.deepcopy(self.service._last_snapshot)
            self.service.close()
            # Emulate persisted older dual-engine aliases without rewriting the history.
            snapshot['ai_side'] = 'w'
            sessions.save(snapshot['game_id'], snapshot)
            self.service, robot, *_ = make_service([starting_board()], [move], session_store=sessions)
            self.service.submit_resume('player')
            state = wait_for(self.service, Phase.ERROR.value)
            self.assertTrue(state['controls']['can_retry'])
            self.assertEqual(state['game_mode'], 'engine_vs_engine')
            self.assertEqual((self.service._player_side, self.service._ai_side), ('w', 'b'))
            self.assertEqual(robot.moves, [])
            self.service.close()


if __name__ == '__main__':
    unittest.main()
