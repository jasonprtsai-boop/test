"""Explicit, operator-supervised traversal of all 90 board intersections."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .config import MotionConfig
from .robot import Pose, TMRobotAdapter


@dataclass(frozen=True)
class InspectionProfile:
    travel_z: float = 380.0
    approach_z: float = 340.0
    inspection_z: float = 300.0
    dwell_seconds: float = 3.0
    travel_speed: int = 20
    approach_speed: int = 10
    inspection_speed: int = 5
    acceleration: int = 100

    def validate(self, motion: MotionConfig, *, allow_below_safe_z: bool) -> None:
        if not self.travel_z > self.approach_z > self.inspection_z:
            raise ValueError("高度必須符合 travel_z > approach_z > inspection_z。")
        if motion.board_surface_z is not None and self.inspection_z <= motion.board_surface_z:
            raise ValueError("inspection_z 必須高於棋盤表面。")
        if (
            motion.board_surface_z is not None
            and motion.piece_height_mm is not None
            and self.inspection_z
            <= motion.board_surface_z + motion.piece_height_mm + motion.piece_height_tolerance_mm
        ):
            raise ValueError("inspection_z 必須高於單層棋子頂面並保留高度容差。")
        if min(self.travel_speed, self.approach_speed, self.inspection_speed, self.acceleration) <= 0:
            raise ValueError("速度與加速度必須大於 0。")
        if not self.inspection_speed < self.approach_speed <= self.travel_speed:
            raise ValueError("下降速度必須最低，並符合 inspection < approach <= travel。")
        if self.dwell_seconds < 0:
            raise ValueError("停留秒數不可為負值。")
        if motion.z_safe is not None and self.travel_z < motion.z_safe and not allow_below_safe_z:
            raise ValueError(
                f"指定跨點高度 Z={self.travel_z:g} 低於設定 Z_SAFE={motion.z_safe:g}；"
                "真機執行必須明確允許。"
            )


@dataclass(frozen=True)
class InspectionPoint:
    number: int
    player_row: int
    player_col: int
    x: float
    y: float

    @property
    def label(self) -> str:
        return f"P{self.number:02d} / 玩家視角 R{self.player_row + 1}C{self.player_col + 1}"


@dataclass(frozen=True)
class InspectionRecord:
    point: InspectionPoint
    target: Pose
    actual: Pose
    error_mm: float


def build_inspection_points(motion: MotionConfig) -> tuple[InspectionPoint, ...]:
    """Return all points in a short snake path, labelled in player view."""
    matrix = motion.player_board_xy_matrix()
    points: list[InspectionPoint] = []
    for row in range(10):
        columns: Iterable[int] = range(9) if row % 2 == 0 else range(8, -1, -1)
        for col in columns:
            x, y = matrix[row][col]
            points.append(InspectionPoint(len(points) + 1, row, col, x, y))
    return tuple(points)


class BoardMatrixInspector:
    def __init__(
        self,
        robot: TMRobotAdapter,
        motion: MotionConfig,
        profile: InspectionProfile,
        *,
        allow_below_safe_z: bool = False,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.robot = robot
        self.motion = motion
        self.profile = profile
        self.allow_below_safe_z = allow_below_safe_z
        self.stop_event = stop_event or threading.Event()

    def _guard(self) -> None:
        if self.stop_event.is_set():
            raise InterruptedError("巡檢已停止；不再送出後續移動命令。")

    def run(
        self,
        points: Iterable[InspectionPoint],
        on_record: Callable[[InspectionRecord], None],
        *,
        before_point: Callable[[InspectionPoint], None] | None = None,
    ) -> None:
        self._guard()
        self.profile.validate(self.motion, allow_below_safe_z=self.allow_below_safe_z)
        selected = tuple(points)
        if not selected:
            raise ValueError("沒有要巡檢的點位。")
        if self.motion.orientation_rxyz is None or self.motion.z_safe is None:
            raise RuntimeError("固定姿態或 Z_SAFE 尚未設定。")

        fixed_orientation = self.motion.orientation_rxyz
        p = self.profile
        with self.robot._lock, self.robot._connect() as sock:
            self._guard()
            self.robot._set_gripper(sock, False, self.stop_event)
            current = self.robot._read_pose(sock)
            self._guard()

            # Start above all configured obstacles before changing XY or tool
            # orientation. Inter-point travel then follows the operator-requested
            # Z=380 profile.
            current_safe = (current[0], current[1], self.motion.z_safe, current[3], current[4], current[5])
            self.robot._move_to(sock, current_safe, speed=p.travel_speed, acceleration=p.acceleration, stop_event=self.stop_event)
            fixed_safe = (current[0], current[1], self.motion.z_safe, *fixed_orientation)
            self.robot._move_to(sock, fixed_safe, speed=p.travel_speed, acceleration=p.acceleration, stop_event=self.stop_event)

            for index, point in enumerate(selected):
                self._guard()
                if before_point is not None:
                    before_point(point)
                self._guard()
                if index == 0:
                    above_first = (point.x, point.y, self.motion.z_safe, *fixed_orientation)
                    self.robot._move_to(sock, above_first, speed=p.travel_speed, acceleration=p.acceleration, stop_event=self.stop_event)

                travel = (point.x, point.y, p.travel_z, *fixed_orientation)
                approach = (point.x, point.y, p.approach_z, *fixed_orientation)
                target = (point.x, point.y, p.inspection_z, *fixed_orientation)
                self.robot._move_to(sock, travel, speed=p.travel_speed, acceleration=p.acceleration, stop_event=self.stop_event)
                self.robot._move_to(sock, approach, speed=p.approach_speed, acceleration=p.acceleration, stop_event=self.stop_event)
                self.robot._move_to(sock, target, speed=p.inspection_speed, acceleration=p.acceleration, stop_event=self.stop_event)
                self._guard()
                actual = self.robot._read_pose(sock)
                error = math.sqrt(sum((actual[i] - target[i]) ** 2 for i in range(3)))
                on_record(InspectionRecord(point, target, actual, error))

                deadline = time.monotonic() + p.dwell_seconds
                while time.monotonic() < deadline:
                    self._guard()
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                self._guard()
                self.robot._move_to(sock, travel, speed=p.approach_speed, acceleration=p.acceleration, stop_event=self.stop_event)

            self._guard()
            last = selected[-1]
            final_safe = (last.x, last.y, self.motion.z_safe, *fixed_orientation)
            self.robot._move_to(sock, final_safe, speed=p.travel_speed, acceleration=p.acceleration, stop_event=self.stop_event)
