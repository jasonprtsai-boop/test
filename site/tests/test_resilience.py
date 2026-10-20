"""Fault injection without hardware: the next request/command must still work."""
import unittest
from unittest.mock import Mock, patch

from tests.fakes import make_service, wait_for
from tmchess.web import create_ui_app


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.service, *_ = make_service([])
        self.addCleanup(self.service.close)

    def test_event_capacity_preserves_control_requests_and_releases_slots(self):
        self.service.submit_start('player')
        wait_for(self.service, 'PLAYER_TURN')
        app = create_ui_app(self.service)
        client = app.test_client()
        streams = []
        try:
            for _ in range(4):
                response = client.get('/api/events', buffered=False)
                self.assertEqual(response.status_code, 200)
                streams.append(response)
            rejected = client.get('/api/events')
            self.assertEqual(rejected.status_code, 503)
            self.assertEqual(client.get('/api/state').status_code, 200)
            self.assertEqual(client.post('/api/game/stop', json={}, headers={
                'Origin': 'http://localhost:8000',
            }).status_code, 202)
            streams.pop().close()
            replacement = client.get('/api/events', buffered=False)
            self.assertEqual(replacement.status_code, 200)
            replacement.close()
            replacement.close()  # Cleanup is idempotent.
        finally:
            for response in streams:
                response.close()

    def test_secondary_recovery_failure_does_not_kill_worker(self):
        def fail_after_board_created(_mode):
            from tests.fakes import starting_board
            self.service._board = starting_board()
            raise RuntimeError('original')

        with patch.object(self.service, '_start', side_effect=fail_after_board_created), \
             patch.object(self.service, '_load_resume_candidate', side_effect=OSError('disk lost')):
            # _handle_error attempts reload only when a committed board exists.
            self.service.submit_start('player')
            wait_for(self.service, 'ERROR')
            self.service._queue.join()
        self.assertTrue(self.service._worker.is_alive())
        self.assertFalse(self.service._busy)
        self.assertEqual(self.service.get_state()['error']['code'], 'recovery_failed')
        self.service.submit_start('player')
        wait_for(self.service, 'PLAYER_TURN')

    def test_excel_schedule_and_status_failures_do_not_block_game(self):
        writer = Mock()
        writer.schedule.side_effect = OSError('Excel unavailable')
        writer.path_for.side_effect = OSError('record directory unavailable')
        self.service.record_writer = writer
        self.service.submit_start('player')
        state = wait_for(self.service, 'PLAYER_TURN')
        self.assertTrue(state['controls']['can_player_done'])
        self.assertIn('Excel unavailable', state['game']['record_status']['error'])
        self.assertTrue(self.service._worker.is_alive())

    def test_preflight_exception_is_visible_and_next_iteration_recovers(self):
        with patch.object(self.service, 'refresh_preflight', side_effect=OSError('probe unavailable')), \
             patch.object(self.service._close_event, 'wait', return_value=True):
            self.service._preflight_loop()
        state = self.service.get_state()
        self.assertFalse(state['preflight']['ready'])
        self.assertEqual(state['preflight']['checks'][0]['detail'], 'probe unavailable')
        self.service._preflight_loop()
        self.assertTrue(self.service.get_state()['preflight']['ready'])


if __name__ == '__main__':
    unittest.main()
