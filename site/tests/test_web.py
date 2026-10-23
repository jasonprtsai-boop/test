import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tmchess.config import CONFIG, SITE_ROOT
from tmchess.service import GameService
from tmchess.vision import BoardObservationError, CaptureResult, DualViewInbox
from tmchess.web import create_ui_app, create_vision_app
from tests.fakes import FakeEngine, FakePreflight, FakeRobot, FakeVision, make_service, starting_board, wait_for
from tmchess.state import Phase


class WebTests(unittest.TestCase):
    def test_post_move_review_api_and_panel_are_removed(self):
        response = self.client.post('/api/game/review', json={'action': 'confirm', 'confirmed': True})
        self.assertEqual(response.status_code, 404)
        page = self.client.get('/player').get_data(as_text=True)
        self.assertNotIn('id="reviewPanel"', page)
        self.assertNotIn('id="confirmMoveButton"', page)
        self.assertNotIn('review', self.service.get_state())

    def setUp(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        self.config = replace(CONFIG, capture_root=Path(self.temp.name))
        self.service, *_ = make_service([starting_board()])
        self.ui = create_ui_app(self.service, self.config)
        self.client = self.ui.test_client()
        self.client.environ_base["HTTP_ORIGIN"] = f"http://localhost:{self.config.network.ui_port}"

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_page_reset_clears_stopped_verification_error(self):
        self.service.store.update(lambda state: state.update(
            phase="STOPPED", first_actor="player",
            error={"code": "verification_failed", "retryable": True},
        ))
        previous_revision = self.service.get_state()["revision"]
        response = self.client.post("/api/game/reset-page", json={})
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(state["phase"], "IDLE")
        self.assertIsNone(state["error"])
        self.assertIsNone(state["first_actor"])
        self.assertTrue(state["controls"]["can_start_current"])
        self.assertGreater(state["revision"], previous_revision)
        self.assertFalse(state["resume"]["available"])

    def test_page_reset_preserves_unknown_motion_lock(self):
        self.service.store.update(lambda state: state["robot"].update(motion_may_continue=True))
        response = self.client.post("/api/game/reset-page", json={})
        self.assertEqual(response.status_code, 409)
        self.assertTrue(self.service.get_state()["robot"]["motion_may_continue"])

    def test_page_reset_rejects_active_worker(self):
        self.service._busy = True
        try:
            response = self.client.post("/api/game/reset-page", json={})
            self.assertEqual(response.status_code, 409)
        finally:
            self.service._busy = False

    def test_three_pages_exist(self):
        for path in ("/", "/player", "/monitor"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("TM5-700".encode(), response.data)

    def test_dynamic_status_and_errors_are_announced_accessibly(self):
        player = self.client.get("/player").get_data(as_text=True)
        monitor = self.client.get("/monitor").get_data(as_text=True)

        self.assertIn('id="gameMessage" class="status-message" role="status"', player)
        self.assertIn('id="errorBox" class="error-box" role="alert"', player)
        self.assertIn('id="visionStatus" class="subtle-badge" role="status"', monitor)
        self.assertIn('id="monitorError" class="error-box" role="alert"', monitor)
        self.assertIn('aria-live="polite"', player)
        self.assertIn('aria-live="polite"', monitor)

    def test_player_keeps_transport_error_visible_after_state_refresh(self):
        response = self.client.get("/static/player.js")
        try:
            script = response.get_data(as_text=True)
        finally:
            response.close()

        self.assertIn("let requestError = null", script)
        self.assertIn("state.error ?", script)
        self.assertIn(": requestError", script)

    def test_persistence_failure_returns_json_503_and_unlocks_controls(self):
        class BrokenStore:
            def create(self, _game_id):
                raise OSError("disk unavailable")

        self.service.session_store = BrokenStore()

        response = self.client.post(
            "/api/game/start-mode",
            json={"mode": "human_red"},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.mimetype, "application/json")
        payload = response.get_json()
        self.assertEqual(payload["error"]["code"], "state_persistence_failed")
        self.assertTrue(payload["state"]["controls"]["can_start"])

    def test_player_page_hides_operator_preflight_details(self):
        player = self.client.get("/player").get_data(as_text=True)
        monitor = self.client.get("/monitor").get_data(as_text=True)

        self.assertIn('id="setupSection"', player)
        self.assertIn('id="gameSection" hidden', player)
        self.assertNotIn("真機預檢", player)
        self.assertNotIn("YOLO", player)
        self.assertNotIn('id="preflightList"', player)
        self.assertNotIn('id="resumeButton"', player)
        self.assertIn('id="lastResultSection"', player)
        self.assertIn('id="lastResultMessage"', player)
        self.assertIn('id="currentBoardButton"', player)
        self.assertIn('id="hintButton"', player)
        self.assertIn('id="executeEngineButton"', player)
        self.assertIn('id="analysisPanel"', player)
        self.assertIn('name="gameMode"', player)
        self.assertIn("偵測目前棋盤", player)
        self.assertIn("從頭開始", player)
        self.assertIn('id="preflightList"', monitor)
        self.assertIn('id="preflightStatus"', monitor)
        self.assertIn('id="recordDownload"', monitor)
        self.assertNotIn('id="operatorToken"', player)
        self.assertNotIn('id="operatorToken"', monitor)
        self.assertIn('id="confirmBaseTcp"', monitor)

    def test_pages_include_keyboard_focus_and_reduced_motion_styles(self):
        response = self.client.get("/static/styles.css")
        try:
            css = response.get_data(as_text=True)
            self.assertIn(".choice-grid input:focus-visible + span", css)
            self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        finally:
            response.close()

    def test_control_api_not_on_vision_service(self):
        inbox = DualViewInbox(Path(self.temp.name), "127.0.0.1", 1024)
        vision = create_vision_app(inbox, replace(self.config, network=replace(self.config.network, robot_ip="127.0.0.1")))
        self.assertEqual(vision.test_client().post("/api/game/start").status_code, 404)
        self.assertEqual(vision.test_client().post("/api/game/start-current").status_code, 404)

    def test_vision_route_not_on_ui_service(self):
        self.assertEqual(self.client.post("/api/vision/tmvision/detect").status_code, 404)

    def test_invalid_sequence_returns_409(self):
        response = self.client.post("/api/game/player-done", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "not_player_turn")

    def test_hint_is_rejected_before_a_board_is_active(self):
        response = self.client.post("/api/game/hint", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "hint_not_available")

    def test_invalid_first_actor_returns_409(self):
        response = self.client.post("/api/game/start", json={"first_actor": "robot"})
        self.assertEqual(response.status_code, 409)

    def test_invalid_game_mode_returns_409(self):
        response = self.client.post("/api/game/start-mode", json={"mode": "continuous"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_game_mode")

    def test_engine_execute_is_rejected_before_a_move_is_ready(self):
        response = self.client.post("/api/game/execute-engine", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "engine_move_not_ready")

    def test_invalid_resume_actor_returns_409(self):
        response = self.client.post("/api/game/resume", json={"current_actor": "robot"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_current_actor")

    def test_invalid_current_board_color_returns_409(self):
        response = self.client.post(
            "/api/game/start-current",
            json={"player_color": "green", "current_actor": "player"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_player_color")

    def test_current_board_api_accepts_analysis_mode_without_color_selection(self):
        response = self.client.post("/api/game/start-current", json={
            "mode": "analysis", "current_actor": "ai", "continuation": "new",
        })
        self.assertEqual(response.status_code, 202)
        state = wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertEqual(state["game_mode"], "analysis")
        self.assertEqual(state["side_to_move"], "b")
        self.assertEqual(state["controllers"], {"w": "human", "b": "human"})

    def test_current_board_api_rejects_unknown_mode(self):
        response = self.client.post("/api/game/start-current", json={
            "mode": "unknown", "current_actor": "player",
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_game_mode")

    def test_current_board_api_starts_from_captured_position(self):
        response = self.client.post(
            "/api/game/start-current",
            json={"player_color": "red", "current_actor": "player"},
        )
        self.assertEqual(response.status_code, 202)
        state = wait_for(self.service, Phase.PLAYER_TURN.value)
        self.assertEqual(state["current_turn"], "player")

    def test_control_post_rejects_non_json_form(self):
        response = self.client.post("/api/game/start", data={"first_actor": "player"})
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.get_json()["error"]["code"], "json_required")

    def test_control_post_rejects_cross_origin_json(self):
        response = self.client.post(
            "/api/game/start",
            json={"first_actor": "player"},
            headers={"Origin": "http://attacker.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "origin_not_allowed")

    def test_control_post_rejects_missing_origin(self):
        client = self.ui.test_client()
        response = client.post("/api/game/start", json={"first_actor": "player"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "origin_not_allowed")

    def test_same_origin_json_control_does_not_require_startup_code(self):
        client = self.ui.test_client()
        response = client.post(
            "/api/game/start",
            json={"first_actor": "robot"},
            headers={"Origin": f"http://localhost:{self.config.network.ui_port}"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"]["code"], "invalid_first_actor")

    def test_operator_base_tcp_confirmation_is_not_on_player_page(self):
        player = self.client.get("/player").get_data(as_text=True)
        monitor = self.client.get("/monitor").get_data(as_text=True)
        self.assertNotIn('id="confirmBaseTcp"', player)
        self.assertIn('id="confirmBaseTcp"', monitor)

    def test_pages_observe_same_revision(self):
        first = self.client.get("/api/state").get_json()
        second = self.ui.test_client().get("/api/state").get_json()
        self.assertEqual(first["revision"], second["revision"])

    def test_sse_publishes_same_state(self):
        before = self.service.get_state()["revision"]
        self.service.store.update(lambda state: state.update(message="SSE sync marker"))
        response = self.client.get(f"/api/events?revision={before}", buffered=False)
        chunk = next(response.response).decode("utf-8")
        payload = json.loads(next(line[6:] for line in chunk.splitlines() if line.startswith("data: ")))
        self.assertEqual(payload["message"], "SSE sync marker")
        self.assertEqual(payload["revision"], self.service.get_state()["revision"])
        response.close()

    def test_media_route_rejects_invalid_path(self):
        self.assertEqual(self.client.get("/api/media/not-a-capture/v1").status_code, 404)
        self.assertEqual(self.client.get("/api/media/20261010T010203Z-12345678/../../x").status_code, 404)

    def test_media_route_serves_only_annotated_view(self):
        capture_id = "20261010T010203Z-12345678"
        self.service.submit_start("player")
        state = wait_for(self.service, Phase.PLAYER_TURN.value)
        game_id = state["game"]["id"]
        directory = Path(self.temp.name) / game_id / "captures" / capture_id
        directory.mkdir(parents=True)
        (directory / "v1-yolo.jpg").write_bytes(b"\xff\xd8\xff\xd9")
        response = self.client.get(f"/api/media/{capture_id}/v1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        response.close()

    def test_failed_board_validation_still_exposes_both_annotated_images(self):
        def failed_capture(capture_id, trigger):
            trigger()
            game_id = self.service.get_state()["game"]["id"]
            directory = self.config.capture_root / game_id
            directory.mkdir(parents=True, exist_ok=True)
            paths = [directory / f"R001_C001_current_board_{capture_id}_{view}_yolo.jpg"
                     for view in ("v1", "v2")]
            for path in paths:
                path.write_bytes(b"\xff\xd8\xff\xd9")
            result = CaptureResult(capture_id, starting_board(), *paths, [[0.9] * 9 for _ in range(10)])
            raise BoardObservationError("兵辨識到 6 枚", result)

        self.service.vision.capture = failed_capture
        self.service.submit_start_current("red", "player", continuation="new")
        state = wait_for(self.service, Phase.ERROR.value)
        self.assertEqual(state["vision"]["status"], "error")
        self.assertIsNone(self.service._board)
        self.assertEqual(self.service.robot.moves, [])
        self.assertTrue(state["vision"]["capture_id"])
        for view in ("v1", "v2"):
            with self.client.get(state["vision"][f"{view}_url"]) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "image/jpeg")

    def test_media_route_serves_flat_round_filename(self):
        capture_id = "20261010T010203Z-12345678"
        self.service.submit_start("player")
        state = wait_for(self.service, Phase.PLAYER_TURN.value)
        game_id = state["game"]["id"]
        directory = Path(self.temp.name) / game_id
        directory.mkdir(parents=True)
        filename = f"R002_C003_ai_verify_{capture_id}_v2_yolo.jpg"
        (directory / filename).write_bytes(b"\xff\xd8\xff\xd9")

        response = self.client.get(f"/api/media/{capture_id}/v2")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        response.close()


class VisionReceiverTests(unittest.TestCase):
    def setUp(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime")
        network = replace(CONFIG.network, robot_ip="127.0.0.1", max_upload_bytes=1024)
        self.config = replace(CONFIG, network=network, capture_root=Path(self.temp.name))
        self.inbox = DualViewInbox(Path(self.temp.name), "127.0.0.1", 1024)
        self.app = create_vision_app(self.inbox, self.config)
        self.client = self.app.test_client()
        self.capture_id = "20261010T010203Z-12345678"
        self.inbox.arm(self.capture_id)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def jpeg(width=16, height=12):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (width, height), "white").save(output, format="JPEG")
        return output.getvalue()

    def upload(self, data=None, mimetype="image/jpeg", ip="127.0.0.1"):
        if data is None:
            data = self.jpeg()
        return self.client.post(
            self.config.network.vision_path,
            data={"file": (io.BytesIO(data), "frame.jpg", mimetype)},
            content_type="multipart/form-data",
            environ_base={"REMOTE_ADDR": ip},
        )

    def test_accepts_exactly_two_jpegs(self):
        first = self.upload().get_json()
        self.assertEqual(first["message"], "success")
        self.assertEqual((first["image_w"], first["image_h"]), (16, 12))
        self.assertEqual(first["annotations"][0]["label"], "capture_ok")
        self.assertEqual(self.upload().get_json()["message"], "success")
        self.assertEqual(self.upload().status_code, 409)

    def test_rejects_wrong_source_ip(self):
        self.assertEqual(self.upload(ip="192.168.1.44").status_code, 403)

    def test_rejects_non_jpeg(self):
        self.assertEqual(self.upload(data=b"hello", mimetype="text/plain").status_code, 400)

    def test_rejects_fake_jpeg(self):
        self.assertEqual(self.upload(data=b"\xff\xd8\xffnot jpeg", mimetype="image/jpeg").status_code, 400)

    def test_rejects_oversized_upload(self):
        self.assertEqual(self.upload(data=b"\xff\xd8\xff" + b"x" * 2048).status_code, 413)

    def test_rejects_upload_without_armed_capture(self):
        self.inbox.cancel()
        self.assertEqual(self.upload().status_code, 409)

    def test_no_public_index(self):
        self.assertEqual(self.client.get("/").status_code, 404)


if __name__ == "__main__":
    unittest.main()
