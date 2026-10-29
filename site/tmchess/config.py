"""Single source of truth for network, vision, engine and robot settings.

Hardware mode is enabled by default. Set ``TMCHESS_HARDWARE_ENABLED=0``
to disable it. Physical motion still requires the runtime safety checks.
"""

from __future__ import annotations

import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple


SITE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SITE_ROOT.parent
HARDWARE_ENABLED = os.getenv("TMCHESS_HARDWARE_ENABLED", "1").strip() == "1"


@dataclass(frozen=True)
class NetworkConfig:
    ui_host: str = "127.0.0.1"
    ui_port: int = 8000
    vision_host: str = "0.0.0.0"
    vision_port: int = 5000
    vision_path: str = "/api/vision/tmvision/detect"
    robot_ip: str = "192.168.1.200"
    robot_listen_port: int = 5890
    max_upload_bytes: int = 12 * 1024 * 1024


@dataclass(frozen=True)
class MotionConfig:
    # RobotBase XY references in player-view order:
    # upper-half top-left (player row 0, col 0), upper-half bottom-right
    # (player row 4, col 8), lower-half top-left (player row 5, col 0),
    # lower-half bottom-right (player row 9, col 8).
    # Screenshot Z values are intentionally excluded; motion uses the independent
    # z_safe / z_approach / z_pick profile below.
    # On 2026-10-20 the operator measured the upper-left intersection at
    # (192.85, -162.24) and confirmed pure translation, with no rotation.
    # Delta (+1.20, +33.94) from the previous (191.65, -196.18) applies to
    # all four references and all 90 cells; spacing and warp remain unchanged.
    board_reference_xy: Optional[Tuple[Tuple[float, float], ...]] = (
        (192.85, -162.24),
        (365.90, 98.95),
        (362.25, -182.89),
        (535.80, 75.80),
    )
    # Operator-specified fixed pose for RobotBase + NOTOOL on 2026-10-12.
    orientation_rxyz: Optional[Tuple[float, float, float]] = (-180.0, 0.0, 80.0)
    # Measured with the installed suction assembly while TMflow uses
    # RobotBase + NOTOOL. Motion remains locked until the missing board
    # geometry and every safety sign-off are completed.
    z_safe: Optional[float] = 420.0
    # First camera/standby position confirmed by the operator on 2026-10-27.
    # RobotBase + NOTOOL; orientation follows orientation_rxyz.
    standby_xyz: Tuple[float, float, float] = (330.08, 80.60, 578.92)
    z_approach: Optional[float] = 355.0
    # Fixed on-site board contact height confirmed on 2026-10-13.  Both normal
    # moves and operator-requested retries use this same absolute Z value.
    z_pick: Optional[float] = 288.0
    board_surface_z: Optional[float] = 280.0
    piece_height_mm: Optional[float] = 10.3
    # Z=288 is 2.3 mm below the nominal 290.3 mm piece top.  This tolerance
    # records that deliberate on-site suction contact calibration.
    piece_height_tolerance_mm: float = 2.5
    graveyard_xy: Optional[Tuple[float, float]] = (128.0, -265.0)
    graveyard_z_safe: Optional[float] = 525.0
    graveyard_z_approach: Optional[float] = 350.0
    graveyard_z_release: Optional[float] = 340.0
    graveyard_opening_xy: Optional[Tuple[float, float]] = (110.0, 110.0)
    graveyard_scatter_radius_mm: Optional[float] = 35.0
    graveyard_payload_diameter_mm: Optional[float] = 27.0
    graveyard_edge_margin_mm: float = 5.0
    base_name: Optional[str] = "RobotBase"
    tcp_name: Optional[str] = "NOTOOL"
    gripper_do_device: str = "ControlBox"
    gripper_do_pin: int = 3
    speed: int = 50
    acceleration: int = 200
    blend: int = 0
    precise: str = "false"
    position_tolerance_mm: float = 1.0
    orientation_tolerance_deg: float = 1.0
    stable_delta_mm: float = 0.10
    orientation_stable_delta_deg: float = 0.10
    stable_samples: int = 3
    move_timeout_seconds: float = 30.0
    socket_timeout_seconds: float = 5.0
    listen_ready_timeout_seconds: float = 12.0
    listen_retry_seconds: float = 0.25
    vacuum_settle_seconds: float = 0.4
    release_settle_seconds: float = 0.2
    pose_poll_seconds: float = 0.30
    # Keep retries at the same fixed board contact height; never descend again
    # after a failed visual verification.
    retry_pick_z_offset_mm: float = 0.0

    def player_board_xy_matrix(self) -> Tuple[Tuple[Tuple[float, float], ...], ...]:
        """Return the player-view 9-column by 10-row row-major XY matrix.

        The four measured references constrain a smooth bilinear grid.  The
        matrix hits every supplied reference exactly while allowing the left
        and right row directions to differ slightly.  Player row 0 is the far
        black edge and player column 0 is the player's left edge.
        """
        references = self.board_reference_xy
        if references is None or len(references) != 4:
            raise RuntimeError("棋盤上下半區四個參考座標尚未設定。")
        upper_left, upper_bottom_right, lower_left, lower_bottom_right = references

        left_row_step = tuple((lower_left[i] - upper_left[i]) / 5.0 for i in range(2))
        right_row_step = tuple((lower_bottom_right[i] - upper_bottom_right[i]) / 5.0 for i in range(2))
        row_warp_per_col = tuple((right_row_step[i] - left_row_step[i]) / 8.0 for i in range(2))
        top_col_step = tuple(
            (upper_bottom_right[i] - upper_left[i] - 4.0 * right_row_step[i]) / 8.0
            for i in range(2)
        )

        return tuple(
            tuple(
                (
                    upper_left[0]
                    + row * left_row_step[0]
                    + col * top_col_step[0]
                    + row * col * row_warp_per_col[0],
                    upper_left[1]
                    + row * left_row_step[1]
                    + col * top_col_step[1]
                    + row * col * row_warp_per_col[1],
                )
                for col in range(9)
            )
            for row in range(10)
        )

    def board_xy_matrix(self) -> Tuple[Tuple[Tuple[float, float], ...], ...]:
        """Return XY indexed by the domain convention ``matrix[row][col]``.

        The board domain and Pikafish use row 0 at Red's home rank.  The
        full-board camera calibration is rotated 180 degrees relative to the
        supplied player view, so both row and column are reversed here.
        """
        player_matrix = self.player_board_xy_matrix()
        return tuple(
            tuple(player_matrix[9 - row][8 - col] for col in range(9))
            for row in range(10)
        )

    def missing_fields(self) -> list[str]:
        required = {
            "棋盤上下半區參考 XY": self.board_reference_xy,
            "姿態 RX/RY/RZ": self.orientation_rxyz,
            "安全高度 Z_SAFE": self.z_safe,
            "接近高度 Z_APPROACH": self.z_approach,
            "取放高度 Z_PICK": self.z_pick,
            "棋盤表面 Z": self.board_surface_z,
            "棋子高度": self.piece_height_mm,
            "落子箱 XY": self.graveyard_xy,
            "落子箱安全高度": self.graveyard_z_safe,
            "落子箱接近高度": self.graveyard_z_approach,
            "落子箱釋放高度": self.graveyard_z_release,
            "落子箱淨開口": self.graveyard_opening_xy,
            "落子箱散布半徑": self.graveyard_scatter_radius_mm,
            "落子棋子直徑": self.graveyard_payload_diameter_mm,
            "Base 名稱": self.base_name,
            "TCP 名稱": self.tcp_name,
        }
        missing = [name for name, value in required.items() if value is None]
        if (
            len(self.standby_xyz) != 3
            or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in self.standby_xyz)
            or self.z_safe is not None and self.standby_xyz[2] < self.z_safe
        ):
            missing.append("第一拍照安全點須為有效 XYZ，且高度不得低於 Z_SAFE")
        if self.board_reference_xy is not None and len(self.board_reference_xy) != 4:
            missing.append("board_reference_xy(必須四點)")
        if all(value is not None for value in (self.z_safe, self.z_approach, self.z_pick)):
            if not self.z_safe > self.z_approach > self.z_pick:  # type: ignore[operator]
                missing.append("z_safe > z_approach > z_pick")
        if all(value is not None for value in (self.z_pick, self.board_surface_z, self.piece_height_mm)):
            measured_height = self.z_pick - self.board_surface_z  # type: ignore[operator]
            if abs(measured_height - self.piece_height_mm) > self.piece_height_tolerance_mm:  # type: ignore[operator]
                missing.append("Z_PICK 與棋盤表面的差值不符合棋子高度")
        if self.position_tolerance_mm <= 0 or self.orientation_tolerance_deg <= 0:
            missing.append("位置與姿態到點容差必須大於 0")
        if self.stable_delta_mm < 0 or self.orientation_stable_delta_deg < 0:
            missing.append("位置與姿態穩定變化上限不得小於 0")
        if self.stable_samples <= 0:
            missing.append("到點穩定樣本數必須大於 0")
        if not -2.0 <= self.retry_pick_z_offset_mm <= 0.0:
            missing.append("重新取棋高度修正必須介於 -2 mm 與 0 mm")
        if (
            self.z_pick is not None
            and self.board_surface_z is not None
            and self.z_pick + self.retry_pick_z_offset_mm <= self.board_surface_z
        ):
            missing.append("重新取棋高度不得低於棋盤表面")
        if self.listen_ready_timeout_seconds <= 0 or self.listen_retry_seconds < 0:
            missing.append("Listen 等待逾時必須大於 0，重試間隔不得小於 0")
        graveyard_z = (self.graveyard_z_safe, self.graveyard_z_approach, self.graveyard_z_release)
        if all(value is not None for value in graveyard_z):
            if not self.graveyard_z_safe > self.graveyard_z_approach > self.graveyard_z_release:  # type: ignore[operator]
                missing.append("graveyard_z_safe > graveyard_z_approach > graveyard_z_release")
        if (
            self.graveyard_opening_xy is not None
            and self.graveyard_scatter_radius_mm is not None
            and self.graveyard_payload_diameter_mm is not None
        ):
            width, height = self.graveyard_opening_xy
            scatter_radius = self.graveyard_scatter_radius_mm
            diameter = self.graveyard_payload_diameter_mm
            margin = self.graveyard_edge_margin_mm
            if min(width, height, diameter) <= 0 or min(scatter_radius, margin) < 0:
                missing.append("落子箱尺寸、棋子直徑與安全裕量必須為有效正值")
            if 2 * scatter_radius + diameter + 2 * margin > min(width, height):
                missing.append("落子箱散布半徑超出淨開口")
        return missing


