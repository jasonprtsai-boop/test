from __future__ import annotations

import threading
import time
from pathlib import Path

from tmchess.board import Move, apply_move, clone_board, standard_starting_board
from tmchess.config import CONFIG
from tmchess.engine import EngineAnalysis
from tmchess.robot import RobotCallbacks, StopRequested
from tmchess.service import GameService
from tmchess.vision import CaptureResult


def starting_board():
    return standard_starting_board()


class FakePreflight:
    def __init__(self, ready=True):
        self.ready = ready
        self.calls = []
        self.base_tcp = False

    def run(self, dynamic=True):
        self.calls.append(dynamic)
        return {
            "ready": self.ready,
            "checks": [{"key": "fake", "label": "測試預檢", "ok": self.ready, "detail": "通過" if self.ready else "失敗"}],
            "checked_at": "2026-10-10T00:00:00+00:00",
        }

    def confirm_base_tcp(self, base_name, tcp_name):
        self.base_tcp = (base_name, tcp_name) == ("RobotBase", "NOTOOL")
        if not self.base_tcp:
            raise RuntimeError("base/tcp mismatch")


class FakeEngine:
    def __init__(self, suggestions=None, legal=True, error=None, has_legal=True, analysis=None):
        self.suggestions = list(suggestions or [])
        self.legal = legal
        self.error = error
        self.has_legal = has_legal
        self.analysis = analysis
        self.suggest_calls = 0
        self.analyze_calls = 0
        self.legal_calls = 0
        self.has_legal_calls = 0
        self.contexts = []

    def start(self):
        if self.error == "start":
            raise RuntimeError("Pikafish start failed")

    def is_legal(self, board, side, move, **context):
        self.legal_calls += 1
        self.contexts.append(("legal", side, context))
        if self.error == "legal":
            raise RuntimeError("Pikafish legality failed")
        return self.legal

    def suggest(self, board, side, **context):
        self.suggest_calls += 1
        self.contexts.append(("suggest", side, context))
        if self.error == "suggest":
            raise RuntimeError("Pikafish suggest failed")
        return self.suggestions.pop(0) if self.suggestions else None

    def analyze(self, board, side, *, multipv=1, **context):
        self.analyze_calls += 1
        self.contexts.append(("analyze", side, context))
        if self.error == "analyze":
            raise RuntimeError("Pikafish analysis failed")
        if self.analysis is not None:
            return self.analysis
        move = self.suggestions[0] if self.suggestions else None
        return EngineAnalysis(move, None, ())

    def has_legal_move(self, board, side, **context):
        self.has_legal_calls += 1
        self.contexts.append(("has_legal", side, context))
        if self.error == "has_legal":
            raise RuntimeError("Pikafish game-status failed")
        return self.has_legal

    def stop(self):
        pass


class FakeVision:
    def __init__(self, outcomes, blocker=None):
        self.outcomes = list(outcomes)
        self.blocker = blocker
        self.calls = 0
        self.selected_game = None
        self.inbox = self

    def check_ready(self):
        pass

    def select_game(self, game_id):
        self.selected_game = game_id

    def capture(self, capture_id, trigger):
        self.calls += 1
        trigger()
        if self.blocker is not None:
            self.blocker.wait(timeout=2)
        if not self.outcomes:
            raise AssertionError("FakeVision 沒有下一個結果")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        confidence = None
        if isinstance(outcome, tuple):
            outcome, confidence = outcome
        root = Path("unused") / capture_id
        return CaptureResult(
            capture_id,
            clone_board(outcome),
            root / "v1-yolo.jpg",
            root / "v2-yolo.jpg",
            confidence,
        )

    def cancel(self):
        if self.blocker is not None:
            self.blocker.set()

    def close(self):
        self.cancel()


class FakeRobot:
    def __init__(self, error=None, block_move=None):
        self.callbacks = RobotCallbacks()
        self.error = error
        self.moves = []
        self.pick_offsets = []
        self.trigger_calls = 0
        self.block_move = block_move
        self.move_entered = threading.Event()

    def probe(self):
        self.callbacks.pose((1, 2, 3, 4, 5, 6))
        return (1, 2, 3, 4, 5, 6)

    def trigger_capture(self, stop_event):
        if stop_event.is_set():
            raise StopRequested("stopped")
        self.trigger_calls += 1

    def confirm_stationary(self):
        return (1, 2, 3, 4, 5, 6)

    def execute_move(self, move, stop_event, *, source_pick_offset_mm=0.0):
        self.moves.append(move)
        self.pick_offsets.append(source_pick_offset_mm)
        self.callbacks.action("測試移動", "測試驗證")
        self.move_entered.set()
        if self.block_move is not None:
            self.block_move.wait(timeout=2)
        if self.error:
            raise self.error

    def close(self):
        if self.block_move is not None:
            self.block_move.set()


def make_service(
    outcomes,
    suggestions=None,
    *,
    analysis=None,
    legal=True,
    engine_error=None,
    has_legal=True,
    robot=None,
    blocker=None,
    config=CONFIG,
    session_store=None,
    record_writer=None,
):
    vision = FakeVision(outcomes, blocker=blocker)
    engine = FakeEngine(
        suggestions, legal=legal, error=engine_error, has_legal=has_legal, analysis=analysis
    )
    robot = robot or FakeRobot()
    service = GameService(
        config, robot, vision, engine,
        preflight_checker=FakePreflight(True), start_preflight_monitor=False,
        session_store=session_store,
        record_writer=record_writer,
    )
    return service, robot, vision, engine


def wait_for(service, phase, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service.get_state()
        controls = state["controls"]
        stable = (
            phase == "PLAYER_TURN" and controls["can_player_done"]
            or phase == "ENGINE_READY" and controls["can_execute_engine_move"]
            or phase == "ERROR" and (
                controls["can_retry"]
                or controls["can_start"]
                or controls["can_resume"]
                or state["robot"]["motion_may_continue"]
            )
            or phase == "FINISHED" and controls["can_start"]
            or phase == "STOPPED" and not controls["can_stop"]
            or phase not in ("PLAYER_TURN", "ENGINE_READY", "ERROR", "FINISHED", "STOPPED")
        )
        if state["phase"] == phase and stable:
            return state
        time.sleep(0.01)
    raise AssertionError(f"等待 {phase} 逾時；目前 {service.get_state()}")
