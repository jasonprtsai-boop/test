"""Regression evidence for stop races, recovery validation and state ownership."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.fakes import make_service, starting_board, wait_for
from tmchess.board import Move, apply_move
from tmchess.config import SITE_ROOT
from tmchess.service import CommandConflict, StatePersistenceError
from tmchess.session import GameSessionStore, SessionCorruptionError
from tmchess.state import StateStore
from tmchess.robot import StopRequested


class DeepAuditTests(unittest.TestCase):
    def idle(self, service):
        deadline = time.monotonic() + 3
        while service._busy and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(service._busy, 'worker did not finish')

    def blocked(self, service, method, result):
        entered, release = threading.Event(), threading.Event()
        def query(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test release timed out')
            return result
        guard = patch.object(service.engine, method, side_effect=query)
        guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(release.set)
        return entered, release

    def test_stop_during_player_legality_does_not_commit_or_restart_turn(self):
        moved = apply_move(starting_board(), Move((0, 3), (0, 4), 0, 'a3a4'))
        service, robot, *_ = make_service([moved])
        self.addCleanup(service.close)
        service.submit_start_mode('analysis')
        wait_for(service, 'PLAYER_TURN')
        entered, release = self.blocked(service, 'is_legal', True)
        service.submit_player_done()
        self.assertTrue(entered.wait(2))
        service.submit_stop()
        release.set()
        self.idle(service)
        self.assertEqual(service.get_state()['phase'], 'STOPPED')
        self.assertEqual(service._board, starting_board())
        self.assertEqual(service._move_history, [])
        self.assertEqual(robot.moves, [])

    def test_stop_during_engine_search_does_not_publish_pending_or_result(self):
        for result in (None, Move((0, 3), (0, 4), 0, 'a3a4')):
            with self.subTest(result=result):
                service, robot, *_ = make_service([starting_board()])
                try:
                    entered, release = self.blocked(service, 'suggest', result)
                    service.submit_start_mode('engine_vs_engine')
                    self.assertTrue(entered.wait(2))
                    service.submit_stop()
                    release.set()
                    self.idle(service)
                    self.assertEqual(service.get_state()['phase'], 'STOPPED')
                    self.assertIsNone(service._pending_move)
                    self.assertEqual(robot.moves, [])
                finally:
                    release.set()
                    service.close()

    def test_failed_stop_save_does_not_release_active_worker(self):
        service, *_ = make_service([])
        self.addCleanup(service.close)
        service.submit_start('player')
        wait_for(service, 'PLAYER_TURN')
        entered, release = self.blocked(service, 'analyze', None)
        service.submit_hint()
        self.assertTrue(entered.wait(2))
        try:
            with patch.object(service, '_persist_session', side_effect=StatePersistenceError('disk failed')):
                with self.assertRaises(StatePersistenceError):
                    service.submit_stop()
            self.assertTrue(service._busy)
            self.assertFalse(service.get_state()['controls']['can_start'])
            with self.assertRaises(CommandConflict):
                service.submit_start('player')
        finally:
            release.set()
            self.idle(service)
        self.assertEqual(service.get_state()['phase'], 'STOPPED')
        self.assertEqual(service.get_state()['analysis']['status'], 'idle')

    def test_closed_service_rejects_new_commands(self):
        service, *_ = make_service([])
        service.close()
        with self.assertRaises(CommandConflict):
            service.submit_start('player')
        self.assertFalse(any(service.get_state()['controls'].values()))
        self.assertEqual(service._queue.unfinished_tasks, 0)
        with self.assertRaises(CommandConflict):
            service.submit_confirm_base_tcp('RobotBase', 'NOTOOL')

    def test_failed_state_mutation_does_not_leak_partial_data(self):
        store = StateStore()
        before = store.snapshot()
        def broken(state):
            state['phase'] = 'ROBOT_MOVING'
            state['robot']['motion_may_continue'] = True
            raise ValueError('invalid state')
        with self.assertRaises(ValueError):
            store.update(broken)
        self.assertEqual(store.snapshot(), before)

    def test_bad_phase_type_is_handled_as_corrupt_snapshot(self):
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / 'runtime') as root:
            store = GameSessionStore(Path(root))
            game_id = '20261020T010203Z-12345678'
            directory = store.create(game_id)
            (directory / 'game.json').write_text(json.dumps({
                'game_id': game_id, 'schema_version': 3, 'phase': [],
                'board': None, 'motion_state_uncertain': True,
            }), encoding='utf-8')
            with self.assertRaises(SessionCorruptionError):
                store.load_latest()

    def test_same_second_game_order_uses_save_time_instead_of_random_uuid(self):
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / 'runtime') as root:
            store = GameSessionStore(Path(root))
            for index, suffix in enumerate(('ffffffff', '00000000')):
                game_id = f'20261020T010203Z-{suffix}'
                store.create(game_id)
                path = store.save(game_id, {'game_id': game_id, 'schema_version': 3,
                    'phase': 'PLAYER_TURN', 'board': starting_board(),
                    'motion_state_uncertain': False, 'marker': index})
                stamp = 1700000000000000000 + index * 1000000000
                os.utime(path, ns=(stamp, stamp))
            self.assertEqual(store.load_latest()['marker'], 1)

    def snapshot(self):
        before = starting_board()
        move = Move((0, 3), (0, 4), 0, 'a3a4')
        return {'game_id': '20261020T010203Z-12345678', 'schema_version': 3,
            'phase': 'ENGINE_READY', 'board': before, 'initial_board': before,
            'initial_side_to_move': 'w', 'side_to_move': 'w', 'move_history': [],
            'game_mode': 'engine_vs_engine', 'controllers': {'w': 'engine', 'b': 'engine'},
            'execution_mode': 'step', 'player_side': 'w', 'ai_side': 'b',
            'motion_state_uncertain': False,
            'pending_move': {'source': [0, 3], 'destination': [0, 4], 'captured_piece': 0, 'uci': 'a3a4'},
            'pending_before': before, 'pending_expected': apply_move(before, move)}

    def test_corrupt_pending_and_mode_are_rejected_before_runtime_mutation(self):
        variants = [
            ('pending_expected', starting_board()),
            ('execution_mode', 'automatic'),
            ('pending_move', {'source': [8, 3], 'destination': [0, 4], 'uci': 'a3a4'}),
            ('pending_retry_count', 'invalid'),
            ('controllers', {'w': 'engine', 'b': 'human'}),
        ]
        for key, value in variants:
            with self.subTest(key=key):
                service, robot, *_ = make_service([])
                try:
                    payload = self.snapshot(); payload[key] = value
                    with self.assertRaises(ValueError):
                        service._restore_session(payload)
                    self.assertIsNone(service._board)
                    self.assertEqual(robot.moves, [])
                finally:
                    service.close()

    def test_late_game_status_cannot_override_stop_after_verified_move(self):
        for has_legal in (True, False):
            with self.subTest(has_legal=has_legal):
                initial = starting_board()
                move = Move((0, 3), (0, 4), 0, 'a3a4')
                expected = apply_move(initial, move)
                service, robot, *_ = make_service([initial, expected], [move])
                try:
                    entered, release = self.blocked(service, 'has_legal_move', has_legal)
                    service.submit_start('ai')
                    self.assertTrue(entered.wait(2))
                    service.submit_stop()
                    release.set()
                    self.idle(service)
                    self.assertEqual(service.get_state()['phase'], 'STOPPED')
                    self.assertEqual(service._board, expected)
                    self.assertEqual(len(service._move_history), 1)
                    self.assertEqual(len(robot.moves), 1)
                finally:
                    release.set()
                    service.close()

    def test_late_current_board_game_status_cannot_override_stop(self):
        service, robot, *_ = make_service([starting_board()])
        self.addCleanup(service.close)
        entered, release = self.blocked(service, 'has_legal_move', False)
        service.submit_start_current('red', 'player')
        self.assertTrue(entered.wait(2))
        service.submit_stop()
        release.set()
        self.idle(service)
        self.assertEqual(service.get_state()['phase'], 'STOPPED')
        self.assertEqual(robot.moves, [])

    def test_stopped_commit_preserves_pending_instead_of_changing_board(self):
        service, *_ = make_service([])
        self.addCleanup(service.close)
        snapshot = self.snapshot()
        service._restore_session(snapshot)
        service._stop_event.set()
        with self.assertRaises(StopRequested):
            service._commit_pending_move('done')
        self.assertEqual(service._board, snapshot['board'])
        self.assertIsNotNone(service._pending_move)
        self.assertEqual(service._move_history, [])

    def test_reset_endpoint_cannot_detach_an_active_game(self):
        service, *_ = make_service([])
        self.addCleanup(service.close)
        service.submit_start('player')
        state = wait_for(service, 'PLAYER_TURN')
        with self.assertRaises(CommandConflict):
            service.submit_reset_page()
        self.assertEqual(service.get_state()['game']['id'], state['game']['id'])
        self.assertEqual(service._board, starting_board())

    def test_invalid_saved_pending_is_rejected_before_capture(self):
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / 'runtime') as root:
            sessions = GameSessionStore(Path(root))
            payload = self.snapshot()
            sessions.create(payload['game_id'])
            sessions.save(payload['game_id'], payload)
            service, robot, vision, _ = make_service([], legal=False, session_store=sessions)
            try:
                service.submit_resume('ai')
                wait_for(service, 'ERROR')
                self.assertEqual(vision.calls, 0)
                self.assertEqual(robot.moves, [])
                self.assertEqual(service._move_history, [])
            finally:
                service.close()

    def test_optional_record_failure_does_not_undo_durable_safe_stop_confirmation(self):
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / 'runtime') as root:
            sessions = GameSessionStore(Path(root))
            payload = self.snapshot(); payload['motion_state_uncertain'] = True
            sessions.create(payload['game_id']); sessions.save(payload['game_id'], payload)
            writer = Mock()
            writer.path_for.return_value = Path(root) / 'unavailable.xlsx'
            writer.status_for.return_value = {}
            writer.schedule.side_effect = OSError('Excel unavailable')
            service, *_ = make_service([], session_store=sessions, record_writer=writer)
            try:
                state = service.submit_confirm_safe_stop(True)
                self.assertFalse(state['robot']['motion_may_continue'])
                self.assertFalse(sessions.load_latest()['motion_state_uncertain'])
                self.assertTrue(state['controls']['can_resume'])
                self.assertIn('Excel unavailable', state['game']['record_status']['error'])
            finally:
                service.close()

    def test_runtime_identity_is_stable_across_page_reset_and_changes_on_restart(self):
        first, *_ = make_service([])
        second, *_ = make_service([])
        try:
            first_id = first.get_state()['instance_id']
            self.assertEqual(first.submit_reset_page()['instance_id'], first_id)
            self.assertNotEqual(second.get_state()['instance_id'], first_id)
        finally:
            first.close(); second.close()

    def test_stop_during_secondary_error_preserves_stopped_state(self):
        service, *_ = make_service([])
        self.addCleanup(service.close)
        entered, release = threading.Event(), threading.Event()
        def fail_start(_mode):
            service._board = starting_board()
            raise RuntimeError('original failure')
        def fail_reload():
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test release timed out')
            raise OSError('recovery disk failed')
        try:
            with patch.object(service, '_start', side_effect=fail_start), \
                 patch.object(service, '_load_resume_candidate', side_effect=fail_reload):
                service.submit_start('player')
                self.assertTrue(entered.wait(2))
                service.submit_stop()
                release.set()
                self.idle(service)
                self.assertEqual(service.get_state()['phase'], 'STOPPED')
                self.assertTrue(service._worker.is_alive())
        finally:
            release.set()