@dataclass(frozen=True)
class VisionConfig:
    model_path: Path = PROJECT_ROOT / "best.pt"
    confidence: float = 0.25
    # Used only to obtain alternate labels for a cell when the primary board
    # violates hard Xiangqi piece-count limits. It never fills an empty cell.
    fallback_confidence: float = 0.10
    # A changed label on an otherwise unchanged, occupied cell may be restored
    # from the previous committed board only after one unique move is found.
    # Missing pieces, extra pieces, move endpoints, and cross-color changes are
    # never repaired by this threshold.
    temporal_repair_max_confidence: float = 0.35
    # Never let temporal correction hide several simultaneous classification
    # changes. One unrelated low-confidence occupied cell is the accepted limit.
    temporal_repair_max_cells: int = 1
    # Applied independently to V1 and V2. TMflow may move between viewpoints at
    # a deliberately low speed, so a single shared deadline is not appropriate.
    image_timeout_seconds: float = 30.0
    # The model runs in an isolated process. These are wall-clock limits, so a
    # native runtime deadlock can be terminated instead of blocking the game.
    model_load_timeout_seconds: float = 60.0
    inference_timeout_seconds: float = 60.0
    v1_reference: Tuple[int, int] = (2048, 1536)
    v1_col_x: Tuple[float, ...] = (807.5, 944.0, 1081.0, 1218.0, 1355.5, 1492.0, 1629.0, 1764.5, 1901.0)
    v1_row_y: Tuple[float, ...] = (155.0, 295.5, 433.5, 571.5, 709.5, 832.0, 969.0, 1104.0, 1242.0, 1380.0)
    v1_max_distance: float = 105.0
    v2_reference: Tuple[int, int] = (2592, 1944)
    # Recalibrated from visible grid lines in the 2026-10-20 V2 captures
    # after board translation; local rows correspond to main rows 1..4.
    v2_col_x: Tuple[float, ...] = (1030.0, 1287.0, 1545.0, 1805.0)
    v2_row_y: Tuple[float, ...] = (1037.0, 1295.0, 1547.0, 1800.0)
    v2_max_dx: float = 125.0
    v2_max_dy: float = 125.0


