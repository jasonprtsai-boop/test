"""Validation and codecs for persisted game-session context."""

from __future__ import annotations

from typing import Any

from .board import (
    Move,
    apply_move,
    clone_board,
    is_red,
    parse_uci_move,
    validate_board,
)
from .session import GAME_ID_PATTERN


def has_complete_replay_context(snapshot: dict[str, Any]) -> bool:
    """Prove that persisted move history rebuilds the saved board and turn."""
    initial_board = snapshot.get("initial_board")
    board = snapshot.get("board")
    if not isinstance(initial_board, list):
        return False
    try:
        validate_board(initial_board)
    except (TypeError, ValueError):
        return False
    if snapshot.get("initial_side_to_move") not in ("w", "b"):
        return False
    move_history = snapshot.get("move_history")
    if not isinstance(move_history, list) or not isinstance(board, list):
        return False
    replay = clone_board(initial_board)
    side = str(snapshot["initial_side_to_move"])
    try:
        for item in move_history:
            if not isinstance(item, dict) or not isinstance(item.get("uci"), str):
                return False
            source, destination = parse_uci_move(item["uci"])
            piece = replay[source[1]][source[0]]
            if piece == 0 or is_red(piece) != (side == "w"):
                return False
            move = Move(
                source,
                destination,
                replay[destination[1]][destination[0]],
                item["uci"],
            )
            replay = apply_move(replay, move)
            side = "b" if side == "w" else "w"
    except (IndexError, TypeError, ValueError):
        return False
    return replay == board and snapshot.get("side_to_move") == side


def session_lineage(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize persisted battle-segment lineage."""
    game_id = snapshot.get("game_id")
    battle_id = snapshot.get("battle_id") or game_id
    parent = snapshot.get("parent_game_id")
    index = snapshot.get("segment_index", 1)
    if any(
        not isinstance(value, str) or not GAME_ID_PATTERN.fullmatch(value)
        for value in (game_id, battle_id)
    ):
        raise ValueError("保存的對戰編號格式錯誤。")
    if parent is not None and (
        not isinstance(parent, str)
        or not GAME_ID_PATTERN.fullmatch(parent)
        or parent == game_id
    ):
        raise ValueError("保存的上一個片段編號錯誤。")
    if type(index) is not int or index < 1:
        raise ValueError("保存的片段序號錯誤。")
    return {
        **(
            {"record_name": str(snapshot.get("record_name") or "")[:40]}
            if "record_name" in snapshot
            else {}
        ),
        "battle_id": battle_id,
        "parent_game_id": parent,
        "segment_index": index,
        "segment_reason": snapshot.get("segment_reason", "legacy_segment"),
        "history_gap": bool(snapshot.get("history_gap", False)),
        "battle_started_at": snapshot.get("battle_started_at") or snapshot.get("started_at"),
    }


def serialize_move(move: Move | None) -> dict[str, Any] | None:
    if move is None:
        return None
    return {
        "source": list(move.source),
        "destination": list(move.destination),
        "captured_piece": move.captured_piece,
        "uci": move.uci,
    }


def deserialize_move(payload: Any) -> Move | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("保存的 pending move 格式錯誤。")
    source = payload.get("source")
    destination = payload.get("destination")
    if (
        not isinstance(source, list)
        or not isinstance(destination, list)
        or len(source) != 2
        or len(destination) != 2
        or any(type(value) is not int for value in source + destination)
        or not isinstance(payload.get("uci"), str)
    ):
        raise ValueError("保存的 pending move 座標錯誤。")
    parsed_source, parsed_destination = parse_uci_move(payload["uci"])
    if tuple(source) != parsed_source or tuple(destination) != parsed_destination:
        raise ValueError("保存的 pending move 座標與 UCI 不一致。")
    captured = payload.get("captured_piece", 0)
    if type(captured) is not int or captured not in range(15):
        raise ValueError("保存的 pending move 吃子資料錯誤。")
    return Move(
        (int(source[0]), int(source[1])),
        (int(destination[0]), int(destination[1])),
        captured,
        str(payload.get("uci", "")),
    )
