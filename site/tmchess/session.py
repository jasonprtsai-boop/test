"""Durable, per-game snapshots for safe process-restart recovery."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .board import validate_board


GAME_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
SESSION_FILENAME = "game.json"
BACKUP_FILENAME = "game.json.bak"
UNSAFE_LEGACY_PHASES = {
    "INITIAL_CAPTURE",
    "VERIFYING",
    "ROBOT_MOVING",
    "STOPPED",
    "ERROR",
}
KNOWN_PHASES = {
    "IDLE",
    "PREFLIGHT",
    "INITIAL_CAPTURE",
    "PLAYER_TURN",
    "AI_THINKING",
    "ENGINE_READY",
    "ROBOT_MOVING",
    "VERIFYING",
    "ERROR",
    "FINISHED",
    "STOPPED",
}


class SessionCorruptionError(RuntimeError):
    """The newest safety snapshot exists but cannot be trusted."""

    def __init__(self, game_id: str, message: str) -> None:
        super().__init__(message)
        self.game_id = game_id


class GameSessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def game_directory(self, game_id: str) -> Path:
        if not GAME_ID_PATTERN.fullmatch(game_id):
            raise ValueError("game id 格式錯誤。")
        path = (self.root / game_id).resolve()
        if self.root not in path.parents:
            raise ValueError("game id 路徑無效。")
        return path

    def create(self, game_id: str) -> Path:
        directory = self.game_directory(game_id)
        with self._lock:
            directory.mkdir(parents=True, exist_ok=False)
        return directory

    def save(self, game_id: str, payload: dict[str, Any]) -> Path:
        directory = self.game_directory(game_id)
        token = uuid.uuid4().hex
        target = directory / SESSION_FILENAME
        backup = directory / BACKUP_FILENAME
        temporary = directory / f".{SESSION_FILENAME}.{token}.tmp"
        backup_temporary = directory / f".{BACKUP_FILENAME}.{token}.tmp"
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with self._lock:
            directory.mkdir(parents=True, exist_ok=True)
            try:
                self._write_durable(temporary, data)
                if target.is_file():
                    self._write_durable(backup_temporary, target.read_bytes())
                    self._replace_with_retry(backup_temporary, backup)
                self._replace_with_retry(temporary, target)
                self._sync_directory(directory)
                return target
            finally:
                for transient in (temporary, backup_temporary):
                    try:
                        transient.unlink(missing_ok=True)
                    except OSError:
                        pass

    @staticmethod
    def _write_durable(path: Path, data: bytes) -> None:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _replace_with_retry(source: Path, destination: Path) -> None:
        """Retry transient Windows sharing violations without exposing a gap."""
        for attempt in range(5):
            try:
                os.replace(source, destination)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        """Persist rename metadata where the platform supports directory fsync."""
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_snapshot(path: Path, game_id: str) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("game_id") != game_id:
            return None
        schema_version = payload.get("schema_version")
        if type(schema_version) is not int or schema_version not in (1, 2, 3):
            return None
        phase = payload.get("phase")
        if not isinstance(phase, str) or phase not in KNOWN_PHASES:
            return None
        board = payload.get("board")
        if board is not None:
            if not isinstance(board, list):
                return None
            try:
                validate_board(board)
            except (TypeError, ValueError):
                return None
        motion_flag = payload.get("motion_state_uncertain")
        if schema_version == 3 and not isinstance(motion_flag, bool):
            return None
        if schema_version in (1, 2) and not isinstance(motion_flag, bool):
            payload = dict(payload)
            if phase in UNSAFE_LEGACY_PHASES:
                payload["motion_state_uncertain"] = True
                payload["recovery_warning"] = (
                    "舊版快照缺少動作安全狀態；已依中斷階段鎖定真機動作。"
                )
        return payload

    @staticmethod
    def _resumable(payload: dict[str, Any]) -> dict[str, Any] | None:
        if payload.get("phase") == "FINISHED":
            return None
        if isinstance(payload.get("board"), list) or payload.get("motion_state_uncertain"):
            return payload
        return None

    def load_latest(self) -> dict[str, Any] | None:
        with self._lock:
            directories = sorted(
                (
                    path
                    for path in self.root.iterdir()
                    if path.is_dir()
                    and GAME_ID_PATTERN.fullmatch(path.name)
                    and ((path / SESSION_FILENAME).exists() or (path / BACKUP_FILENAME).exists())
                ),
                # UUIDs are random: two games created in the same second must
                # be ordered by their durable snapshot time, not UUID text.
                key=lambda path: (
                    path.name[:16],
                    max((p.stat().st_mtime_ns for p in
                         (path / SESSION_FILENAME, path / BACKUP_FILENAME) if p.exists()), default=0),
                    path.name,
                ),
                reverse=True,
            )
            if not directories:
                return None
            directory = directories[0]
            game_id = directory.name
            target = directory / SESSION_FILENAME
            payload = self._read_snapshot(target, game_id)
            if payload is not None:
                return self._resumable(payload)

            backup = self._read_snapshot(directory / BACKUP_FILENAME, game_id)
            if backup is not None:
                recovered = dict(backup)
                recovered["motion_state_uncertain"] = True
                recovered["recovery_warning"] = "最新安全快照損壞，已載入上一版並鎖定真機動作。"
                return recovered
            raise SessionCorruptionError(
                game_id,
                "最新對局的安全快照損壞或版本不受支援；已鎖定真機動作。",
            )
