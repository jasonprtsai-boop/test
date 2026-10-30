"""Application service: one state source and one serialized hardware queue."""

from __future__ import annotations

import copy
import logging
import queue
import shutil
import threading
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .board import (
    Board,
    Move,
    Position,
    apply_move,
    board_to_fen,
    clone_board,
    find_player_move,
    is_red,
    parse_uci_move,
    position_to_uci,
    standard_starting_board,
    validate_board,
    validate_pawn_positions,
)
from .config import AppConfig
from .records import GameRecordWriter
from .robot import GripperStateUnknown, Pose, RobotCallbacks, StopRequested
from .session import GAME_ID_PATTERN, GameSessionStore, SessionCorruptionError
from .state import Phase, StateStore, initial_state, utc_now
from .vision import BoardObservationError


LOGGER = logging.getLogger(__name__)


class CommandConflict(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StatePersistenceError(RuntimeError):
    """A control command could not be committed to durable storage."""

    code = "state_persistence_failed"


class VerificationError(RuntimeError):
    def __init__(self, message: str, observed: Board | None = None) -> None:
        super().__init__(message)
        self.observed = observed


class PreflightChecker:
    def __init__(self, config: AppConfig, robot, vision, engine) -> None:
        self.config = config
        self.robot = robot
        self.vision = vision
        self.engine = engine
        self._operator_lock = threading.Lock()
        self._base_tcp_confirmed = False

    def confirm_base_tcp(self, base_name: str, tcp_name: str) -> None:
        expected_base = self.config.motion.base_name
        expected_tcp = self.config.motion.tcp_name
        if base_name != expected_base or tcp_name != expected_tcp:
            raise CommandConflict(
                "base_tcp_mismatch",
                f"必須確認 TMflow 顯示 {expected_base} / {expected_tcp}。",
            )
        with self._operator_lock:
            self._base_tcp_confirmed = True

    def base_tcp_confirmed(self) -> bool:
        with self._operator_lock:
            return self._base_tcp_confirmed

    @staticmethod
    def _check(checks: list[dict[str, Any]], key: str, label: str, ok: bool, detail: str) -> None:
        checks.append({"key": key, "label": label, "ok": bool(ok), "detail": detail})

    def run(self, dynamic: bool = True) -> dict[str, Any]:
        c = self.config
        checks: list[dict[str, Any]] = []
        self._check(checks, "hardware_enabled", "真機動作開關", c.safety.hardware_enabled,
                    "已啟用" if c.safety.hardware_enabled else "HARDWARE_ENABLED 已關閉")
        missing = c.motion.missing_fields()
        self._check(checks, "coordinates", "棋盤、姿態、Z 與落子箱座標", not missing,
                    "完整" if not missing else "缺少：" + ", ".join(missing))
        safety_flags = {
            "emergency_stop": ("實體急停確認", c.safety.emergency_stop_confirmed),
            "motion_parameters": ("速度與加速度簽核", c.safety.motion_parameters_verified),
            "dual_view_upload": ("V1／V2 即時上傳簽核", c.safety.dual_view_upload_verified),
            "base": ("Base 簽核", c.safety.base_verified),
            "tcp": ("TCP 簽核", c.safety.tcp_verified),
            "base_tcp_runtime_guard": (
                "Base／TCP 啟動時一致性保護",
                c.safety.base_tcp_runtime_guard_verified,
            ),
            "board": ("棋盤 9×10 矩陣簽核", c.safety.board_coordinates_verified),
            "z": ("三段 Z 高度簽核", c.safety.z_heights_verified),
            "graveyard": ("落子箱簽核", c.safety.graveyard_verified),
            "graveyard_capacity": (
                "落子箱滿載容量風險接受",
                c.safety.graveyard_capacity_risk_accepted,
            ),
        }
        for key, (label, ok) in safety_flags.items():
            self._check(checks, key, label, ok, "已確認" if ok else "尚未確認")
        model_ok = c.vision.model_path.is_file()
        engine_ok = c.engine.executable.is_file() and c.engine.nnue.is_file()
        self._check(checks, "model", "YOLO 模型檔", model_ok, str(c.vision.model_path))
        self._check(checks, "pikafish", "Pikafish 與 NNUE", engine_ok, str(c.engine.executable.parent))
        disk_target = c.capture_root if c.capture_root.exists() else c.capture_root.parent
        try:
            free_bytes = shutil.disk_usage(disk_target).free
        except OSError as exc:
            self._check(checks, "disk_space", "執行資料磁碟空間", False, str(exc))
        else:
            disk_ok = free_bytes >= c.minimum_free_disk_bytes
            self._check(
                checks,
                "disk_space",
                "執行資料磁碟空間",
                disk_ok,
                f"可用 {free_bytes / (1024 ** 3):.2f} GiB；最低 1.00 GiB",
            )
        static_ready = all(item["ok"] for item in checks)
        if dynamic and static_ready:
            confirmed = self.base_tcp_confirmed()
            self._check(
                checks,
                "base_tcp_runtime_confirmation",
                "本次啟動 Base／TCP 人工確認",
                confirmed,
                (
                    f"已確認 {c.motion.base_name} / {c.motion.tcp_name}"
                    if confirmed
                    else f"請在操作端確認 {c.motion.base_name} / {c.motion.tcp_name}"
                ),
            )
            if not confirmed:
                return {"ready": False, "checks": checks, "checked_at": utc_now()}
            for key, label, probe in (
                ("vision_runtime", "TMvision receiver 與 YOLO 載入", self.vision.check_ready),
                ("engine_runtime", "Pikafish 啟動", self.engine.start),
                ("listen_runtime", "Listen、Pose 與吸盤關閉", self.robot.probe),
            ):
                try:
                    probe()
                except Exception as exc:
                    self._check(checks, key, label, False, str(exc))
                else:
                    self._check(checks, key, label, True, "通過")
        elif dynamic:
            self._check(checks, "runtime", "真機動態預檢", False, "靜態安全條件未完成，未連線")
        return {"ready": all(item["ok"] for item in checks), "checks": checks, "checked_at": utc_now()}


class GameService:
    START_MODES = {
        "human_red": ({"w": "human", "b": "engine"}, "automatic", "player"),
        "human_black": ({"w": "engine", "b": "human"}, "automatic", "ai"),
        "engine_vs_engine": ({"w": "engine", "b": "engine"}, "step", "ai"),
        "analysis": ({"w": "human", "b": "human"}, "analysis", "player"),
    }

    def __init__(
        self,
        config: AppConfig,
        robot,
        vision,
        engine,
        store: StateStore | None = None,
        preflight_checker: PreflightChecker | None = None,
        start_preflight_monitor: bool = True,
        session_store: GameSessionStore | None = None,
        record_writer: GameRecordWriter | None = None,
    ) -> None:
        self.config = config
        self.store = store or StateStore()
        self.robot = robot
        self.vision = vision
        self.engine = engine
        self.preflight_checker = preflight_checker or PreflightChecker(config, robot, vision, engine)
        self.session_store = session_store
        self.record_writer = record_writer
        self._record_schedule_error: str | None = None
        self._queue: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._lock = threading.RLock()
        self._busy = False
        self._closed = False
        self._instance_id = uuid.uuid4().hex
        self._preflight_monitor_started = False
        self._preflight_thread: threading.Thread | None = None
        self._close_event = threading.Event()
        self._stop_event = threading.Event()
        self._board: Board | None = None
        self._game_id: str | None = None
        self._game_started_at: str | None = None
        self._move_history: list[dict[str, Any]] = []
        self._capture_history: list[dict[str, Any]] = []
        self._motion_history: list[dict[str, Any]] = []
        self._event_history: list[dict[str, Any]] = []
        self._engine_history: list[dict[str, Any]] = []
        self._battle_metadata: dict[str, Any] = {}
        self._last_snapshot: dict[str, Any] | None = None
        self._game_mode = "standard"
        self._initial_board: Board | None = None
        self._pending_metrics: dict[str, Any] = {}
        self._last_capture_record: dict[str, Any] | None = None
        self._resume_snapshot: dict[str, Any] | None = None
        self._pending_move: Move | None = None
        self._pending_expected: Board | None = None
        self._pending_before: Board | None = None
        self._pending_retry_count = 0
        self._last_observed: Board | None = None
        self._player_side = "w"
        self._ai_side = "b"
        self._controllers = {"w": "human", "b": "engine"}
        self._side_to_move = "w"
        self._initial_side_to_move = "w"
        self._execution_mode = "automatic"
        self._worker = threading.Thread(target=self._worker_loop, name="game-worker", daemon=True)
        self._bind_callbacks()
        self._load_resume_candidate()
        static_report = self.preflight_checker.run(dynamic=False)
        self._static_preflight_ready = static_report["ready"]
        self._set_preflight(static_report)
        self._worker.start()
        if start_preflight_monitor:
            self.start_preflight_monitor()

    def _bind_callbacks(self) -> None:
        if hasattr(self.robot, "callbacks"):
            self.robot.callbacks = RobotCallbacks(self._on_pose, self._on_action)

    @staticmethod
    def _has_complete_replay_context(snapshot: dict[str, Any]) -> bool:
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

    def _load_resume_candidate(self) -> None:
        if self.session_store is None:
            return
        try:
            snapshot = self.session_store.load_latest()
        except SessionCorruptionError as exc:
            self._game_id = exc.game_id
            self._resume_snapshot = None
            self.store.update(lambda state: (
                state.update(
                    phase=Phase.ERROR.value,
                    message=str(exc),
                    error={
                        "code": "session_corrupt",
                        "message": str(exc),
                        "retryable": False,
                    },
                ),
                state["game"].update(id=exc.game_id),
                state["robot"].update(
                    motion_may_continue=True,
                    current_action="安全快照損壞，Robot 狀態未知",
                    next_action="由操作人員確認 Robot 已停止",
                ),
            ))
            return
        if snapshot is None:
            return
        self._last_snapshot = copy.deepcopy(snapshot)
        if self.record_writer is not None:
            # Rebuild a possibly interrupted workbook from the durable JSON
            # snapshot without delaying startup preflight or robot commands.
            self._schedule_record(snapshot)
        has_board = isinstance(snapshot.get("board"), list)
        replay_complete = has_board and self._has_complete_replay_context(snapshot)
        # Keep the raw board snapshot so a safety acknowledgement can update it
        # without erasing data. Availability still requires full replay context.
        self._resume_snapshot = snapshot if has_board else None
        motion_value = snapshot.get("motion_state_uncertain")
        if isinstance(motion_value, bool):
            motion_uncertain = motion_value
        else:
            motion_uncertain = (
                snapshot.get("schema_version") == 3
                or snapshot.get("phase")
                in {
                    Phase.INITIAL_CAPTURE.value,
                    Phase.VERIFYING.value,
                    Phase.ROBOT_MOVING.value,
                    Phase.STOPPED.value,
                    Phase.ERROR.value,
                }
            )
        if motion_uncertain:
            # Keep the latest folder writable so an operator acknowledgement can
            # durably clear a safety-only snapshot even if no board exists yet.
            self._game_id = str(snapshot.get("game_id", "")) or None
        self.store.update(lambda state: (
            state.update(
                message=(
                    str(snapshot.get("recovery_warning"))
                    if motion_uncertain and snapshot.get("recovery_warning")
                    else "上一局缺少完整著法歷史；請從目前棋盤建立新的對局基準。"
                    if has_board and not replay_complete
                    else state["message"]
                )
            ),
            state["resume"].update(
                available=replay_complete,
                game_id=snapshot.get("game_id"),
                saved_at=snapshot.get("saved_at"),
                phase=snapshot.get("phase"),
                current_turn=snapshot.get("current_turn"),
                first_actor=snapshot.get("first_actor"),
                game_mode=snapshot.get("game_mode"),
                controllers=snapshot.get("controllers"),
                side_to_move=snapshot.get("side_to_move"),
            ),
            state["robot"].update(
                motion_may_continue=motion_uncertain,
                current_action=(
                    "上次中斷時 Robot 狀態未知"
                    if motion_uncertain else state["robot"]["current_action"]
                ),
                next_action=(
                    "由操作人員確認 Robot 已停止"
                    if motion_uncertain else state["robot"]["next_action"]
                ),
            ),
        ))

    def _on_pose(self, pose: Pose) -> None:
        def mutate(state):
            state["robot"]["pose"] = dict(zip(("x", "y", "z", "rx", "ry", "rz"), (round(v, 3) for v in pose)))
            state["robot"]["pose_updated_at"] = utc_now()
            state["robot"]["pose_stale"] = False
        self.store.update(mutate)

    def _on_action(self, current: str, next_action: str) -> None:
        self.store.update(lambda state: state["robot"].update(current_action=current, next_action=next_action))
        with self._lock:
            if self._motion_history and self._motion_history[-1].get("status") == "moving":
                record = self._motion_history[-1]
                record["last_action"] = current
                record.setdefault("stages", []).append({"recorded_at": utc_now(), "action": current, "next_action": next_action})

    def _set_preflight(self, report: dict[str, Any]) -> None:
        self.store.update(lambda state: state.update(preflight=report))

    def refresh_preflight(self, dynamic: bool = True) -> dict[str, Any]:
        report = self.preflight_checker.run(dynamic=dynamic)
        self._set_preflight(report)
        if dynamic and report["ready"]:
            # Runtime probing does not clear a durable interrupted-motion lock.
            # A stable Pose is only one observation; a human must confirm that
            # the physical cell is stopped before automatic work is unlocked.
            self.store.update(lambda state: state["robot"].update(
                current_action=(
                    state["robot"]["current_action"]
                    if state["robot"].get("motion_may_continue")
                    else "動態預檢通過"
                ),
                next_action=(
                    "由操作人員確認 Robot 已停止"
                    if state["robot"].get("motion_may_continue")
                    else "等待操作"
                ),
            ))
        return report

    def start_preflight_monitor(self) -> None:
        """Run the one-time runtime preflight after dependent servers are ready."""
        with self._lock:
            if self._closed or self._preflight_monitor_started or not self._static_preflight_ready:
                return
            self._preflight_monitor_started = True
            self.store.update(lambda state: state["preflight"].update(
                ready=False,
                checks=[
                    *[item for item in state["preflight"]["checks"] if item.get("key") != "runtime_pending"],
                    {
                        "key": "runtime_pending",
                        "label": "啟動動態預檢",
                        "ok": False,
                        "detail": "正在確認執行環境",
                    },
                ],
                checked_at=utc_now(),
            ))
            self._preflight_thread = threading.Thread(
                target=self._preflight_loop,
                name="preflight-monitor",
                daemon=True,
            )
            self._preflight_thread.start()

    def _preflight_loop(self) -> None:
        # Configuration is immutable for a running process.  When a static
        # safety item is missing, retrying would only create noisy revisions.
        if not self._static_preflight_ready:
            return
        while not self._close_event.is_set():
            try:
                report = self.refresh_preflight(dynamic=True)
            except Exception as exc:
                LOGGER.exception("[PREFLIGHT] monitor iteration failed")
                self._set_preflight({
                    "ready": False,
                    "checked_at": utc_now(),
                    "checks": [{
                        "key": "runtime_failure", "label": "啟動預檢失敗，將自動重試",
                        "ok": False, "detail": str(exc),
                    }],
                })
            else:
                if report["ready"]:
                    return
            if self._close_event.wait(5):
                return

    def set_engine_movetime(self, value):
        return self._set_engine_limit("movetime_ms", value, 100, 10000)

    def set_engine_depth(self, value):
        return self._set_engine_limit("depth", value, 1, 30)

    def _set_engine_limit(self, field, value, minimum, maximum):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"設定必須是 {minimum} 到 {maximum} 的整數。")
        with self._lock:
            settings = replace(self.config.engine, **{field: value})
            self.engine.config = settings
            self.config = replace(self.config, engine=settings)
            return {field: value}

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return self._get_state_locked()

    def _get_state_locked(self) -> dict[str, Any]:
        snapshot = self.store.snapshot()
        snapshot["instance_id"] = self._instance_id
        snapshot["engine_movetime_ms"] = self.config.engine.movetime_ms
        snapshot["engine_depth"] = self.config.engine.depth
        pose_time = snapshot["robot"].get("pose_updated_at")
        if pose_time:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(pose_time)).total_seconds()
                snapshot["robot"]["pose_stale"] = age > 2.0
            except ValueError:
                snapshot["robot"]["pose_stale"] = True
        with self._lock:
            phase = snapshot["phase"]
            recoverable = bool(snapshot.get("error", {}).get("retryable")) if snapshot.get("error") else False
            motion_uncertain = bool(snapshot["robot"].get("motion_may_continue"))
            current_controller = self._controllers.get(self._side_to_move)
            active = phase in {
                Phase.PREFLIGHT.value,
                Phase.INITIAL_CAPTURE.value,
                Phase.PLAYER_TURN.value,
                Phase.AI_THINKING.value,
                Phase.ENGINE_READY.value,
                Phase.ROBOT_MOVING.value,
                Phase.VERIFYING.value,
            } or (
                phase == Phase.ERROR.value
                and (recoverable or motion_uncertain or self._board is not None)
            )
            can_initialize_game = (
                not self._busy
                and not motion_uncertain
                and snapshot["preflight"]["ready"]
                and (not recoverable or phase == Phase.STOPPED.value)
                and phase in (
                    Phase.IDLE.value,
                    Phase.STOPPED.value,
                    Phase.FINISHED.value,
                    Phase.ERROR.value,
                )
            )
            snapshot["controls"] = {
                # Standard start creates a battle; current-board recovery may
                # continue this segment or link an explicit new baseline.
                "can_start": can_initialize_game,
                "can_start_current": can_initialize_game,
                "can_resume": (
                    not self._busy
                    and not motion_uncertain
                    and snapshot["preflight"]["ready"]
                    and bool(snapshot["resume"]["available"])
                    and phase in (Phase.IDLE.value, Phase.STOPPED.value, Phase.FINISHED.value, Phase.ERROR.value)
                ),
                "can_player_done": (
                    not self._busy
                    and phase == Phase.PLAYER_TURN.value
                    and current_controller == "human"
                ),
                "can_hint": (
                    not self._busy
                    and not motion_uncertain
                    and phase == Phase.PLAYER_TURN.value
                    and current_controller == "human"
                    and self._board is not None
                ),
                "can_execute_engine_move": (
                    not self._busy
                    and not motion_uncertain
                    and phase == Phase.ENGINE_READY.value
                    and current_controller == "engine"
                    and self._pending_move is not None
                ),
                "can_retry": (
                    not self._busy
                    and not motion_uncertain
                    and phase == Phase.ERROR.value
                    and recoverable
                    and self._pending_move is not None
                ),
                "can_stop": active or self._busy,
                "can_confirm_safe_stop": motion_uncertain and not self._busy,
                "can_confirm_base_tcp": not any(
                    check.get("key") == "base_tcp_runtime_confirmation" and check.get("ok")
                    for check in snapshot["preflight"].get("checks", [])
                ),
            }
            if self._closed:
                snapshot["controls"] = {key: False for key in snapshot["controls"]}
        snapshot["hardware"] = {
            "enabled": self.config.safety.hardware_enabled,
            "stop_notice": "網站停止只會停止流程與後續命令，不能取代實體 Emergency Stop。",
        }
        game_id = snapshot.get("game", {}).get("id")
        record_ready = False
        snapshot["game"]["record_path"] = None
        record_status: dict[str, Any] = {}
        if self.record_writer is not None and isinstance(game_id, str):
            try:
                record_path = self.record_writer.path_for(game_id)
                snapshot["game"]["record_path"] = str(record_path)
                record_ready = record_path.is_file()
                record_status = self.record_writer.status_for(game_id)
            except (ValueError, OSError) as exc:
                record_ready = False
                record_status = {"error": str(exc)}
        if self._record_schedule_error:
            record_status = {**record_status, "error": self._record_schedule_error}
        snapshot["game"]["record_url"] = "/api/game/record" if game_id else None
        snapshot["game"]["record_ready"] = record_ready
        snapshot["game"]["record_status"] = record_status
        return snapshot

    def current_record_path(self) -> Path | None:
        with self._lock:
            writer = self.record_writer
            game_id = self._game_id
        if writer is None or game_id is None:
            return None
        path = writer.path_for(game_id)
        return path if path.is_file() else None

    def wait_state_after(self, revision: int, timeout: float = 15.0) -> dict[str, Any] | None:
        result = self.store.wait_after(revision, timeout)
        return self.get_state() if result is not None else None

    def _require(self, condition: bool, code: str, message: str) -> None:
        if self._closed:
            raise CommandConflict("service_closed", "服務已關閉，請等待重新啟動完成。")
        if not condition:
            raise CommandConflict(code, message)

    def _record_persistence_failure(
        self,
        exc: Exception,
        *,
        context: str,
        phase: str | None = Phase.ERROR.value,
        force_motion_lock: bool = False,
        reset_game: bool = False,
        release_busy: bool = True,
    ) -> StatePersistenceError:
        message = f"{context}無法安全保存，命令未完成：{exc}"
        if release_busy:
            self._busy = False
        if reset_game:
            self._game_id = None
            self._game_started_at = None
        self.store.update(lambda state: (
            state.update(
                phase=state["phase"] if phase is None else phase,
                message=message,
                error={
                    "code": StatePersistenceError.code,
                    "message": message,
                    "retryable": False,
                    "type": type(exc).__name__,
                },
            ),
            state["robot"].update(
                motion_may_continue=(
                    True if force_motion_lock else state["robot"].get("motion_may_continue", False)
                ),
                current_action=(
                    "安全狀態保存失敗，Robot 狀態保持鎖定"
                    if force_motion_lock else state["robot"]["current_action"]
                ),
                next_action=(
                    "確認儲存空間後，再由操作人員確認 Robot 已停止"
                    if force_motion_lock else state["robot"]["next_action"]
                ),
            ),
            state["game"].update(id=None) if reset_game else None,
        ))
        return StatePersistenceError(message)

    def submit_reset_page(self) -> dict[str, Any]:
        """Start a fresh UI session without clearing physical safety evidence."""
        with self._lock:
            state = self.get_state()
            self._require(not self._busy, "workflow_busy", "流程仍在執行，請先停止並等待動作結束。")
            self._require(
                state["phase"] in (Phase.IDLE.value, Phase.ERROR.value, Phase.STOPPED.value, Phase.FINISHED.value),
                "game_active", "對局仍在進行，請先停止再重設。",
            )
            self._require(
                not state["robot"].get("motion_may_continue"),
                "motion_state_unknown",
                "請先到監控台由現場操作人員確認 Robot 已停止及吸盤狀態，再重新開啟玩家頁。",
            )
            # Existing game files are retained. Detach runtime state only.
            self._board = None
            self._game_id = self._game_started_at = None
            self._resume_snapshot = None
            self._pending_move = self._pending_expected = self._pending_before = self._last_observed = None
            self._pending_retry_count = 0
            self._initial_board = None
            self._move_history = []
            self._capture_history = []
            self._motion_history = []
            self._event_history = []
            self._engine_history = []
            self._battle_metadata = {}
            # Retain the last battle as a recovery candidate across a page
            # reset. Explicit standard/new start still creates a new battle.
            self._pending_metrics = {}
            self._last_capture_record = None
            self._controllers = {"w": "human", "b": "engine"}
            self._side_to_move = self._initial_side_to_move = "w"
            self._player_side, self._ai_side = "w", "b"
            self._execution_mode = "automatic"
            self._game_mode = "standard"
            self._stop_event.clear()
            fresh = initial_state()
            fresh["preflight"] = state["preflight"]
            fresh["robot"] = state["robot"]
            fresh["robot"].update(current_action="等待新局", next_action="選擇開始方式")
            def reset(current):
                revision = current["revision"]
                current.clear()
                current.update(fresh)
                current["revision"] = revision
            self.store.update(reset)
        return self.get_state()

    def submit_start(self, first_actor: str) -> dict[str, Any]:
        self._require(first_actor in ("player", "ai"), "invalid_first_actor", "first_actor 必須是 player 或 ai。")
        mode = "human_red" if first_actor == "player" else "human_black"
        return self._submit_start_mode(mode)

    def submit_start_mode(self, mode: str, *, record_name: str = "") -> dict[str, Any]:
        self._require(
            mode in self.START_MODES,
            "invalid_game_mode",
            "mode 必須是 human_red、human_black、engine_vs_engine 或 analysis。",
        )
        return self._submit_start_mode(mode, record_name=record_name)

    def _submit_start_mode(self, mode: str, *, record_name: str = "") -> dict[str, Any]:
        controllers, execution_mode, first_actor = self.START_MODES[mode]
        with self._lock:
            state = self.get_state()
            self._require(state["controls"]["can_start"], "preflight_or_state_blocked", "安全預檢未通過，或目前狀態不能開始。")
            self._busy = True
            self._stop_event.clear()
            self._board = None
            self._pending_move = self._pending_expected = self._pending_before = self._last_observed = None
            self._pending_retry_count = 0
            self._controllers = dict(controllers)
            self._side_to_move = "w"
            self._execution_mode = execution_mode
            human_sides = [side for side, controller in controllers.items() if controller == "human"]
            self._player_side = human_sides[0] if human_sides else "w"
            self._ai_side = self._other_side(self._player_side)
            try:
                self._begin_new_game(mode, record_name=record_name)
                red_is_human = controllers["w"] == "human"
                self.store.update(lambda current: (
                    current.update(
                        phase=Phase.PLAYER_TURN.value if red_is_human else Phase.INITIAL_CAPTURE.value,
                        first_actor=first_actor,
                        current_turn="player" if red_is_human else None,
                        game_mode=mode,
                        controllers=dict(controllers),
                        side_to_move="w",
                        execution_mode=execution_mode,
                        message=(
                            "遊戲開始，輪到紅方走子。"
                            if red_is_human else "開始對局，準備拍攝並驗證初始盤面…"
                        ),
                        error=None,
                        last_move=None,
                    ),
                    current["analysis"].update(
                        status="idle", side=None, bestmove=None, ponder=None,
                        lines=[], updated_at=None, error=None,
                    ),
                ))
                self.store.update(lambda current: current["robot"].update(motion_may_continue=False))
                self.store.update(lambda current: current["resume"].update(available=False))
                self._persist_session()
            except StatePersistenceError as exc:
                raise self._record_persistence_failure(
                    exc,
                    context="新對局",
                    reset_game=True,
                ) from exc
            self._queue.put(("start", mode))
        return self.get_state()

    def submit_start_current(self, player_color: str, current_actor: str, *, continuation: str = "auto", record_name: str = "", mode: str | None = None) -> dict[str, Any]:
        self._require(continuation in ("auto", "rebaseline", "new"), "invalid_continuation", "恢復方式必須是 auto、rebaseline 或 new。")
        self._require(
            player_color in ("red", "black"),
            "invalid_player_color",
            "player_color 必須是 red 或 black。",
        )
        self._require(
            current_actor in ("player", "ai"),
            "invalid_current_actor",
            "current_actor 必須是 player 或 ai。",
        )
        self._require(mode is None or mode in ("human_red", "engine_vs_engine", "analysis"),
                      "invalid_game_mode", "目前棋盤模式必須是 human_red、engine_vs_engine 或 analysis。")
        if mode is not None:
            self._require(player_color == "red", "invalid_player_color", "新模式固定以紅方作為玩家方。")
        with self._lock:
            state = self.get_state()
            self._require(
                state["controls"]["can_start_current"],
                "preflight_or_state_blocked",
                "安全預檢未通過，或目前狀態不能以實體棋盤開始。",
            )
            previous = self._last_snapshot or self._resume_snapshot
            if continuation == "new" or not previous or previous.get("phase") == Phase.FINISHED.value:
                previous = None
            if previous:
                self._lineage(previous)
            if previous and continuation == "auto" and self._has_complete_replay_context(previous):
                requested_player = "w" if player_color == "red" else "b"
                self._require(mode is not None or previous.get("player_side") == requested_player, "recovery_color_conflict",
                              "接續對戰不能更換玩家棋色；請保留原棋色或明確選擇全新對戰。")
                self._resume_snapshot = copy.deepcopy(previous)
                # Reuse the existing recovery path and its pending-move guards.
                return self._queue_resume("auto", self._resume_snapshot)
            self._busy = True
            self._stop_event.clear()
            self._board = None
            self._pending_move = self._pending_expected = self._pending_before = self._last_observed = None
            self._pending_retry_count = 0
            self._player_side, self._ai_side = (
                ("w", "b") if player_color == "red" else ("b", "w")
            )
            selected_mode = mode or (previous.get("game_mode") if previous and previous.get("game_mode") in ("engine_vs_engine", "analysis") else "current_board")
            if selected_mode in self.START_MODES:
                controllers, self._execution_mode, _ = self.START_MODES[selected_mode]
                self._controllers = dict(controllers)
            else:
                self._controllers = {self._player_side: "human", self._ai_side: "engine"}
                self._execution_mode = "automatic"
            self._side_to_move = self._player_side if current_actor == "player" else self._ai_side
            # first_actor remains the persisted source of the red/black side
            # assignment.  For a mid-game baseline it does not imply move 1.
            first_actor = "player" if player_color == "red" else "ai"
            try:
                self._begin_new_game(selected_mode, previous=previous, record_name=record_name)
                self.store.update(lambda current: (
                    current.update(
                        phase=Phase.VERIFYING.value,
                        first_actor=first_actor,
                        current_turn=current_actor,
                        game_mode=selected_mode,
                        controllers=dict(self._controllers),
                        side_to_move=self._side_to_move,
                        execution_mode=self._execution_mode,
                        message="正在拍攝目前棋盤並建立新的對局基準…",
                        error=None,
                        last_move=None,
                    ),
                    current["analysis"].update(
                        status="idle", side=None, bestmove=None, ponder=None,
                        lines=[], updated_at=None, error=None,
                    ),
                    current["robot"].update(motion_may_continue=False),
                    current["resume"].update(available=False),
                ))
                self._persist_session()
            except StatePersistenceError as exc:
                raise self._record_persistence_failure(
                    exc,
                    context="目前棋盤對局",
                    reset_game=True,
                ) from exc
            self._queue.put(("start_current", (player_color, current_actor)))
        return self.get_state()

    def submit_resume(self, current_actor: str) -> dict[str, Any]:
        self._require(
            current_actor in ("player", "ai"),
            "invalid_current_actor",
            "current_actor 必須是 player 或 ai。",
        )
        with self._lock:
            state = self.get_state()
            self._require(
                state["controls"]["can_resume"] and self._resume_snapshot is not None,
                "nothing_to_resume",
                "目前沒有可繼續的上一局。",
            )
            return self._queue_resume(current_actor, self._resume_snapshot)

    def _queue_resume(self, current_actor: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Called under the command lock after safety and availability checks."""
        with self._lock:
            self._busy = True
            self._stop_event.clear()
            self.store.update(lambda current: (
                current.update(
                    phase=Phase.VERIFYING.value,
                    first_actor=snapshot.get("first_actor"),
                    current_turn=snapshot.get("current_turn") if current_actor == "auto" else current_actor,
                    message="正在重新拍攝並核對上一局實體盤面…",
                    error=None,
                    last_move=snapshot.get("last_move"),
                ),
                current["analysis"].update(
                    status="idle", side=None, bestmove=None, ponder=None,
                    lines=[], updated_at=None, error=None,
                ),
                current["game"].update(id=snapshot.get("game_id")),
                current["resume"].update(available=False),
            ))
            self._queue.put(("resume", (current_actor, snapshot)))
        return self.get_state()

    def submit_player_done(self) -> dict[str, Any]:
        with self._lock:
            state = self.get_state()
            self._require(state["controls"]["can_player_done"], "not_player_turn", "目前不是可提交的玩家回合。")
            self._busy = True
            self.store.update(lambda current: (
                current.update(
                    phase=Phase.VERIFYING.value, message="正在拍攝並驗證玩家走子…", error=None,
                ),
                current["analysis"].update(
                    status="idle", side=None, bestmove=None, ponder=None,
                    lines=[], updated_at=None, error=None,
                ),
            ))
            self._queue.put(("player_done", None))
        return self.get_state()

    def submit_hint(self) -> dict[str, Any]:
        with self._lock:
            state = self.get_state()
            self._require(state["controls"]["can_hint"], "hint_not_available", "目前不能取得提示。")
            self._busy = True
            side = self._side_to_move
            self.store.update(lambda current: current["analysis"].update(
                status="calculating",
                side=side,
                bestmove=None,
                ponder=None,
                lines=[],
                updated_at=None,
                error=None,
            ))
            self._queue.put(("hint", side))
        return self.get_state()

    def submit_execute_engine_move(self) -> dict[str, Any]:
        with self._lock:
            state = self.get_state()
            self._require(
                state["controls"]["can_execute_engine_move"],
                "engine_move_not_ready",
                "目前沒有可確認執行的引擎著法。",
            )
            self._busy = True
            self.store.update(lambda current: current.update(
                message="已確認執行這一步，正在啟動機械手臂…",
                error=None,
            ))
            self._queue.put(("execute_engine", None))
        return self.get_state()

    def submit_retry(self) -> dict[str, Any]:
        with self._lock:
            state = self.get_state()
            self._require(state["controls"]["can_retry"], "nothing_to_retry", "目前沒有可安全重試的 pending move。")
            self._busy = True
            self.store.update(lambda current: current.update(
                phase=Phase.VERIFYING.value, message="先重新拍攝，確認棋子實際位置…", error=None,
            ))
            self._queue.put(("retry", None))
        return self.get_state()

    def submit_stop(self) -> dict[str, Any]:
        with self._lock:
            state = self.get_state()
            self._require(state["controls"]["can_stop"], "already_stopped", "目前沒有執行中的流程。")
            self._stop_event.set()
            if hasattr(self.vision, "cancel"):
                self.vision.cancel()
            elif hasattr(self.vision, "inbox"):
                self.vision.inbox.cancel()
            moving = bool(state["robot"].get("motion_may_continue")) or state["phase"] == Phase.ROBOT_MOVING.value
            self.store.update(lambda current: (
                current.update(phase=Phase.STOPPED.value, current_turn=None,
                               message="停止已要求；不再排入後續動作。"),
                current["robot"].update(
                    current_action="等待目前低階動作回傳" if moving else "流程已停止",
                    next_action="使用實體 Emergency Stop 處理危險狀況",
                    motion_may_continue=moving,
                ),
            ))
            try:
                self._persist_session()
            except StatePersistenceError as exc:
                raise self._record_persistence_failure(
                    exc,
                    context="停止狀態",
                    phase=None,
                    force_motion_lock=moving,
                    release_busy=False,
                ) from exc
        return self.get_state()

    def submit_confirm_base_tcp(self, base_name: str, tcp_name: str) -> dict[str, Any]:
        with self._lock:
            self._require(not self._closed, "service_closed", "服務已關閉。")
            self.preflight_checker.confirm_base_tcp(base_name, tcp_name)
            # This acknowledgement must not overwrite active game diagnostics.
            self.store.update(lambda state: None)
        return self.get_state()

    def submit_confirm_safe_stop(self, confirmed: bool) -> dict[str, Any]:
        self._require(confirmed, "confirmation_required", "必須由操作人員確認 Robot 已完全停止。")
        with self._lock:
            state = self.get_state()
            self._require(
                state["controls"]["can_confirm_safe_stop"],
                "safe_stop_not_required",
                "目前沒有待確認的中斷動作。",
            )
            self.store.update(lambda current: current["robot"].update(
                motion_may_continue=False,
                current_action="操作人員已確認 Robot 停止",
                next_action="可重新拍攝盤面後恢復",
            ))
            self.store.update(lambda current: current.update(error=None))
            self._event_history.append({
                "sequence": len(self._event_history) + 1, "recorded_at": utc_now(),
                "event_type": "operator_safe_stop_confirmation", "phase": state['phase'],
                "message": "現場操作人員確認 Robot 已停止；此確認不代表棋步已完成。",
                "current_action": "人工確認停止", "motion_state_uncertain": False,
                "confirmed_by": "local_operator", "error": copy.deepcopy(state.get('error')),
            })
            try:
                if (
                    self.session_store is not None
                    and self._game_id is not None
                    and self._board is None
                    and self._resume_snapshot is not None
                ):
                    # The saved board has not been restored into runtime state.
                    # Preserve it instead of writing a safety-only board=None.
                    payload = dict(self._resume_snapshot)
                    payload["motion_state_uncertain"] = False
                    payload["saved_at"] = utc_now()
                    events = copy.deepcopy(payload.get("event_history", []))
                    confirmation = dict(self._event_history[-1], sequence=len(events) + 1)
                    payload["event_history"] = [*events, confirmation]
                    self.session_store.save(self._game_id, payload)
                    if self.record_writer is not None:
                        self._schedule_record(payload)
                    self._resume_snapshot = payload
                    self._last_snapshot = copy.deepcopy(payload)
                else:
                    self._persist_session()
            except Exception as exc:
                failure = exc if isinstance(exc, StatePersistenceError) else StatePersistenceError(str(exc))
                raise self._record_persistence_failure(
                    failure,
                    context="安全停止確認",
                    phase=None,
                    force_motion_lock=True,
                ) from exc
            if self._board is not None:
                self._load_resume_candidate()
        return self.get_state()

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            command, payload = item
            LOGGER.info("[WORKFLOW] command started command=%s", command)
            try:
                if command == "start":
                    self._start(payload)
                elif command == "start_current":
                    self._start_current(payload)
                elif command == "resume":
                    self._resume(payload)
                elif command == "player_done":
                    self._player_done()
                elif command == "hint":
                    self._hint(payload)
                elif command == "execute_engine":
                    self._execute_pending()
                elif command == "retry":
                    self._retry()
            except Exception as exc:
                if isinstance(exc, StopRequested):
                    LOGGER.info("[WORKFLOW] command cancelled command=%s", command)
                else:
                    LOGGER.exception(
                        "[WORKFLOW] command failed command=%s type=%s message=%s",
                        command, type(exc).__name__, exc,
                    )
                if command == "hint":
                    with self._lock:
                        self.store.update(lambda state: state["analysis"].update(
                            status="idle" if self._stop_event.is_set() else "error",
                            updated_at=utc_now(),
                            error=None if self._stop_event.is_set() else {"type": type(exc).__name__, "message": str(exc)},
                        ))
                else:
                    try:
                        self._handle_error(exc)
                    except Exception as recovery_error:
                        # A secondary failure (e.g. reloading a session from an
                        # unavailable disk) must not kill the only queue worker.
                        LOGGER.exception("[WORKFLOW] error recovery failed")
                        with self._lock:
                            if not self._stop_event.is_set():
                                self.store.update(lambda state: state.update(
                                    phase=Phase.ERROR.value,
                                    message=f"{exc}；恢復資料讀取失敗：{recovery_error}",
                                    error={"code": "recovery_failed", "message": str(recovery_error), "retryable": False},
                                ))
            finally:
                record = self._last_capture_record
                if record is not None and record.get("status") == "processing":
                    # A stop or stationary-confirmation error can happen after
                    # vision returned, outside its local exception handler.
                    record.update(
                        status="interrupted" if self._stop_event.is_set() else "failed",
                        completed_at=utc_now(),
                        error_type="StopRequested" if self._stop_event.is_set() else "WorkflowError",
                        error_message=self.store.snapshot().get("message"),
                    )
                    try:
                        self._persist_session()
                    except Exception:
                        LOGGER.exception("[SESSION] failed to save final capture status")
                with self._lock:
                    self._busy = False
                    # Publish final status before another HTTP command can
                    # observe the released busy flag and initialize a new game.
                    self.store.update(lambda _state: None)
                    if self._stop_event.is_set():
                        self.store.update(lambda state: state["robot"].update(
                            current_action=(
                                "流程已停止；低階動作狀態未知"
                                if state["robot"].get("motion_may_continue")
                                else "流程已停止"
                            ),
                        ))
                LOGGER.info("[WORKFLOW] command ended command=%s", command)
                self._queue.task_done()

    def _guard_running(self) -> None:
        if self._closed or self._stop_event.is_set():
            raise StopRequested("流程已停止。")

    @staticmethod
    def _capture_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{uuid.uuid4().hex[:8]}"

    def _begin_new_game(self, game_mode: str, *, previous: dict[str, Any] | None = None, record_name: str = "") -> None:
        lineage = self._lineage(previous) if previous else None
        while True:
            game_id = self._capture_id()
            if self.session_store is None:
                break
            try:
                self.session_store.create(game_id)
            except FileExistsError:
                continue
            except Exception as exc:
                raise StatePersistenceError(f"無法建立對局資料夾：{exc}") from exc
            break
        self._game_id = game_id
        self._battle_metadata = {
            "battle_id": lineage["battle_id"] if lineage else game_id,
            "parent_game_id": previous["game_id"] if previous else None,
            "segment_index": lineage["segment_index"] + 1 if lineage else 1,
            "segment_reason": "rebaseline_after_interruption" if previous else "new_battle",
            "history_gap": bool(previous and previous.get("board") is not None),
            "battle_started_at": (previous.get("battle_started_at") or previous.get("started_at")) if previous else utc_now(),
        }
        if previous is None:
            self._battle_metadata["record_name"] = record_name.strip()[:40]
        elif "record_name" in previous:
            self._battle_metadata["record_name"] = previous["record_name"]
        self._last_snapshot = None
        self._game_started_at = utc_now()
        self._game_mode = game_mode
        self._initial_board = None
        self._initial_side_to_move = self._side_to_move
        self._move_history = []
        self._capture_history = []
        self._motion_history = []
        self._event_history = []
        self._engine_history = []
        self._pending_metrics = {}
        self._last_capture_record = None
        self._resume_snapshot = None
        if hasattr(self.vision, "select_game"):
            self.vision.select_game(game_id)
        self.store.update(lambda state: (
            state["game"].update(id=game_id, **self._battle_metadata),
            state["vision"].update(initial_state()["vision"]),
        ))

    @staticmethod
    def _lineage(snapshot: dict[str, Any]) -> dict[str, Any]:
        game_id = snapshot.get('game_id')
        battle_id = snapshot.get('battle_id') or game_id
        parent = snapshot.get('parent_game_id')
        index = snapshot.get('segment_index', 1)
        if any(not isinstance(v, str) or not GAME_ID_PATTERN.fullmatch(v) for v in (game_id, battle_id)):
            raise ValueError('保存的對戰編號格式錯誤。')
        if parent is not None and (not isinstance(parent, str) or not GAME_ID_PATTERN.fullmatch(parent) or parent == game_id):
            raise ValueError('保存的上一個片段編號錯誤。')
        if type(index) is not int or index < 1:
            raise ValueError('保存的片段序號錯誤。')
        return dict(**({"record_name": str(snapshot.get("record_name") or "")[:40]} if "record_name" in snapshot else {}), battle_id=battle_id, parent_game_id=parent, segment_index=index,
                    segment_reason=snapshot.get('segment_reason', 'legacy_segment'),
                    history_gap=bool(snapshot.get('history_gap', False)),
                    battle_started_at=snapshot.get('battle_started_at') or snapshot.get('started_at'))

    @staticmethod
    def _serialize_move(move: Move | None) -> dict[str, Any] | None:
        if move is None:
            return None
        return {
            "source": list(move.source),
            "destination": list(move.destination),
            "captured_piece": move.captured_piece,
            "uci": move.uci,
        }

    @staticmethod
    def _serialize_analysis_line(line: Any, side: str) -> dict[str, Any]:
        payload = asdict(line)
        payload["pv"] = list(line.pv)
        if line.wdl is not None:
            side_win, draw, side_loss = line.wdl
            red_win, black_win = (
                (side_win, side_loss) if side == "w" else (side_loss, side_win)
            )
            payload.update(
                red_win_per_mille=red_win,
                draw_per_mille=draw,
                black_win_per_mille=black_win,
            )
        return payload

    @staticmethod
    def _deserialize_move(payload: Any) -> Move | None:
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

    def _persist_session(self) -> None:
        # Compose the snapshot and update record history under the same lock as
        # command submissions. This prevents shutdown and worker persistence
        # from observing partially-mutated runtime state.
        with self._lock:
            self._persist_session_locked()

    def _persist_session_locked(self) -> None:
        if self._game_id is None:
            return
        state = self.get_state()
        saved_at = utc_now()
        self._append_record_history(state, saved_at)
        payload = {
            "schema_version": 3,
            "game_id": self._game_id,
            **self._battle_metadata,
            "started_at": self._game_started_at or saved_at,
            "saved_at": saved_at,
            "phase": state["phase"],
            "current_turn": state.get("current_turn"),
            "first_actor": state.get("first_actor"),
            "player_side": self._player_side,
            "ai_side": self._ai_side,
            "game_mode": self._game_mode,
            "controllers": dict(self._controllers),
            "side_to_move": self._side_to_move,
            "execution_mode": self._execution_mode,
            "initial_board": clone_board(self._initial_board) if self._initial_board is not None else None,
            "initial_side_to_move": self._initial_side_to_move,
            "board": clone_board(self._board) if self._board is not None else None,
            "pending_move": self._serialize_move(self._pending_move),
            "pending_expected": clone_board(self._pending_expected) if self._pending_expected is not None else None,
            "pending_before": clone_board(self._pending_before) if self._pending_before is not None else None,
            "pending_retry_count": self._pending_retry_count,
            "last_move": state.get("last_move"),
            "last_capture_id": state["vision"].get("capture_id"),
            "motion_state_uncertain": bool(state["robot"].get("motion_may_continue")),
            "message": state.get("message"),
            "move_history": self._move_history,
            "capture_history": self._capture_history,
            "motion_history": self._motion_history,
            "event_history": self._event_history,
            "engine_history": self._engine_history,
            "pending_metrics": self._pending_metrics,
            "settings": self._record_settings(),
        }
        if self.session_store is not None:
            try:
                self.session_store.save(self._game_id, payload)
            except Exception as exc:
                raise StatePersistenceError(f"無法保存對局快照：{exc}") from exc
        if self.record_writer is not None:
            self._schedule_record(payload)
        self._last_snapshot = copy.deepcopy(payload)

    def _schedule_record(self, payload: dict[str, Any]) -> None:
        try:
            self.record_writer.schedule(payload)
            self._record_schedule_error = None
        except Exception as exc:
            self._record_schedule_error = str(exc)
            LOGGER.exception("[RECORD] Excel scheduling failed; durable session remains authoritative")

    def _append_record_history(self, state: dict[str, Any], recorded_at: str) -> None:
        last_move = state.get("last_move")
        if isinstance(last_move, dict):
            marker = (
                last_move.get("committed_at"),
                last_move.get("actor"),
                last_move.get("uci"),
                tuple(last_move.get("source", [])),
                tuple(last_move.get("destination", [])),
            )
            previous_marker = None
            if self._move_history:
                previous = self._move_history[-1]
                previous_marker = (
                    previous.get("committed_at"),
                    previous.get("actor"),
                    previous.get("uci"),
                    tuple(previous.get("source", [])),
                    tuple(previous.get("destination", [])),
                )
            if marker != previous_marker:
                actor = str(last_move.get("actor", ""))
                source = last_move.get("source", [])
                destination = last_move.get("destination", [])
                self._move_history.append({
                    **last_move,
                    "sequence": len(self._move_history) + 1,
                    "committed_at": last_move.get("committed_at") or recorded_at,
                    "side": (
                        last_move.get("side")
                        or (self._player_side if actor == "player" else self._ai_side)
                    ),
                    "source_text": position_to_uci(tuple(source)) if len(source) == 2 else "",
                    "destination_text": position_to_uci(tuple(destination)) if len(destination) == 2 else "",
                    "capture_id": (
                        last_move.get("capture_id")
                        or state.get("vision", {}).get("capture_id")
                    ),
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
        if self._event_history:
            previous_comparable = {
                key: value for key, value in self._event_history[-1].items()
                if key not in ("recorded_at", "sequence")
            }
        if comparable != previous_comparable:
            event["sequence"] = len(self._event_history) + 1
            self._event_history.append(event)

    def _record_settings(self) -> dict[str, Any]:
        return {
            "post_move_image_verification": False,
            "hardware_enabled": self.config.safety.hardware_enabled,
            "engine_depth": self.config.engine.depth,
            "engine_movetime_ms": self.config.engine.movetime_ms,
            "base_name": self.config.motion.base_name,
            "tcp_name": self.config.motion.tcp_name,
            "speed": self.config.motion.speed,
            "acceleration": self.config.motion.acceleration,
            "z_safe": self.config.motion.z_safe,
            "z_approach": self.config.motion.z_approach,
            "z_pick": self.config.motion.z_pick,
            "vision_confidence": self.config.vision.confidence,
        }

    def _restore_session(self, snapshot: dict[str, Any]) -> None:
        game_id = str(snapshot.get("game_id", ""))
        board = snapshot.get("board")
        if not isinstance(board, list):
            raise ValueError("上一局沒有可恢復的棋盤。")
        validate_board(board)
        if not self._has_complete_replay_context(snapshot):
            raise ValueError("上一局缺少完整著法歷史，不能安全延續引擎規則狀態。")
        pending = self._deserialize_move(snapshot.get("pending_move"))
        if pending is not None:
            sx, sy = pending.source
            dx, dy = pending.destination
            piece = board[sy][sx]
            if (
                not piece or is_red(piece) != (snapshot["side_to_move"] == "w")
                or pending.captured_piece != board[dy][dx]
                or snapshot.get("pending_before") != board
                or snapshot.get("pending_expected") != apply_move(board, pending)
            ):
                raise ValueError("待確認著法與保存盤面、預期盤面不一致，禁止恢復動作。")
            validate_board(snapshot["pending_expected"])
        elif snapshot.get("pending_before") is not None or snapshot.get("pending_expected") is not None:
            raise ValueError("保存的 pending 盤面缺少對應著法。")
        mode = snapshot.get("game_mode") or "standard"
        if not isinstance(mode, str) or mode not in (*self.START_MODES, "standard", "current_board"):
            raise ValueError("保存的對局模式無效。")
        required_mode = {"engine_vs_engine": "step", "analysis": "analysis"}.get(mode, "automatic")
        if snapshot.get("execution_mode", required_mode) != required_mode:
            raise ValueError("保存的對局模式與執行模式不一致，禁止自動恢復。")
        expected_controllers = self.START_MODES.get(mode)
        if expected_controllers and snapshot.get("controllers", expected_controllers[0]) != expected_controllers[0]:
            raise ValueError("保存的對局模式與控制者不一致。")
        player_side = snapshot.get("player_side", "w")
        ai_side = snapshot.get("ai_side", "b")
        # Legacy dual-engine starts stored both role aliases as red. The
        # controllers and side_to_move above remain authoritative in this mode.
        if mode == "engine_vs_engine" and player_side == ai_side == "w":
            snapshot = copy.deepcopy(snapshot)
            snapshot["ai_side"] = ai_side = "b"
        if player_side not in ("w", "b") or ai_side not in ("w", "b") or player_side == ai_side:
            raise ValueError("保存的玩家與 AI 棋色無效。")
        controllers = snapshot.get("controllers", dict(expected_controllers[0]) if expected_controllers else {player_side: "human", ai_side: "engine"})
        if not isinstance(controllers, dict) or any(controllers.get(side) not in ("human", "engine") for side in ("w", "b")):
            raise ValueError("保存的控制者資料錯誤。")
        if mode not in ("engine_vs_engine", "analysis") and controllers != {player_side: "human", ai_side: "engine"}:
            raise ValueError("保存的棋色與控制者不一致。")
        retry_count = snapshot.get("pending_retry_count", 0)
        if type(retry_count) is not int or retry_count < 0:
            raise ValueError("保存的重試次數錯誤。")
        self._game_id = game_id
        self._battle_metadata = self._lineage(snapshot)
        self.store.update(lambda state: state["game"].update(id=game_id, **self._battle_metadata))
        self._game_started_at = str(snapshot.get("started_at") or snapshot.get("saved_at") or utc_now())
        move_history = snapshot.get("move_history")
        capture_history = snapshot.get("capture_history")
        motion_history = snapshot.get("motion_history")
        event_history = snapshot.get("event_history")
        self._move_history = [dict(item) for item in move_history if isinstance(item, dict)] if isinstance(move_history, list) else []
        self._capture_history = [dict(item) for item in capture_history if isinstance(item, dict)] if isinstance(capture_history, list) else []
        self._motion_history = [dict(item) for item in motion_history if isinstance(item, dict)] if isinstance(motion_history, list) else []
        self._event_history = [dict(item) for item in event_history if isinstance(item, dict)] if isinstance(event_history, list) else []
        engine_history = snapshot.get("engine_history")
        self._engine_history = copy.deepcopy([q for q in engine_history if isinstance(q, dict)]) if isinstance(engine_history, list) else []
        self._game_mode = str(snapshot.get("game_mode") or "standard")
        initial_board = snapshot.get("initial_board")
        self._initial_board = clone_board(initial_board)
        initial_side = snapshot.get("initial_side_to_move")
        self._initial_side_to_move = str(initial_side)
        pending_metrics = snapshot.get("pending_metrics")
        self._pending_metrics = dict(pending_metrics) if isinstance(pending_metrics, dict) else {}
        self._last_capture_record = self._capture_history[-1] if self._capture_history else None
        self._board = clone_board(board)
        self._player_side = str(snapshot.get("player_side", "w"))
        self._ai_side = str(snapshot.get("ai_side", "b"))
        if self._player_side not in ("w", "b") or self._ai_side not in ("w", "b"):
            raise ValueError("上一局的紅黑方資料錯誤。")
        self._controllers = dict(controllers)
        side_to_move = snapshot.get("side_to_move")
        if side_to_move not in ("w", "b"):
            side_to_move = (
                self._ai_side if snapshot.get("current_turn") == "ai" else self._player_side
            )
        self._side_to_move = str(side_to_move)
        default_execution = (
            "step" if self._game_mode == "engine_vs_engine"
            else "analysis" if self._game_mode == "analysis"
            else "automatic"
        )
        self._execution_mode = str(snapshot.get("execution_mode") or default_execution)
        if self._execution_mode not in ("automatic", "step", "analysis"):
            raise ValueError("上一局的執行模式資料錯誤。")
        self._pending_move = self._deserialize_move(snapshot.get("pending_move"))
        pending_expected = snapshot.get("pending_expected")
        pending_before = snapshot.get("pending_before")
        self._pending_expected = clone_board(pending_expected) if isinstance(pending_expected, list) else None
        self._pending_before = clone_board(pending_before) if isinstance(pending_before, list) else None
        self._pending_retry_count = min(1, retry_count)
        self._last_observed = None
        if hasattr(self.vision, "select_game"):
            self.vision.select_game(game_id)

    @staticmethod
    def _capture_purpose_code(purpose: str) -> str:
        return {
            "初始盤面": "initial",
            "以目前棋盤建立對局": "current_board",
            "恢復上一局": "resume",
            "玩家走子": "player_move",
            "AI 落子驗證": "ai_verify",
            "重試前盤面確認": "retry_check",
        }.get(purpose, "capture")

    def _current_round(self) -> int:
        # One round contains up to two committed plies. For a mid-game import,
        # this is the round number within the newly recorded session.
        return len(self._move_history) // 2 + 1

    @staticmethod
    def _confidence_summary(confidence: Any) -> tuple[float | None, float | None, float | None]:
        values = [
            float(value)
            for row in confidence or []
            for value in row
            if isinstance(value, (int, float))
        ]
        if not values:
            return None, None, None
        return min(values), sum(values) / len(values), max(values)

    def _record_observation(self, record: dict[str, Any], result: Any) -> None:
        low, average, high = self._confidence_summary(result.confidence)
        record.update(
            observed_board=clone_board(result.board), confidence_grid=copy.deepcopy(result.confidence),
            v1_yolo=result.v1_path.name, v2_yolo=result.v2_path.name,
            result_json=result.result_path.name if result.result_path is not None else record.get("result_json"),
            capture_seconds=result.capture_seconds, processing_seconds=result.processing_seconds,
            total_seconds=result.total_seconds, confidence_min=low, confidence_avg=average,
            confidence_max=high, detected_pieces=sum(piece != 0 for row in result.board for piece in row),
        )

        # Diagnostic images exist before board validation or stationary checks.
        # Publish them without accepting the board or marking motion complete.
        self.store.update(lambda state: state["vision"].update(
            capture_id=result.capture_id,
            v1_url=f"/api/media/{result.capture_id}/v1",
            v2_url=f"/api/media/{result.capture_id}/v2",
            updated_at=utc_now(),
        ))

    def _accept_observation(self, board: Board, status: str) -> None:
        if self._last_capture_record is not None:
            self._last_capture_record.update(accepted_board=clone_board(board), acceptance_status=status)

    def _record_capture_artifacts(self, record: dict[str, Any]) -> None:
        """Only link files that exist, retaining expected names as diagnostics."""
        directory = self.session_store.game_directory(self._game_id) if self.session_store and self._game_id else None
        expected = record.setdefault("expected_artifacts", {})
        status = record.setdefault("artifact_status", {})
        for key in ("v1_raw", "v2_raw", "v1_yolo", "v2_yolo", "result_json"):
            name = record.get(key) or expected.get(key)
            if name:
                expected[key] = name
            exists = bool(directory and name and (directory / name).is_file())
            status[key] = "available" if exists else "not_received" if key.endswith("raw") else "not_generated"
            record[key] = name if exists else None

    def _capture(self, purpose: str):
        self._guard_running()
        capture_id = self._capture_id()
        round_number = self._current_round()
        sequence = len(self._capture_history) + 1
        purpose_code = self._capture_purpose_code(purpose)
        prefix = capture_id
        if hasattr(self.vision, "configure_capture"):
            prefix = self.vision.configure_capture(round_number, sequence, purpose_code)
        stored_prefix = f"{prefix}_{capture_id}" if prefix != capture_id else capture_id
        started_at = utc_now()
        started = time.monotonic()
        record = {
            "sequence": sequence,
            "round": round_number,
            "capture_id": capture_id,
            "purpose": purpose,
            "purpose_code": purpose_code,
            "status": "processing",
            "started_at": started_at,
            "completed_at": None,
            "filename_prefix": stored_prefix,
            "v1_raw": f"{stored_prefix}_v1.jpg",
            "v2_raw": f"{stored_prefix}_v2.jpg",
            "v1_yolo": f"{stored_prefix}_v1_yolo.jpg",
            "v2_yolo": f"{stored_prefix}_v2_yolo.jpg",
            "result_json": f"{stored_prefix}_result.json",
            "capture_seconds": None,
            "processing_seconds": None,
            "total_seconds": None,
            "confidence_min": None,
            "confidence_avg": None,
            "confidence_max": None,
            "detected_pieces": None,
            "error_type": None,
            "error_message": None,
        }
        self._capture_history.append(record)
        self._last_capture_record = record
        LOGGER.info("[VISION] capture requested id=%s purpose=%s", capture_id, purpose)
        self.store.update(lambda state: (
            state["vision"].update(
                status="processing",
                active_capture_id=capture_id,
                last_attempt_id=capture_id,
                round=round_number,
                capture_sequence=sequence,
                purpose=purpose,
            ),
            state.update(message=f"{purpose}：等待雙視角與 YOLO…"),
            state["robot"].update(
                motion_may_continue=True,
                current_action="準備執行雙視角拍攝路徑",
                next_action="等待 TMflow 回到 Listen 並確認 Pose 穩定",
            ),
        ))
        # Persist before ScriptExit. A process crash after the command is sent
        # must restart locked rather than claiming the robot is stationary.
        self._persist_session()
        def trigger_capture():
            trigger_started = time.monotonic()
            try:
                self.robot.trigger_capture(self._stop_event)
            except Exception:
                record["failure_stage"] = "capture_trigger"
                raise
            finally:
                record["trigger_seconds"] = round(time.monotonic() - trigger_started, 3)
        try:
            try:
                result = self.vision.capture(capture_id, trigger_capture)
            except BoardObservationError as exc:
                self._record_observation(record, exc.result)
                record["validation_error"] = str(exc)
                record["failure_stage"] = "board_validation"
                if self._board is None:
                    raise
                # Only a completed observation with a committed baseline can
                # reach contextual reconciliation. Missing images/ML failures
                # never become a fabricated board.
                result = exc.result
        except Exception as exc:
            record.update(
                status="failed",
                completed_at=utc_now(),
                workflow_seconds=round(time.monotonic() - started, 3),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            if record.get('total_seconds') is None:
                record['total_seconds'] = record['workflow_seconds']
            record.setdefault("failure_stage", "capture_or_inference")
            self._record_capture_artifacts(record)
            self.store.update(lambda state: state["vision"].update(
                status="error",
                active_capture_id=None,
                last_attempt_id=capture_id,
            ))
            if not self._stop_event.is_set() and hasattr(self.robot, "confirm_stationary"):
                try:
                    self.robot.confirm_stationary()
                except Exception:
                    LOGGER.exception("[ROBOT] could not confirm stationary after capture failure")
                else:
                    if not self._stop_event.is_set():
                        self.store.update(lambda state: state["robot"].update(
                            motion_may_continue=False,
                            current_action="拍攝流程已回到 Listen",
                            next_action="處理拍攝錯誤",
                        ))
                        self._persist_session()
            raise
        # Preserve a completed observation even if a subsequent Pose query or
        # stop interrupts the workflow before it can accept the board.
        self._record_observation(record, result)
        record.update(status="needs_reconciliation" if record.get("validation_error") else "ready",
                      completed_at=utc_now(), workflow_seconds=round(time.monotonic() - started, 3))
        if record["total_seconds"] is None:
            record["total_seconds"] = round(time.monotonic() - started, 3)
        self._record_capture_artifacts(record)
        self._persist_session()
        self._guard_running()
        if hasattr(self.robot, "confirm_stationary"):
            stationary_started = time.monotonic()
            try:
                self.robot.confirm_stationary()
            except Exception as exc:
                record.update(status="failed", failure_stage="stationary_confirmation",
                              error_type=type(exc).__name__, error_message=str(exc))
                raise
            finally:
                record["stationary_confirmation_seconds"] = round(time.monotonic() - stationary_started, 3)
                record["completed_at"] = utc_now()
                record["workflow_seconds"] = round(time.monotonic() - started, 3)
        self._guard_running()
        self.store.update(lambda state: state["robot"].update(
            motion_may_continue=False,
            current_action="雙視角拍攝路徑完成",
            next_action="處理棋盤辨識結果",
        ))
        self._persist_session()
        self._guard_running()
        LOGGER.info("[VISION] capture completed id=%s purpose=%s", capture_id, purpose)
        self.store.update(lambda state: state["vision"].update(
            status="ready", capture_id=result.capture_id,
            active_capture_id=None,
            last_attempt_id=result.capture_id,
        ))
        return result

    @staticmethod
    def _other_side(side: str) -> str:
        return "b" if side == "w" else "w"

    @staticmethod
    def _side_label(side: str) -> str:
        return "紅方" if side == "w" else "黑方"

    def _engine_context(self) -> dict[str, Any]:
        """Return the only history context allowed for an engine query."""
        if self._initial_board is None:
            if self._board is None:
                return {}
            raise RuntimeError("目前盤面缺少完整起始盤面，已阻止無歷史的引擎查詢。")
        moves: list[str] = []
        for item in self._move_history:
            value = item.get("uci") if isinstance(item, dict) else None
            if not isinstance(value, str) or not value:
                raise RuntimeError("保存的完整著法歷史缺少 UCI 著法。")
            moves.append(value)
        return {
            "initial_board": clone_board(self._initial_board),
            "initial_side": self._initial_side_to_move,
            "moves": tuple(moves),
        }

    def _query_engine(self, operation: str, board: Board, side: str, *args: Any, **context: Any) -> Any:
        """Record failures and analyses even when no move gets committed."""
        started = time.monotonic()
        try:
            fen = board_to_fen(board, side)
        except ValueError:
            # Recording must not introduce validation beyond the query path.
            fen = None
        record = {
            "sequence": len(self._engine_history) + 1, "operation": operation,
            "started_at": utc_now(), "completed_at": None, "status": "processing",
            "side": side, "fen": fen,
            "requested_move": args[0].uci if args and isinstance(args[0], Move) else None,
            "history_length": len(context.get("moves", ())), "depth": self.config.engine.depth,
            "movetime_ms": self.config.engine.movetime_ms,
        }
        with self._lock:
            self._engine_history.append(record)
            self._persist_session()
        terminal = {}
        try:
            result = getattr(self.engine, operation)(board, side, *args, **context)
            terminal["status"] = "completed"
            if isinstance(result, Move):
                terminal["bestmove"] = result.uci
                analysis = getattr(self.engine, 'last_analysis', None)
                if analysis is not None and operation == 'suggest':
                    terminal['ponder'] = analysis.ponder
                    terminal['lines'] = [self._serialize_analysis_line(line, side) for line in analysis.lines]
            elif hasattr(result, "lines"):
                terminal["bestmove"] = result.bestmove.uci if result.bestmove else None
                terminal["ponder"] = result.ponder
                terminal["lines"] = [self._serialize_analysis_line(line, side) for line in result.lines]
            else:
                terminal["result"] = result
            return result
        except Exception as exc:
            terminal.update(status="failed", error_type=type(exc).__name__, error_message=str(exc))
            raise
        finally:
            with self._lock:
                record.update(**terminal, completed_at=utc_now(), duration_seconds=round(time.monotonic() - started, 3))
                self._persist_session()

    def _start(self, mode: str) -> None:
        self._guard_running()
        controllers, execution_mode, _first_actor = self.START_MODES[mode]
        self._controllers = dict(controllers)
        self._execution_mode = execution_mode
        self._side_to_move = "w"
        if self._controllers["w"] == "human":
            # A human Red start uses the signed-off standard setup. No camera,
            # engine, or robot command is issued until the move is submitted.
            with self._lock:
                self._guard_running()
                self._board = standard_starting_board()
                self._initial_board = clone_board(self._board)
                self._enter_player_turn("遊戲開始，輪到紅方走子。")
                self._persist_session()
            return

        self._update_running(lambda state: state.update(
            phase=Phase.INITIAL_CAPTURE.value, message="拍攝並驗證初始盤面…",
        ))
        result = self._capture("初始盤面")
        if result.board != standard_starting_board():
            raise VerificationError(
                "引擎執紅的新對局必須是標準初始盤面；目前辨識結果不一致，禁止機械動作。",
                result.board,
            )
        with self._lock:
            self._guard_running()
            self._board = clone_board(result.board)
            self._initial_board = clone_board(self._board)
            self._accept_observation(self._board, 'accepted_initial_baseline')
            self._persist_session()
        self._perform_ai_turn()

    def _start_current(self, payload: tuple[str, str]) -> None:
        self._guard_running()
        result = self._capture("以目前棋盤建立對局")
        with self._lock:
            self._guard_running()
            validate_pawn_positions(result.board)
            self._board = clone_board(result.board)
            self._initial_board = clone_board(self._board)
            self._accept_observation(self._board, 'accepted_initial_baseline')
            self._last_observed = None
            self._persist_session()
        if self._controllers.get(self._side_to_move) == "human":
            if not self._query_engine("has_legal_move",
                self._board, self._side_to_move, **self._engine_context()
            ):
                self._update_running(lambda state: state.update(
                    phase=Phase.FINISHED.value,
                    current_turn=None,
                    message=(f"{self._side_label(self._side_to_move)}無合法著法，{self._side_label(self._other_side(self._side_to_move))}獲勝。"
                             if self._game_mode == "analysis" else "目前局面玩家無合法著法，AI 獲勝。"),
                    error=None,
                ))
                self._persist_session()
                return
            self._enter_player_turn(f"目前棋盤已建立為新基準，輪到{self._side_label(self._side_to_move)}。")
            self._persist_session()
            return
        self._perform_ai_turn()

    def _resume(self, payload: tuple[str, dict[str, Any]]) -> None:
        current_actor, snapshot = payload
        self._guard_running()
        self._restore_session(snapshot)
        if self._pending_move is not None:
            legal = self._query_engine("is_legal", self._board, self._side_to_move, self._pending_move, **self._engine_context())
            self._guard_running()
            if not legal:
                raise ValueError("保存的 pending move 不合法，禁止恢復動作。")
        requested_side = self._side_to_move
        if current_actor != "auto" and self._game_mode not in ("engine_vs_engine", "analysis"):
            requested_side = self._player_side if current_actor == "player" else self._ai_side
        self._update_running(lambda state: state.update(
            game_mode=self._game_mode,
            controllers=dict(self._controllers),
            side_to_move=self._side_to_move,
            execution_mode=self._execution_mode,
            current_turn=(
                "player" if self._controllers[self._side_to_move] == "human" else "ai"
            ),
        ))
        if self._board is None:
            raise RuntimeError("上一局沒有可恢復的棋盤。")
        result = self._capture("恢復上一局")
        self._last_observed = clone_board(result.board)

        if self._pending_move is not None:
            if self._pending_expected is None or self._pending_before is None:
                raise VerificationError("上一局 pending move 資料不完整，禁止自動恢復。", result.board)
            protected = {self._pending_move.source, self._pending_move.destination}
            expected_match = self._match_expected_board(
                self._pending_expected, result.board, result.confidence, protected
            )
            before_match = self._match_expected_board(
                self._pending_before, result.board, result.confidence, protected
            )
            if expected_match is not None and before_match is None:
                self._log_temporal_repairs("恢復已完成的 AI 落子", expected_match[1])
                self._commit_pending_move("已確認中斷前的 AI 落子完成，輪到玩家。")
                return
            if before_match is not None and expected_match is None:
                self._log_temporal_repairs("恢復 AI 落子前盤面", before_match[1])
                raise VerificationError("上一局的 AI 著法尚未完成；請確認現場後按「重新落子」。", result.board)
            raise VerificationError("實體盤面既非中斷前盤面也非預期盤面，請人工檢查。", result.board)

        persisted_match = self._match_expected_board(
            self._board, result.board, result.confidence
        )
        if current_actor == "auto" and persisted_match is None and self._controllers.get(self._side_to_move) == "human":
            # A failed player photo may have happened after the player moved.
            # The branch below must still infer and validate exactly one move.
            requested_side = self._other_side(self._side_to_move)
        if persisted_match is not None and requested_side != self._side_to_move:
            raise VerificationError("盤面沒有新增著法，但所選回合與保存歷史不符；請依保存回合續局。", result.board)
        if self._controllers.get(requested_side) == "human":
            if requested_side != self._side_to_move:
                raise VerificationError("不能略過保存的引擎回合；請依保存回合續局。", result.board)
            if persisted_match is None:
                difference_count = sum(
                    expected_piece != observed_piece
                    for expected_row, observed_row in zip(self._board, result.board)
                    for expected_piece, observed_piece in zip(expected_row, observed_row)
                )
                raise VerificationError(
                    f"實體盤面與上一局保存盤面有 {difference_count} 格差異，不能當作同一局續接。"
                    "若要保留現場盤面，請改選「從目前棋盤開始」。",
                    result.board,
                )
            self._log_temporal_repairs("恢復玩家回合", persisted_match[1])
            self._enter_player_turn("上一局盤面核對完成，輪到玩家。")
            self._persist_session()
            return

        if persisted_match is not None:
            self._log_temporal_repairs("恢復 AI 回合", persisted_match[1])
            self._perform_ai_turn()
            return

        moved_side = self._side_to_move
        if self._controllers.get(moved_side) != "human" or requested_side != self._other_side(moved_side):
            raise VerificationError("引擎回合的實體盤面與保存盤面不一致，禁止自動動作。", result.board)
        try:
            move = find_player_move(self._board, result.board, moved_side)
            observed = result.board
            repaired: list[Position] = []
        except ValueError:
            reconciled = self._reconcile_player_observation(
                self._board, result.board, result.confidence, moved_side
            )
            if reconciled is None:
                raise VerificationError(
                    "無法從保存盤面判定唯一且安全的玩家著法，不能讓 AI 繼續。"
                    "若現場盤面才是正確基準，請改選「從目前棋盤開始」。",
                    result.board,
                )
            move, observed, repaired = reconciled
        self._log_temporal_repairs("恢復時提交玩家著法", repaired)
        legal = self._query_engine("is_legal",
            self._board, moved_side, move, **self._engine_context()
        )
        with self._lock:
            self._guard_running()
            if not legal:
                raise VerificationError("恢復時辨識到的玩家著法不合法，禁止 AI 動作。", result.board)
            before = clone_board(self._board)
            self._board = clone_board(observed)
            self._side_to_move = self._other_side(moved_side)
            self._update_running(lambda state: state.update(last_move=self._move_payload(
                "player", move, before, self._board,
                side=moved_side,
                repaired_cells=repaired,
            ), side_to_move=self._side_to_move))
            self._persist_session()
        self._perform_ai_turn()

    def _player_done(self) -> None:
        if self._board is None:
            raise RuntimeError("尚未建立初始盤面。")
        side = self._side_to_move
        if self._controllers.get(side) != "human":
            raise RuntimeError("目前不是人類控制的回合。")
        turn_started = time.monotonic()
        result = self._capture("玩家走子")
        observed = result.board
        repaired: list[Position] = []
        try:
            move = find_player_move(self._board, observed, side)
        except ValueError as original_error:
            reconciled = self._reconcile_player_observation(
                self._board, observed, result.confidence, side
            )
            if reconciled is None:
                raise original_error
            move, observed, repaired = reconciled
            self._log_temporal_repairs("玩家走子", repaired)
        self._update_running(lambda state: state.update(
            phase=Phase.AI_THINKING.value,
            current_turn="ai",
            message="驗證玩家著法是否合法…",
        ))
        legality_started = time.monotonic()
        legal = self._query_engine("is_legal",
            self._board, side, move, **self._engine_context()
        )
        legality_seconds = round(time.monotonic() - legality_started, 3)
        with self._lock:
            self._guard_running()
            if not legal:
                raise ValueError("玩家著法不符合中國象棋規則。")
            before = clone_board(self._board)
            self._board = clone_board(observed)
            self._update_running(lambda state: state.update(last_move=self._move_payload(
                "player", move, before, self._board,
                side=side,
                repaired_cells=repaired,
                extra={
                    "legality_seconds": legality_seconds,
                    "turn_seconds": round(time.monotonic() - turn_started, 3),
                },
            )))
            self._side_to_move = self._other_side(side)
            self._update_running(lambda state: state.update(side_to_move=self._side_to_move))
            self._persist_session()
        self._advance_to_current_controller()

    def _hint(self, side: str) -> None:
        self._guard_running()
        if self._board is None:
            raise RuntimeError("尚未建立盤面，不能取得提示。")
        if side != self._side_to_move or self._controllers.get(side) != "human":
            raise RuntimeError("提示方與目前人類控制的顏色不一致。")
        analysis = self._query_engine("analyze",
            clone_board(self._board), side, multipv=3, **self._engine_context()
        )
        self._update_running(lambda state: state["analysis"].update(
            status="ready",
            side=side,
            bestmove=self._serialize_move(analysis.bestmove),
            ponder=analysis.ponder,
            lines=[self._serialize_analysis_line(line, side) for line in analysis.lines],
            updated_at=utc_now(),
            error=None,
        ))

    def _advance_to_current_controller(self) -> None:
        self._guard_running()
        side = self._side_to_move
        controller = self._controllers.get(side)
        if controller == "engine":
            self._perform_ai_turn()
            return
        if controller != "human":
            raise RuntimeError("目前回合沒有有效的控制者。")
        if not self._query_engine("has_legal_move",
            self._board, side, **self._engine_context()
        ):
            winner_side = self._other_side(side)
            message = (
                "玩家無合法著法，AI 獲勝。"
                if self._controllers.get(winner_side) == "engine"
                else f"{self._side_label(side)}無合法著法，{self._side_label(winner_side)}獲勝。"
            )
            self._update_running(lambda state: state.update(
                phase=Phase.FINISHED.value,
                current_turn=None,
                side_to_move=side,
                message=message,
                error=None,
            ))
            self._persist_session()
            return
        self._enter_player_turn(f"輪到{self._side_label(side)}走子。")
        self._persist_session()

    def _perform_ai_turn(self) -> None:
        self._guard_running()
        if self._board is None:
            raise RuntimeError("尚未建立盤面。")
        side = self._side_to_move
        if self._controllers.get(side) != "engine":
            raise RuntimeError("目前不是引擎控制的回合。")
        self._update_running(lambda state: state.update(
            phase=Phase.AI_THINKING.value,
            current_turn="ai",
            side_to_move=side,
            message=f"Pikafish 正在替{self._side_label(side)}計算…",
        ))
        # Persist the turn transition before invoking an external process. A
        # crash or Pikafish failure must not leave the saved turn as "player".
        self._persist_session()
        engine_started = time.monotonic()
        context = self._engine_context()
        move = self._query_engine("suggest", self._board, side, **context)
        self._guard_running()
        self._pending_metrics = {
            "engine_seconds": round(time.monotonic() - engine_started, 3),
            "turn_started_at": utc_now(),
            "move_attempt_id": f"{self._game_id}:{len(self._move_history) + 1}",
        }
        if move is None:
            winner_side = self._other_side(side)
            message = (
                "AI 無合法著法，玩家獲勝。"
                if self._controllers.get(winner_side) == "human"
                else f"{self._side_label(side)}無合法著法，{self._side_label(winner_side)}獲勝。"
            )
            self._update_running(lambda state: state.update(
                phase=Phase.FINISHED.value,
                current_turn=None,
                message=message,
            ))
            self._persist_session()
            return
        legality_started = time.monotonic()
        legal = self._query_engine("is_legal", self._board, side, move, **context)
        self._pending_metrics["legality_seconds"] = round(time.monotonic() - legality_started, 3)
        with self._lock:
            self._guard_running()
            if not legal:
                raise RuntimeError("Pikafish 回傳的著法未通過目前局面的二次合法性驗證。")
            self._pending_move = move
            self._pending_before = clone_board(self._board)
            self._pending_expected = apply_move(self._board, move)
            self._last_observed = None
            self._pending_retry_count = 0
            self._persist_session()
            if self._execution_mode == "step":
                self._update_running(lambda state: state.update(
                    phase=Phase.ENGINE_READY.value,
                    current_turn="ai",
                    side_to_move=side,
                    message=(
                        f"{self._side_label(side)}引擎著法 {move.uci} 已算好；"
                        "確認現場安全後再執行。"
                    ),
                ))
                self._persist_session()
                return
        self._execute_pending()

    def _execute_pending(self, *, retry_pick: bool = False) -> None:
        self._guard_running()
        assert self._pending_move is not None
        self._update_running(lambda state: state.update(
            phase=Phase.ROBOT_MOVING.value, current_turn="ai", message="機械手臂正在執行 AI 著法…",
        ))
        self._update_running(lambda state: state["robot"].update(
            motion_may_continue=True,
            current_action="準備執行 AI 棋步",
            next_action="完成後提交棋步",
        ))
        self._persist_session()
        motion_started_at = utc_now()
        motion_started = time.monotonic()
        motion_record = self._new_motion_record(retry_pick, motion_started_at)
        self._motion_history.append(motion_record)
        self._pending_metrics["motion_sequence"] = motion_record["sequence"]
        self._persist_session()
        try:
            if retry_pick:
                robot_report = self.robot.execute_move(
                    self._pending_move,
                    self._stop_event,
                    source_pick_offset_mm=self.config.motion.retry_pick_z_offset_mm,
                )
            else:
                robot_report = self.robot.execute_move(self._pending_move, self._stop_event)
        except Exception as exc:
            motion_record.update(
                status="failed",
                completed_at=utc_now(),
                duration_seconds=round(time.monotonic() - motion_started, 3),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            self._complete_motion_record(motion_record)
            self._pending_metrics["robot_seconds"] = motion_record["duration_seconds"]
            raise
        motion_record.update(
            status="completed",
            completed_at=utc_now(),
            duration_seconds=round(time.monotonic() - motion_started, 3),
        )
        if isinstance(robot_report, dict):
            motion_record.update(robot_report)
        self._complete_motion_record(motion_record)
        self._pending_metrics["robot_seconds"] = motion_record["duration_seconds"]
        self._pending_metrics["motion_sequence"] = motion_record["sequence"]
        self._guard_running()
        self._update_running(lambda state: state["robot"].update(
            motion_may_continue=False,
            current_action="AI 棋步低階動作完成",
            next_action="提交棋步並切換回合",
        ))
        self._persist_session()
        self._commit_pending_move("AI 落子動作完成，輪到玩家。", confirmation_method="motion_completed")

    def _retry(self) -> None:
        self._guard_running()
        if self._pending_move is None or self._pending_expected is None or self._pending_before is None:
            raise RuntimeError("沒有保留中的 AI 著法。")
        # Re-observe first: blindly replaying a possibly completed move can pick an empty square.
        self._last_observed = None
        result = self._capture("重試前盤面確認")
        self._last_observed = clone_board(result.board)
        protected = {self._pending_move.source, self._pending_move.destination}
        expected_match = self._match_expected_board(
            self._pending_expected, result.board, result.confidence, protected
        )
        before_match = self._match_expected_board(
            self._pending_before, result.board, result.confidence, protected
        )
        if expected_match is not None and before_match is None:
            self._log_temporal_repairs("重試後 AI 落子驗證", expected_match[1])
            self._commit_pending_move("重新拍攝後確認 AI 著法已完成，輪到玩家。")
            return
        if before_match is None or expected_match is not None:
            raise VerificationError(
                "盤面與落子前、預期盤面皆不一致。請停止流程，從現場盤面重新建立對局。",
                result.board,
            )
        self._log_temporal_repairs("重試前盤面", before_match[1])
        self._pending_retry_count = 1
        self._persist_session()
        self._execute_pending(retry_pick=True)

    def _commit_pending_move(self, player_message: str, *, confirmation_method: str = "vision") -> None:
        """Commit an AI move after completed motion or explicit recovery observation."""
        with self._lock:
            self._guard_running()
            assert self._pending_move is not None and self._pending_expected is not None
            self._pending_metrics["confirmation_method"] = confirmation_method
            move_side = self._side_to_move
            move = self._pending_move
            before = clone_board(self._pending_before or self._board)
            after = clone_board(self._pending_expected)
            self._board = clone_board(self._pending_expected)
            metrics = dict(self._pending_metrics)
            started_at = metrics.get("turn_started_at")
            if started_at:
                metrics["turn_seconds"] = round((datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds()
                                                + metrics.get("engine_seconds", 0), 3)
            sequence = len(self._move_history) + 1
            for motion in self._motion_history:
                if motion.get("sequence") == metrics.get("motion_sequence"):
                    motion.update(verification_status="skipped" if confirmation_method == "motion_completed" else "confirmed",
                                  committed_move_sequence=sequence,
                                  confirmation_method=confirmation_method,
                                  verification_capture_id=None if confirmation_method == "motion_completed" else (self._last_capture_record or {}).get("capture_id"))
            self._pending_move = self._pending_expected = self._pending_before = self._last_observed = None
            self._pending_retry_count = 0
            self._pending_metrics = {}
            self._update_running(lambda state: state.update(
                last_move=self._move_payload(
                    "ai", move, before, after,
                    side=move_side,
                    extra=metrics,
                ),
                current_turn=None,
                message="AI 著法已提交，正在判斷對局是否結束…",
            ))
            self._side_to_move = self._other_side(move_side)
            self._update_running(lambda state: state.update(side_to_move=self._side_to_move))
            # Persist the committed board before another external-engine call.
            self._persist_session()
        if self._controllers.get(self._side_to_move) == "human":
            if not self._query_engine("has_legal_move",
                self._board, self._side_to_move, **self._engine_context()
            ):
                self._update_running(lambda state: state.update(
                    phase=Phase.FINISHED.value,
                    current_turn=None,
                    message="玩家無合法著法，AI 獲勝。",
                    error=None,
                ))
                self._persist_session()
                return
            self._enter_player_turn(player_message)
            self._persist_session()
            return
        self._perform_ai_turn()

    def _match_expected_board(
        self,
        expected: Board,
        observed: Board,
        confidence: list[list[float | None]] | None,
        protected: set[Position] | None = None,
    ) -> tuple[Board, list[Position]] | None:
        """Match one observation without hiding physical board changes.

        Only a low-confidence, same-color class change on a cell that should
        remain occupied may be restored. Empty/missing/extra pieces and move
        endpoints are hard failures.
        """
        corrected = clone_board(observed)
        repaired: list[Position] = []
        protected = protected or set()
        if (
            protected and self._pending_before is not None
            and self._pending_expected is not None and expected == self._pending_expected
        ):
            reconciled = self._reconcile_player_observation(
                self._pending_before, observed, confidence, self._side_to_move
            )
            if reconciled is not None and reconciled[1] == expected:
                return reconciled[1], reconciled[2]
        for row in range(10):
            for col in range(9):
                if corrected[row][col] == expected[row][col]:
                    continue
                position = (col, row)
                expected_piece = expected[row][col]
                observed_piece = corrected[row][col]
                score = confidence[row][col] if confidence is not None else None
                repairable = (
                    position not in protected
                    and expected_piece != 0
                    and observed_piece != 0
                    and is_red(expected_piece) == is_red(observed_piece)
                    and score is not None
                    and score <= self.config.vision.temporal_repair_max_confidence
                )
                if not repairable:
                    return None
                corrected[row][col] = expected_piece
                repaired.append(position)
                if len(repaired) > self.config.vision.temporal_repair_max_cells:
                    return None
        return corrected, repaired

    def _reconcile_player_observation(
        self,
        before: Board,
        observed: Board,
        confidence: list[list[float | None]] | None,
        side: str,
    ) -> tuple[Move, Board, list[Position]] | None:
        """Find one unique player move, then repair unrelated label noise."""
        expected_red = side == "w"
        sources = [
            (col, row)
            for row in range(10)
            for col in range(9)
            if before[row][col]
            and is_red(before[row][col]) == expected_red
            and observed[row][col] == 0
        ]
        matches: list[tuple[Move, Board, list[Position]]] = []
        for source in sources:
            sx, sy = source
            moved_piece = before[sy][sx]
            destinations = [
                (col, row)
                for row in range(10)
                for col in range(9)
                if (col, row) != source
                and observed[row][col] == moved_piece
                and before[row][col] != moved_piece
            ]
            for destination in destinations:
                dx, dy = destination
                captured = before[dy][dx]
                move = Move(
                    source,
                    destination,
                    captured,
                    position_to_uci(source) + position_to_uci(destination),
                )
                try:
                    expected = apply_move(before, move)
                except ValueError:
                    continue
                corrected = expected
                repaired = [
                    (col, row) for row in range(10) for col in range(9)
                    if observed[row][col] != expected[row][col]
                    and (col, row) not in {source, destination}
                ]
                try:
                    validate_board(corrected)
                    confirmed_move = find_player_move(before, corrected, side)
                except ValueError:
                    continue
                matches.append((confirmed_move, corrected, repaired))
        return matches[0] if len(matches) == 1 else None

    def _log_temporal_repairs(self, context: str, repaired: list[Position]) -> None:
        if repaired:
            if self._last_capture_record is not None:
                observed = self._last_capture_record.get("observed_board")
                details = []
                for col, row in repaired:
                    details.append({
                        "cell": position_to_uci((col, row)),
                        "observed": observed[row][col] if observed is not None else None,
                        "restored": self._board[row][col] if self._board is not None else None,
                    })
                self._last_capture_record["history_repairs"] = details
                self._last_capture_record["repair_context"] = context
                self._last_capture_record["status"] = "reconciled"
            LOGGER.warning(
                "[VISION] restored unrelated cells from committed board (%s): %s",
                context,
                [position_to_uci(position) for position in repaired],
            )

    def _position_details(self, position: Position) -> dict[str, Any]:
        col, row = position
        player_row = 9 - row
        player_col = 8 - col
        point_offset = player_col if player_row % 2 == 0 else 8 - player_col
        x, y = self.config.motion.board_xy_matrix()[row][col]
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

    def _move_payload(
        self,
        actor: str,
        move: Move,
        before: Board,
        after: Board,
        *,
        side: str | None = None,
        repaired_cells: list[Position] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        side = side or (self._player_side if actor == "player" else self._ai_side)
        other_side = "b" if side == "w" else "w"
        sequence = len(self._move_history) + 1
        image_verified = (extra or {}).get("confirmation_method") != "motion_completed"
        capture = (self._last_capture_record or {}) if image_verified else {}
        fen_error = None
        try:
            fen_before = board_to_fen(before, side)
            fen_after = board_to_fen(after, other_side)
        except ValueError as exc:
            # Operational recording must never turn a pre-existing board
            # validation issue into a new robot-workflow failure.
            fen_before = fen_after = None
            fen_error = str(exc)
        payload = {
            "actor": actor,
            "side": side,
            "round": (sequence + 1) // 2,
            "uci": move.uci,
            "source": list(move.source),
            "destination": list(move.destination),
            "source_position": self._position_details(move.source),
            "destination_position": self._position_details(move.destination),
            "captured": bool(move.captured_piece),
            "captured_piece_id": move.captured_piece,
            "fen_before": fen_before,
            "fen_after": fen_after,
            "fen_error": fen_error,
            "capture_id": capture.get("capture_id"),
            "capture_seconds": capture.get("capture_seconds"),
            "vision_processing_seconds": capture.get("processing_seconds"),
            "vision_total_seconds": capture.get("total_seconds"),
            "vision_repaired_cells": [
                position_to_uci(item) for item in (repaired_cells or [])
            ],
            "committed_at": utc_now(),
        }
        if extra:
            payload.update(extra)
            if "repaired_cells" in extra and not payload["vision_repaired_cells"]:
                payload["vision_repaired_cells"] = list(extra["repaired_cells"])
        if payload.get("legality_seconds") is None:
            check = next((q for q in reversed(self._engine_history)
                          if q.get("operation") == "is_legal" and q.get("requested_move") == move.uci
                          and q.get("status") == "completed"), {})
            payload["legality_seconds"] = check.get("duration_seconds")
        if actor == "player" and payload.get("turn_seconds") is None and capture.get("started_at"):
            payload["turn_seconds"] = round((datetime.now(timezone.utc) - datetime.fromisoformat(capture["started_at"])).total_seconds(), 3)
        payload.setdefault("move_attempt_id", f"{self._game_id}:{sequence}")
        if image_verified and self._last_capture_record is not None:
            self._last_capture_record.update(accepted_board=clone_board(after), accepted_move_sequence=sequence,
                                             accepted_uci=move.uci, acceptance_status="committed")
        return payload

    def _new_motion_record(self, retry_pick: bool, started_at: str) -> dict[str, Any]:
        assert self._pending_move is not None
        source = self._position_details(self._pending_move.source)
        destination = self._position_details(self._pending_move.destination)
        return {
            "sequence": len(self._motion_history) + 1,
            "round": self._current_round(),
            "move_uci": self._pending_move.uci,
            "move_attempt_id": self._pending_metrics.get("move_attempt_id"),
            "verification_status": "pending", "committed_move_sequence": None,
            "retry": retry_pick,
            "status": "moving",
            "started_at": started_at,
            "completed_at": None,
            "duration_seconds": None,
            "source": source,
            "destination": destination,
            "target_x": destination["robot_x"],
            "target_y": destination["robot_y"],
            "target_z": self.config.motion.z_safe,
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

    def _complete_motion_record(self, record: dict[str, Any]) -> None:
        # Successful adapters report the landing pose before their standby return.
        if record.get("position_error_mm") is not None:
            return
        pose = self.store.snapshot().get("robot", {}).get("pose")
        if not isinstance(pose, dict):
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
        orientation = self.config.motion.orientation_rxyz
        if orientation is not None and all(isinstance(pose.get(key), (int, float)) for key in ("rx", "ry", "rz")):
            errors = [
                abs((float(pose[key]) - target_angle + 180.0) % 360.0 - 180.0)
                for key, target_angle in zip(("rx", "ry", "rz"), orientation)
            ]
            record["orientation_error_deg"] = round(max(errors), 4)

    def _enter_player_turn(self, message: str) -> None:
        self._update_running(lambda state: state.update(
            phase=Phase.PLAYER_TURN.value,
            current_turn="player",
            side_to_move=self._side_to_move,
            message=message,
            error=None,
        ))

    def _update_running(self, mutator: Callable[[dict[str, Any]], None]) -> None:
        """Accept a workflow result atomically against a concurrent stop."""
        with self._lock:
            self._guard_running()
            self.store.update(mutator)

    def _handle_error(self, exc: Exception) -> None:
        with self._lock:
            if (self._stop_event.is_set() or isinstance(exc, StopRequested)) and not isinstance(exc, GripperStateUnknown):
                return
            before = self.store.snapshot()
            motion_uncertain = bool(before["robot"].get("motion_may_continue")) or isinstance(exc, GripperStateUnknown) or (
                before["phase"] == Phase.ROBOT_MOVING.value
                and isinstance(exc, (ConnectionError, TimeoutError))
            )
            retryable = isinstance(exc, (VerificationError, BoardObservationError)) and self._pending_move is not None
            if isinstance(exc, VerificationError) and exc.observed is not None:
                self._last_observed = clone_board(exc.observed)
            if isinstance(exc, GripperStateUnknown):
                code = "gripper_state_unknown"
            elif motion_uncertain:
                code = "motion_state_unknown"
            else:
                code = "verification_failed" if retryable else "workflow_failed"
            self.store.update(lambda state: (
                state.update(
                    phase=Phase.ERROR.value,
                    message=str(exc),
                    error={"code": code, "message": str(exc), "retryable": retryable, "type": type(exc).__name__},
                ),
                state["vision"].update(status="error"),
                state["robot"].update(
                    next_action=(
                        "確認 Robot 完全停止與吸盤狀態後重新啟動系統"
                        if motion_uncertain else
                        ("中斷棋步待恢復：確認盤面後重試" if retryable else "檢查錯誤後繼續上一局")
                    ),
                    motion_may_continue=motion_uncertain,
                ),
            ))
        try:
            self._persist_session()
        except Exception as persist_error:
            LOGGER.critical(
                "[SESSION] failed to persist workflow error: %s",
                persist_error,
                exc_info=True,
            )
            self.store.update(lambda state: state.update(
                message=f"{exc}；對局狀態保存失敗：{persist_error}",
                error={
                    "code": "state_persistence_failed",
                    "message": f"對局狀態保存失敗：{persist_error}",
                    "retryable": False,
                    "type": type(persist_error).__name__,
                },
            ))
            return
        if not retryable and not motion_uncertain and self._board is not None:
            self._load_resume_candidate()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._close_event.set()
            self._stop_event.set()
            state = self.get_state()
            physical_command_active = bool(state["robot"].get("motion_may_continue")) or (
                self._busy and state["phase"] == Phase.ROBOT_MOVING.value
            )
            if physical_command_active:
                self.store.update(lambda current: current["robot"].update(
                    motion_may_continue=True,
                    current_action="程式關閉時低階動作狀態未知",
                    next_action="重新啟動後由操作人員確認 Robot 已停止",
                ))
            try:
                self._persist_session()
            except Exception:
                # Shutdown must continue even if the disk is unavailable. The
                # preflight free-space check reduces, but cannot eliminate,
                # this residual risk.
                LOGGER.critical("[SESSION] failed to persist shutdown state", exc_info=True)
        if hasattr(self.vision, "close"):
            self.vision.close()
        preflight_thread = self._preflight_thread
        if (
            preflight_thread is not None
            and preflight_thread is not threading.current_thread()
            and preflight_thread.is_alive()
        ):
            preflight_thread.join(
                timeout=min(
                    max(self.config.motion.listen_ready_timeout_seconds + 1.0, 3.0),
                    20.0,
                )
            )
        preflight_alive = bool(preflight_thread and preflight_thread.is_alive())
        if preflight_alive:
            LOGGER.critical(
                "[PREFLIGHT] monitor did not stop; retaining engine and robot leases until process exit"
            )
        elif hasattr(self.engine, "stop"):
            self.engine.stop()
        self._queue.put(None)
        self._worker.join(timeout=3)
        # Keep the inter-process hardware lease until process exit if the worker
        # is still unwinding a socket call. Releasing it early could let a second
        # process command the same robot concurrently.
        if not self._worker.is_alive() and not preflight_alive and hasattr(self.robot, "close"):
            self.robot.close()
        if self.record_writer is not None:
            self.record_writer.close()