@dataclass(frozen=True)
class EngineConfig:
    executable: Path = PROJECT_ROOT / "engine_bin" / "pikafish.exe"
    nnue: Path = PROJECT_ROOT / "engine_bin" / "pikafish.nnue"
    movetime_ms: int = 500
    depth: int = 2
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class SafetyConfig:
    hardware_enabled: bool = HARDWARE_ENABLED
    # User-provided on-site sign-offs recorded on 2026-10-10.  These do not
    # replace the dynamic startup probes or unlock hardware by themselves.
    emergency_stop_confirmed: bool = True
    motion_parameters_verified: bool = True
    dual_view_upload_verified: bool = True
    base_verified: bool = True
    tcp_verified: bool = True
    # The website now requires an operator confirmation of the visible TMflow
    # Base/TCP names on every process start. TMflow 1.82.51 has no verified
    # programmatic query in this project, so the confirmation remains human.
    base_tcp_runtime_guard_verified: bool = True
    # Operator confirmed the translated board coordinates on 2026-10-20.
    # This is a human sign-off, not a new automated 90-point measurement.
    board_coordinates_verified: bool = True
    z_heights_verified: bool = True
    graveyard_verified: bool = True
    # The operator explicitly accepted on 2026-10-13 that full-box pile height
    # will not be tested.  This is a recorded risk acceptance, not evidence
    # that worst-case capacity has been physically verified.
    graveyard_capacity_risk_accepted: bool = True


@dataclass(frozen=True)
class AppConfig:
    network: NetworkConfig = field(default_factory=NetworkConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    # One folder is created per game. Captures and game.json live below it.
    capture_root: Path = SITE_ROOT / "runtime" / "games"
    # Human-readable records stay shallow and separate from capture internals.
    record_root: Path = SITE_ROOT / "對局紀錄"
    minimum_free_disk_bytes: int = 1024 * 1024 * 1024


CONFIG = AppConfig()
