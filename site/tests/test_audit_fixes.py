import threading
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from tmchess.calibration import BoardMatrixInspector, InspectionProfile, build_inspection_points
from tmchess.config import CONFIG, SITE_ROOT
from tmchess.robot import TMRobotAdapter, StopRequested
from tmchess.service import CommandConflict, StatePersistenceError
from tmchess.records import GameRecordWriter
from tmchess.session import GameSessionStore
from tmchess.web import create_ui_app
from tests.fakes import make_service, wait_for

class AuditFixTests(unittest.TestCase):
    def test_finish_api_saves_disk_and_rejects_unconfirmed_or_cross_origin(self):
        service, robot, *_ = make_service([])
        try:
            with tempfile.TemporaryDirectory(dir=SITE_ROOT) as directory:
                service.session_store = GameSessionStore(Path(directory))
                service.submit_start('player'); wait_for(service,'PLAYER_TURN')
                client=create_ui_app(service).test_client()
                data={'reason':'agreed_draw','confirmed':True}
                self.assertEqual(client.post('/api/game/finish',json=data).status_code,403)
                headers={'Origin':'http://localhost:8000'}
                self.assertEqual(client.post('/api/game/finish',json={'reason':'agreed_draw'},headers=headers).status_code,409)
                response=client.post('/api/game/finish',json=data,headers=headers)
                self.assertEqual(response.status_code,200)
                payload=json.loads((Path(directory)/service._game_id/'game.json').read_text(encoding='utf-8'))
                self.assertEqual(payload['phase'],'FINISHED')
                self.assertEqual(payload['game_result']['reason'],'agreed_draw')
                self.assertIsNone(service.session_store.load_latest())
                self.assertEqual(robot.moves,[])
                service.close()
        finally: service.close()

    def test_calibration_stop_at_each_command_prevents_next_command(self):
        for stop_at in range(1, 9):
            with self.subTest(stop_at=stop_at):
                stop = threading.Event()
                robot = TMRobotAdapter(CONFIG.network, CONFIG.motion)
                sent = []
                def send(sock, pose, **kwargs):
                    sent.append(pose)
                    if len(sent) == stop_at:
                        stop.set()
                with patch.object(robot, '_connect', return_value=MagicMock()), patch.object(robot, '_set_gripper'), patch.object(robot, '_read_pose', return_value=(100,100,400,1,2,3)), patch.object(robot, '_send_line', side_effect=send), patch.object(robot, '_wait_until_pose'):
                    inspector = BoardMatrixInspector(robot,CONFIG.motion,InspectionProfile(dwell_seconds=0),stop_event=stop)
                    try:
                        inspector.run(build_inspection_points(CONFIG.motion)[:1], lambda record:None)
                    except (StopRequested, InterruptedError):
                        pass
                self.assertEqual(len(sent),stop_at)

    def test_finish_persists_result_and_never_moves(self):
        for reason in ('resign','agreed_draw'):
            service, robot, *_ = make_service([])
            try:
                service.submit_start('player')
                wait_for(service,'PLAYER_TURN')
                state=service.submit_finish(reason,True)
                self.assertEqual(state['phase'],'FINISHED')
                self.assertEqual(service._last_snapshot['game_result']['reason'],reason)
                self.assertIsNone(service._last_snapshot['pending_move'])
                self.assertFalse(state['controls']['can_finish'])
                self.assertEqual(robot.moves,[])
                workbook=GameRecordWriter._build_workbook(service._last_snapshot)
                rows=list(workbook['對局摘要'].values)
                self.assertTrue(any(row[0]=='人工結束原因' and row[1] != '—' for row in rows))
                workbook.close()
                with self.assertRaises(CommandConflict): service.submit_finish(reason,True)
            finally: service.close()

    def test_finish_guards_and_save_failure(self):
        service, robot, *_ = make_service([])
        try:
            service.submit_start('player'); wait_for(service,'PLAYER_TURN')
            for reason, confirmed in [('bad', True), ('resign',False),('resign','true')]:
                with self.assertRaises(CommandConflict): service.submit_finish(reason,confirmed)
            with patch.object(service,'_persist_session',side_effect=StatePersistenceError('disk')):
                with self.assertRaises(StatePersistenceError): service.submit_finish('resign',True)
            self.assertEqual(service.get_state()['phase'],'PLAYER_TURN')
            service.store.update(lambda s:s['robot'].update(motion_may_continue=True))
            with self.assertRaises(CommandConflict): service.submit_finish('agreed_draw',True)
            self.assertEqual(robot.moves,[])
        finally: service.close()

    def test_history_missing_baseline_is_not_complete(self):
        service,*_=make_service([])
        try:
            service.submit_start('player'); wait_for(service,'PLAYER_TURN')
            self.assertTrue(service.get_state()['game']['history_complete'])
            service._initial_board=None
            self.assertFalse(service.get_state()['game']['history_complete'])
        finally: service.close()
