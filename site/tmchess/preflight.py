"""Static and runtime readiness checks for the physical robot workflow."""

from __future__ import annotations

import shutil
import threading
from typing import Any

from .config import AppConfig
from .errors import CommandConflict
from .state import utc_now


class PreflightChecker:
    """Evaluate configuration, operator sign-off, and runtime dependencies."""

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
    def _check(
        checks: list[dict[str, Any]],
        key: str,
        label: str,
        ok: bool,
        detail: str,
    ) -> None:
        checks.append({"key": key, "label": label, "ok": bool(ok), "detail": detail})

    def disk_space_check(self) -> dict[str, Any]:
        """Return a fresh free-space check without rerunning hardware probes."""
        config = self.config
        disk_target = (
            config.capture_root if config.capture_root.exists() else config.capture_root.parent
        )
        try:
            free_bytes = shutil.disk_usage(disk_target).free
        except OSError as exc:
            return {
                "key": "disk_space",
                "label": "執行資料磁碟空間",
                "ok": False,
                "detail": str(exc),
            }
        return {
            "key": "disk_space",
            "label": "執行資料磁碟空間",
            "ok": free_bytes >= config.minimum_free_disk_bytes,
            "detail": (
                f"可用 {free_bytes / (1024 ** 3):.2f} GiB；"
                f"最低 {config.minimum_free_disk_bytes / (1024 ** 3):.2f} GiB"
            ),
        }

    def run(self, dynamic: bool = True) -> dict[str, Any]:
        config = self.config
        checks: list[dict[str, Any]] = []
        self._check(
            checks,
            "hardware_enabled",
            "真機動作開關",
            config.safety.hardware_enabled,
            "已啟用" if config.safety.hardware_enabled else "HARDWARE_ENABLED 已關閉",
        )
        missing = config.motion.missing_fields()
        self._check(
            checks,
            "coordinates",
            "棋盤、姿態、Z 與落子箱座標",
            not missing,
            "完整" if not missing else "缺少：" + ", ".join(missing),
        )
        safety_flags = {
            "emergency_stop": ("實體急停確認", config.safety.emergency_stop_confirmed),
            "motion_parameters": ("速度與加速度簽核", config.safety.motion_parameters_verified),
            "dual_view_upload": ("V1／V2 即時上傳簽核", config.safety.dual_view_upload_verified),
            "base": ("Base 簽核", config.safety.base_verified),
            "tcp": ("TCP 簽核", config.safety.tcp_verified),
            "base_tcp_runtime_guard": (
                "Base／TCP 啟動時一致性保護",
                config.safety.base_tcp_runtime_guard_verified,
            ),
            "board": ("棋盤 9×10 矩陣簽核", config.safety.board_coordinates_verified),
            "z": ("三段 Z 高度簽核", config.safety.z_heights_verified),
            "graveyard": ("落子箱簽核", config.safety.graveyard_verified),
            "graveyard_capacity": (
                "落子箱滿載容量風險接受",
                config.safety.graveyard_capacity_risk_accepted,
            ),
        }
        for key, (label, ok) in safety_flags.items():
            self._check(checks, key, label, ok, "已確認" if ok else "尚未確認")

        model_ok = config.vision.model_path.is_file()
        engine_ok = config.engine.executable.is_file() and config.engine.nnue.is_file()
        self._check(checks, "model", "YOLO 模型檔", model_ok, str(config.vision.model_path))
        self._check(
            checks,
            "pikafish",
            "Pikafish 與 NNUE",
            engine_ok,
            str(config.engine.executable.parent),
        )

        checks.append(self.disk_space_check())

        static_ready = all(item["ok"] for item in checks)
        if dynamic and static_ready:
            confirmed = self.base_tcp_confirmed()
            self._check(
                checks,
                "base_tcp_runtime_confirmation",
                "本次啟動 Base／TCP 人工確認",
                confirmed,
                (
                    f"已確認 {config.motion.base_name} / {config.motion.tcp_name}"
                    if confirmed
                    else f"請在操作端確認 {config.motion.base_name} / {config.motion.tcp_name}"
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
            self._check(
                checks,
                "runtime",
                "真機動態預檢",
                False,
                "靜態安全條件未完成，未連線",
            )
        return {
            "ready": all(item["ok"] for item in checks),
            "checks": checks,
            "checked_at": utc_now(),
        }
