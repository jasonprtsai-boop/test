"""Workflow audit history and record-payload composition.

The journal owns diagnostic histories only. GameService remains the single
source of truth for the live board, workflow phase and hardware command queue.
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .board import Board, Move, Position, board_to_fen, clone_board, position_to_uci
from .config import AppConfig, MotionConfig
from .state import utc_now


class WorkflowJournal:
    """Own append-only workflow evidence without owning live game state."""

    def __init__(self) -> None:
        self.move_history: list[dict[str, Any]] = []
        self.capture_history: list[dict[str, Any]] = []
        self.motion_history: list[dict[str, Any]] = []
        self.event_history: list[dict[str, Any]] = []
        self.engine_history: list[dict[str, Any]] = []

    def reset(self) -> None:
        """Clear histories in place so compatibility references stay valid."""
        self.move_history.clear()
        self.capture_history.clear()
        self.motion_history.clear()
        self.event_history.clear()
        self.engine_history.clear()

    def restore(self, snapshot: dict[str, Any]) -> None:
        self.reset()
        sources = (
            (self.move_history, snapshot.get("move_history"), False),
            (self.capture_history, snapshot.get("capture_history"), False),
            (self.motion_history, snapshot.get("motion_history"), False),
            (self.event_history, snapshot.get("event_history"), False),
            (self.engine_history, snapshot.get("engine_history"), True),
        )
        for target, source, deep in sources:
            if not isinstance(source, list):
                continue
            values = [item for item in source if isinstance(item, dict)]
            target.extend(copy.deepcopy(values) if deep else [dict(item) for item in values])

    def append_state_history(
        self,
        state: dict[str, Any],
        recorded_at: str,
        *,
        player_side: str,
        ai_side: str,
    ) -> None:
        """Append deduplicated move and state-transition evidence."""
        last_move = state.get("last_move")
        if isinstance(last_move, dict):
            marker = _move_marker(last_move)
            previous_marker = _move_marker(self.move_history[-1]) if self.move_history else None
            if marker != previous_marker:
                actor = str(last_move.get("actor", ""))
                source = last_move.get("source", [])
                destination = last_move.get("destination", [])
                self.move_history.append({
                    **last_move,
                    "sequence": len(self.move_history) + 1,
                    "committed_at": last_move.get("committed_at") or recorded_at,
                    "side": last_move.get("side") or (player_side if actor == "player" else ai_side),
                    "source_text": position_to_uci(tuple(source)) if len(source) == 2 else "",
                    "destination_text": position_to_uci(tuple(destination)) if len(destination) == 2 else "",
                    "capture_id": last_move.get("capture_id") or state.get("vision", {}).get("capture_id"),
                })

        error = state.get("error") if isinstance(state.get("error"), dict) else None
        robot = state.get("robot", {})
        vision = state.get("vision", {})
        event = {
            "recorded_at": recorded_at,
            "phase": state.get("phase"),
            "current_turn": state.get("current_turn"),
            "message": state.get("message"),
            "capture_id": (
                vision.get("active_capture_id")
                or vision.get("last_attempt_id")
                or vision.get("capture_id")
            ),
            "current_action": robot.get("current_action"),
            "next_action": robot.get("next_action"),
            "motion_state_uncertain": bool(robot.get("motion_may_continue")),
            "error": error,
        }
        comparable = {key: value for key, value in event.items() if key != "recorded_at"}
        previous_comparable = None
        if self.event_history:
            previous_comparable = {
                key: value
                for key, value in self.event_history[-1].items()
                if key not in ("recorded_at", "sequence")
            }
        if comparable != previous_comparable:
            event["sequence"] = len(self.event_history) + 1
            self.event_history.append(event)

    def current_round(self) -> int:
        return len(self.move_history) // 2 + 1

    def move_payload(
        self,
        motion: MotionConfig,
        actor: str,
        move: Move,
        before: Board,
        after: Board,
        *,
        player_side: str,
        ai_side: str,
        game_id: str | None,
        last_capture_record: dict[str, Any] | None,
        side: str | None = None,
        repaired_cells: list[Position] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        side = side or (player_side if actor == "player" else ai_side)
        other_side = "b" if side == "w" else "w"
        sequence = len(self.move_history) + 1
        image_verified = (extra or {}).get("confirmation_method") != "motion_completed"
        capture = (last_capture_record or {}) if image_verified else {}
        fen_error = None
        try:
            fen_before = board_to_fen(before, side)
            fen_after = board_to_fen(after, other_side)
        except ValueError as exc:
            # Audit recording must not turn a pre-existing validation issue
            # into a new hardware-workflow failure.
            fen_before = fen_after = None
            fen_error = str(exc)
        payload = {
            "actor": actor,
            "side": side,
            "round": (sequence + 1) // 2,
            "uci": move.uci,
            "source": list(move.source),
            "destination": list(move.destination),
            "source_position": position_details(motion, move.source),
            "destination_position": position_details(motion, move.destination),
            "captured": bool(move.captured_piece),
            "captured_piece_id": move.captured_piece,
            "fen_before": fen_before,
            "fen_after": fen_after,
            "fen_error": fen_error,
            "capture_id": capture.get("capture_id"),
            "capture_seconds": capture.get("capture_seconds"),
            "vision_processing_seconds": capture.get("processing_seconds"),
            "vision_total_seconds": capture.get("total_seconds"),
            "vision_repaired_cells": [position_to_uci(item) for item in (repaired_cells or [])],
            "committed_at": utc_now(),
        }
        if extra:
            payload.update(extra)
            if "repaired_cells" in extra and not payload["vision_repaired_cells"]:
                payload["vision_repaired_cells"] = list(extra["repaired_cells"])
        if payload.get("legality_seconds") is None:
            check = next((
                query for query in reversed(self.engine_history)
                if query.get("operation") == "is_legal"
                and query.get("requested_move") == move.uci
                and query.get("status") == "completed"
            ), {})
            payload["legality_seconds"] = check.get("duration_seconds")
        if actor == "player" and payload.get("turn_seconds") is None and capture.get("started_at"):
            payload["turn_seconds"] = round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(capture["started_at"])).total_seconds(),
                3,
            )
        payload.setdefault("move_attempt_id", f"{game_id}:{sequence}")
        if image_verified and last_capture_record is not None:
            last_capture_record.update(
                accepted_board=clone_board(after),
                accepted_move_sequence=sequence,
                accepted_uci=move.uci,
                acceptance_status="committed",
            )
        return payload

    def new_motion_record(
        self,
        motion: MotionConfig,
        move: Move,
        pending_metrics: dict[str, Any],
        retry_pick: bool,
        started_at: str,
    ) -> dict[str, Any]:
        source = position_details(motion, move.source)
        destination = position_details(motion, move.destination)
        return {
            "sequence": len(self.motion_history) + 1,
            "round": self.current_round(),
            "move_uci": move.uci,
            "move_attempt_id": pending_metrics.get("move_attempt_id"),
            "verification_status": "pending",
            "committed_move_sequence": None,
            "retry": retry_pick,
            "status": "moving",
            "started_at": started_at,
            "completed_at": None,
            "duration_seconds": None,
            "source": source,
            "destination": destination,
            "target_x": destination["robot_x"],
            "target_y": destination["robot_y"],
            "target_z": motion.z_safe,
            "actual_x": None,
            "actual_y": None,
            "actual_z": None,
            "position_error_mm": None,
            "orientation_error_deg": None,
            "graveyard_x": None,
            "graveyard_y": None,
            "error_type": None,
            "error_message": None,
        }


def _move_marker(move: dict[str, Any]) -> tuple[Any, ...]:
    return (
        move.get("committed_at"),
        move.get("actor"),
        move.get("uci"),
        tuple(move.get("source", [])),
        tuple(move.get("destination", [])),
    )


def serialize_analysis_line(line: Any, side: str) -> dict[str, Any]:
    payload = asdict(line)
    payload["pv"] = list(line.pv)
    if line.wdl is not None:
        side_win, draw, side_loss = line.wdl
        red_win, black_win = (side_win, side_loss) if side == "w" else (side_loss, side_win)
        payload.update(
            red_win_per_mille=red_win,
            draw_per_mille=draw,
            black_win_per_mille=black_win,
        )
    return payload


def record_settings(config: AppConfig, build_identity: dict[str, Any]) -> dict[str, Any]:
    return {
        "post_move_image_verification": False,
        "hardware_enabled": config.safety.hardware_enabled,
        "engine_limit_strength": config.engine.limit_strength,
        "engine_target_elo": config.engine.target_elo,
        "engine_depth": config.engine.depth,
        "engine_movetime_ms": config.engine.movetime_ms,
        "base_name": config.motion.base_name,
        "tcp_name": config.motion.tcp_name,
        "speed": config.motion.speed,
        "acceleration": config.motion.acceleration,
        "z_safe": config.motion.z_safe,
        "z_approach": config.motion.z_approach,
        "z_pick": config.motion.z_pick,
        "vision_confidence": config.vision.confidence,
        **build_identity,
    }


def capture_purpose_code(purpose: str) -> str:
    return {
        "初始盤面": "initial",
        "以目前棋盤建立對局": "current_board",
        "恢復上一局": "resume",
        "玩家走子": "player_move",
        "AI 落子驗證": "ai_verify",
        "重試前盤面確認": "retry_check",
    }.get(purpose, "capture")


def update_observation_record(record: dict[str, Any], result: Any) -> None:
    values = [
        float(value)
        for row in result.confidence or []
        for value in row
        if isinstance(value, (int, float))
    ]
    low, average, high = (
        (min(values), sum(values) / len(values), max(values))
        if values else (None, None, None)
    )
    record.update(
        observed_board=clone_board(result.board),
        confidence_grid=copy.deepcopy(result.confidence),
        v1_yolo=result.v1_path.name,
        v2_yolo=result.v2_path.name,
        result_json=result.result_path.name if result.result_path is not None else record.get("result_json"),
        capture_seconds=result.capture_seconds,
        processing_seconds=result.processing_seconds,
        total_seconds=result.total_seconds,
        confidence_min=low,
        confidence_avg=average,
        confidence_max=high,
        detected_pieces=sum(piece != 0 for row in result.board for piece in row),
    )


def update_artifact_status(record: dict[str, Any], directory: Path | None) -> None:
    expected = record.setdefault("expected_artifacts", {})
    status = record.setdefault("artifact_status", {})
    for key in ("v1_raw", "v2_raw", "v1_yolo", "v2_yolo", "result_json"):
        name = record.get(key) or expected.get(key)
        if name:
            expected[key] = name
        exists = bool(directory and name and (directory / name).is_file())
        status[key] = "available" if exists else "not_received" if key.endswith("raw") else "not_generated"
        record[key] = name if exists else None


def position_details(motion: MotionConfig, position: Position) -> dict[str, Any]:
    col, row = position
    player_row = 9 - row
    player_col = 8 - col
    point_offset = player_col if player_row % 2 == 0 else 8 - player_col
    x, y = motion.board_xy_matrix()[row][col]
    return {
        "col": col,
        "row": row,
        "board_text": position_to_uci(position),
        "matrix": f"R{row + 1}C{col + 1}",
        "player_matrix": f"R{player_row + 1}C{player_col + 1}",
        "player_point": player_row * 9 + point_offset + 1,
        "robot_x": round(x, 3),
        "robot_y": round(y, 3),
    }


def complete_motion_record(
    record: dict[str, Any],
    pose: dict[str, Any] | None,
    orientation: tuple[float, float, float] | None,
) -> None:
    if record.get("position_error_mm") is not None or not isinstance(pose, dict):
        return
    actual = tuple(pose.get(key) for key in ("x", "y", "z"))
    target = tuple(record.get(key) for key in ("target_x", "target_y", "target_z"))
    if all(isinstance(value, (int, float)) for value in (*actual, *target)):
        record.update(
            actual_x=actual[0],
            actual_y=actual[1],
            actual_z=actual[2],
            position_error_mm=round(sum((a - b) ** 2 for a, b in zip(actual, target)) ** 0.5, 4),
        )
    if orientation is not None and all(isinstance(pose.get(key), (int, float)) for key in ("rx", "ry", "rz")):
        errors = [
            abs((float(pose[key]) - target_angle + 180.0) % 360.0 - 180.0)
            for key, target_angle in zip(("rx", "ry", "rz"), orientation)
        ]
        record["orientation_error_deg"] = round(max(errors), 4)
