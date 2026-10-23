import re
import queue
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tmchess.board import apply_move, standard_starting_board
from tmchess.config import CONFIG, SITE_ROOT
from tmchess.engine import PikafishEngine, parse_analysis_line
from tmchess.web import build_runtime
from tests.fakes import starting_board


class PikafishIntegrationTests(unittest.TestCase):
    def test_invalid_pawn_position_reports_engine_reason(self):
        board = standard_starting_board()
        board[3][0], board[4][1] = 0, 14
        engine = PikafishEngine(CONFIG.engine)
        self.addCleanup(engine.stop)
        with self.assertRaisesRegex(RuntimeError, r"兵／卒位置不合法.*WHITE pawn"):
            engine.suggest(board, "b")
        # A rejected position must not poison the next valid search.
        self.assertIsNotNone(engine.suggest(standard_starting_board(), "b"))

    def test_wait_for_reports_critical_error(self):
        engine = PikafishEngine(CONFIG.engine)
        engine._lines.put("info string CRITICAL ERROR: invalid configuration")
        with self.assertRaisesRegex(RuntimeError, "invalid configuration"):
            engine._wait_for("readyok")

    def test_parses_multipv_wdl_and_mate_analysis_lines(self):
        line = parse_analysis_line(
            "info depth 12 seldepth 18 multipv 2 score cp -23 lowerbound "
            "wdl 7 927 66 nodes 30146 nps 814756 time 37 pv h9g7 h0g2"
        )

        self.assertIsNotNone(line)
        assert line is not None
        self.assertEqual(line.depth, 12)
        self.assertEqual(line.multipv, 2)
        self.assertEqual((line.score_type, line.score, line.bound), ("cp", -23, "lowerbound"))
        self.assertEqual(line.wdl, (7, 927, 66))
        self.assertEqual(line.pv, ("h9g7", "h0g2"))

        mate = parse_analysis_line(
            "info depth 5 seldepth 5 multipv 1 score mate 1 nodes 34 time 3 pv e7d7"
        )
        self.assertIsNotNone(mate)
        assert mate is not None
        self.assertEqual((mate.score_type, mate.score), ("mate", 1))
        self.assertIsNone(mate.wdl)

    def test_ignores_incomplete_or_invalid_analysis_lines(self):
        self.assertIsNone(parse_analysis_line("info depth 4 currmove h2e2 currmovenumber 1"))
        self.assertIsNone(parse_analysis_line("info depth 4 score cp nope pv h2e2"))
        self.assertIsNone(parse_analysis_line("bestmove h2e2"))

    def test_bundled_engine_accepts_board_and_returns_legal_move(self):
        engine = PikafishEngine(CONFIG.engine)
        self.addCleanup(engine.stop)
        board = starting_board()

        move = engine.suggest(board, "w")

        self.assertIsNotNone(move)
        assert move is not None
        self.assertRegex(move.uci, re.compile(r"^[a-i][0-9][a-i][0-9]$"))
        self.assertTrue(engine.is_legal(board, "w", move))
        self.assertTrue(engine.has_legal_move(board, "w"))
        self.assertNotEqual(apply_move(board, move), board)

    def test_bundled_engine_returns_three_ranked_analysis_lines(self):
        engine = PikafishEngine(CONFIG.engine)
        self.addCleanup(engine.stop)

        analysis = engine.analyze(starting_board(), "w", multipv=3)

        self.assertIsNotNone(analysis.bestmove)
        self.assertEqual([line.multipv for line in analysis.lines], [1, 2, 3])
        self.assertTrue(all(line.depth >= 1 for line in analysis.lines))
        self.assertTrue(all(line.wdl is not None for line in analysis.lines))
        self.assertEqual(analysis.lines[0].pv[0], analysis.bestmove.uci)

    def test_position_command_replays_complete_history(self):
        initial = starting_board()
        after_red = apply_move(initial, PikafishEngine._move_from_uci(initial, "w", "a3a4"))
        current = apply_move(after_red, PikafishEngine._move_from_uci(after_red, "b", "a6a5"))

        command = PikafishEngine._position_command(
            current,
            "w",
            initial_board=initial,
            initial_side="w",
            moves=("a3a4", "a6a5"),
        )

        self.assertIn(" moves a3a4 a6a5", command)

    def test_position_command_rejects_history_that_does_not_match_board(self):
        with self.assertRaisesRegex(ValueError, "無法重建"):
            PikafishEngine._position_command(
                starting_board(),
                "b",
                initial_board=starting_board(),
                initial_side="w",
                moves=("a3a4",),
            )

    def test_search_timeout_invalidates_engine_process(self):
        engine = PikafishEngine(replace(CONFIG.engine, timeout_seconds=0.01))
        engine.start = lambda: None
        engine._send = lambda _command: None
        engine._lines = queue.Queue()
        engine._lines.put("readyok")
        stopped = []
        engine.stop = lambda: stopped.append(True)

        with self.assertRaisesRegex(TimeoutError, "bestmove"):
            engine._search("position fen test", "go infinite")

        self.assertEqual(stopped, [True])

    def test_application_runtime_uses_real_pikafish_adapter(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = replace(
            CONFIG,
            capture_root=root / "games",
            record_root=root / "records",
        )
        service, _inbox = build_runtime(config, start_preflight_monitor=False)
        self.addCleanup(service.close)

        self.assertIsInstance(service.engine, PikafishEngine)


if __name__ == "__main__":
    unittest.main()
