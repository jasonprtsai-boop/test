import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tmchess.board import Move, apply_move, board_to_fen, standard_starting_board
from tmchess.record_audit import audit_history
from tmchess.records import GameRecordWriter
from tmchess.session import GameSessionStore
from tmchess.service import GameService
from tmchess.config import SITE_ROOT
from tests.fakes import make_service, wait_for


class RecordAuditTests(unittest.TestCase):
    def payload(self):
        before = standard_starting_board()
        after = apply_move(before, Move((0, 3), (0, 4), 0, 'a3a4'))
        return {'game_id': '20261019T000000Z-12345678', 'initial_board': before,
                'initial_side_to_move': 'w', 'board': after, 'side_to_move': 'b',
                'move_history': [{'uci': 'a3a4', 'side': 'w', 'fen_before': board_to_fen(before, 'w'),
                                  'fen_after': board_to_fen(after, 'b')}]}

    def test_reconstructs_each_committed_board_without_mutating_source(self):
        payload = self.payload()
        original = copy.deepcopy(payload)
        frames, issues = audit_history(payload)
        self.assertEqual(issues, [])
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[-1]['board'], payload['board'])
        self.assertEqual(payload, original)

    def test_malformed_records_return_issues_without_crashing(self):
        for payload in (None, [], "invalid"):
            with self.subTest(payload=payload):
                frames, issues = audit_history(payload)
                self.assertEqual(frames, [])
                self.assertTrue(issues)
        for item in (None, [], {"uci": None}, {"uci": 123}):
            payload = self.payload()
            payload["move_history"] = [item]
            frames, issues = audit_history(payload)
            self.assertEqual(len(frames), 1)
            self.assertTrue(issues)

    def test_friendly_capture_is_rejected_before_reconstruction(self):
        payload = self.payload()
        payload["move_history"] = [{"uci": "a0b0"}]
        frames, issues = audit_history(payload)
        self.assertEqual(len(frames), 1)
        self.assertTrue(any("己方" in issue for issue in issues))

    def test_mismatched_side_and_fen_are_explicit(self):
        payload = self.payload()
        payload['side_to_move'] = 'w'
        payload['move_history'][0]['fen_after'] = 'corrupt'
        _, issues = audit_history(payload)
        self.assertEqual(len(issues), 2)

    def test_export_preserves_literal_formula_like_logs_and_board_orientation(self):
        payload = self.payload()
        payload['message'] = '=1+1'
        workbook = GameRecordWriter._build_workbook(payload)
        self.addCleanup(workbook.close)
        cells = [cell for row in workbook['對局摘要'] for cell in row if cell.value == '=1+1']
        self.assertEqual(len(cells), 1)
        self.assertEqual(cells[0].data_type, 's')
        self.assertEqual(workbook['棋盤履歷']['A7'].value, 9)
        self.assertEqual(workbook['棋盤履歷']['A16'].value, 0)
        self.assertEqual(workbook['棋盤履歷']['F16'].value, '帥')

    def test_locked_excel_retries_without_new_game_action(self):
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / 'runtime') as root:
            writer = GameRecordWriter(Path(root))
            writer.retry_seconds = 0.02
            try:
                with patch.object(writer, '_write', side_effect=[PermissionError('locked'), None]) as write:
                    writer.schedule(self.payload())
                    self.assertTrue(writer.flush(timeout=2))
                    self.assertEqual(write.call_count, 2)
                    self.assertIsNone(writer.status_for(self.payload()['game_id'])['last_error'])
            finally:
                writer.close()

    def test_retry_keeps_newer_snapshot_instead_of_overwriting_it(self):
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / 'runtime') as root:
            writer = GameRecordWriter(Path(root))
            writer.retry_seconds = 0.01
            entered, release = threading.Event(), threading.Event()
            seen = []
            def write(payload):
                seen.append(payload['message'])
                if len(seen) == 1:
                    entered.set()
                    release.wait(2)
                    raise PermissionError('locked')
            try:
                with patch.object(writer, '_write', side_effect=write):
                    first = self.payload(); first['message'] = 'old'
                    writer.schedule(first)
                    self.assertTrue(entered.wait(2))
                    newer = self.payload(); newer['message'] = 'new'
                    writer.schedule(newer)
                    release.set()
                    self.assertTrue(writer.flush(2))
                    self.assertEqual(seen, ['old', 'new'])
            finally:
                release.set()
                writer.close()

    def test_failed_resume_selection_does_not_corrupt_saved_turn(self):
        for outcome in (standard_starting_board(), RuntimeError('camera failed')):
            with self.subTest(outcome=type(outcome).__name__), tempfile.TemporaryDirectory(dir=SITE_ROOT / 'runtime') as root:
                sessions = GameSessionStore(Path(root))
                first, *_ = make_service([], session_store=sessions)
                try:
                    first.submit_start('player')
                    wait_for(first, 'PLAYER_TURN')
                finally:
                    first.close()
                service, robot, *_ = make_service([outcome], session_store=sessions)
                try:
                    service.submit_resume('ai')
                    wait_for(service, 'ERROR')
                    snapshot = sessions.load_latest()
                    self.assertEqual(snapshot['side_to_move'], 'w')
                    self.assertTrue(GameService._has_complete_replay_context(snapshot))
                    self.assertEqual(robot.moves, [])
                finally:
                    service.close()
