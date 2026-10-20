import unittest

from tmchess.board import Move, apply_move, board_to_fen, find_player_move, parse_uci_move, validate_board
from tests.fakes import starting_board


class BoardTests(unittest.TestCase):
    def test_board_to_fen_has_correct_side(self):
        self.assertTrue(board_to_fen(starting_board(), "w").endswith(" w - - 0 1"))

    def test_parse_uci(self):
        self.assertEqual(parse_uci_move("a3a4"), ((0, 3), (0, 4)))

    def test_rejects_invalid_uci(self):
        with self.assertRaises(ValueError):
            parse_uci_move("a10a9")

    def test_find_player_move(self):
        before = starting_board()
        after = apply_move(before, Move((0, 3), (0, 4), 0, "a3a4"))
        self.assertEqual(find_player_move(before, after, "w").uci, "a3a4")

    def test_rejects_wrong_side(self):
        before = starting_board()
        after = apply_move(before, Move((0, 6), (0, 5), 0, "a6a5"))
        with self.assertRaisesRegex(ValueError, "非己方"):
            find_player_move(before, after, "w")

    def test_rejects_multiple_changes(self):
        before = starting_board()
        after = apply_move(before, Move((0, 3), (0, 4), 0, "a3a4"))
        after = apply_move(after, Move((2, 3), (2, 4), 0, "c3c4"))
        with self.assertRaisesRegex(ValueError, "一次只能"):
            find_player_move(before, after, "w")

    def test_rejects_missing_king(self):
        board = starting_board()
        board[0][4] = 0
        with self.assertRaisesRegex(ValueError, "紅帥"):
            validate_board(board)

    def test_midgame_allows_captured_pieces(self):
        board = starting_board()
        board[3][0] = 0
        board[6][0] = 0
        board[0][0] = 0
        validate_board(board)

    def test_extra_soldier_reports_count_and_cells(self):
        board = starting_board()
        board[4][0] = 14
        with self.assertRaisesRegex(ValueError, "兵 辨識到 6 枚，超過最多 5 枚") as caught:
            validate_board(board)
        self.assertIn("a4", str(caught.exception))

    def test_rejects_facing_kings(self):
        board = [[0 for _ in range(9)] for _ in range(10)]
        board[0][4], board[9][4] = 13, 6
        with self.assertRaisesRegex(ValueError, "照面"):
            validate_board(board)


if __name__ == "__main__":
    unittest.main()
