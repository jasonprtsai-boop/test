"""Thread-safe game state with monotonic revisions for HTTP and SSE."""

from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Phase(str, Enum):
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    INITIAL_CAPTURE = "INITIAL_CAPTURE"
    PLAYER_TURN = "PLAYER_TURN"
    AI_THINKING = "AI_THINKING"
    ENGINE_READY = "ENGINE_READY"
    ROBOT_MOVING = "ROBOT_MOVING"
    VERIFYING = "VERIFYING"
    ERROR = "ERROR"
    FINISHED = "FINISHED"
    STOPPED = "STOPPED"


def initial_state() -> dict[str, Any]:
    return {
        "revision": 0,
        "updated_at": utc_now(),
        "phase": Phase.IDLE.value,
        "game": {"id": None},
        "resume": {
            "available": False,
            "game_id": None,
            "saved_at": None,
            "phase": None,
            "current_turn": None,
            "first_actor": None,
            "game_mode": None,
            "controllers": None,
            "side_to_move": None,
        },
        "first_actor": None,
        "current_turn": None,
        "game_mode": None,
        "controllers": {"w": "human", "b": "engine"},
        "side_to_move": None,
        "execution_mode": "automatic",
        "message": "系統待命；真機動作預設鎖定。",
        "last_move": None,
        "game_result": None,
        "analysis": {
            "status": "idle",
            "side": None,
            "bestmove": None,
            "ponder": None,
            "lines": [],
            "updated_at": None,
            "error": None,
        },
        "error": None,
        "robot": {
            "pose": None,
            "pose_updated_at": None,
            "pose_stale": True,
            "current_action": "待命",
            "next_action": "完成安全預檢",
            "motion_may_continue": False,
        },
        "vision": {
            "status": "idle",
            "capture_id": None,
            "active_capture_id": None,
            "last_attempt_id": None,
            "round": None,
            "capture_sequence": None,
            "purpose": None,
            "v1_url": None,
            "v2_url": None,
            "updated_at": None,
        },
        "preflight": {"ready": False, "checks": [], "checked_at": None},
    }


class StateStore:
    def __init__(self) -> None:
        self._state = initial_state()
        self._condition = threading.Condition()

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return copy.deepcopy(self._state)

    def update(self, mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._condition:
            candidate = copy.deepcopy(self._state)
            mutator(candidate)
            candidate["revision"] += 1
            candidate["updated_at"] = utc_now()
            result = copy.deepcopy(candidate)
            self._state = candidate
            self._condition.notify_all()
            return result

    def wait_after(self, revision: int, timeout: float) -> dict[str, Any] | None:
        with self._condition:
            if self._state["revision"] <= revision:
                self._condition.wait_for(lambda: self._state["revision"] > revision, timeout=timeout)
            if self._state["revision"] <= revision:
                return None
            return copy.deepcopy(self._state)
