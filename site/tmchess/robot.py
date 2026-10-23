"""TM5-700 Listen/TMSCT adapter extracted from the measured dual-view test.

The adapter sends only commands already observed in the local test program.  It
does not attempt to alter the TMflow graph, Base, TCP or safety configuration.
"""

from __future__ import annotations

import itertools
import logging
import math
import os
import random
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .board import Move, Position
from .config import SITE_ROOT, MotionConfig, NetworkConfig

Pose = tuple[float, float, float, float, float, float]
LOGGER = logging.getLogger(__name__)


class StopRequested(RuntimeError):
    pass


class GripperStateUnknown(RuntimeError):
    """The adapter could not confirm that the vacuum output was turned off."""


class ListenModeUnavailable(RuntimeError):
    """The controller accepted TCP but TMflow is not at a Listen node."""


class HardwareLease:
    """Process-wide file lease preventing two new adapters using one robot.

    Legacy experimental scripts outside ``site`` do not participate in this
    lease and must still be kept closed while the website is running.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def acquire(self) -> None:
        if self._file is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                "另一個網站或點位工具正在使用 TM5-700；請先關閉另一個程序。"
            ) from exc
        self._file = handle

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@dataclass(frozen=True)
class RobotCallbacks:
    pose: Callable[[Pose], None] = lambda _pose: None
    action: Callable[[str, str], None] = lambda _current, _next: None


class TMRobotAdapter:
    def __init__(
        self,
        network: NetworkConfig,
        motion: MotionConfig,
        callbacks: RobotCallbacks | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self.network = network
        self.motion = motion
        self.callbacks = callbacks or RobotCallbacks()
        self._random = random_source or random.Random()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._hardware_lease = HardwareLease(SITE_ROOT / "runtime" / "tm5-hardware.lock")

    @staticmethod
    def make_packet(script_id: str, script: str) -> bytes:
        payload = f"{script_id},{script}".encode("utf-8")
        body = b"TMSCT," + str(len(payload)).encode("ascii") + b"," + payload + b","
        checksum = 0
        for value in body:
            checksum ^= value
        return b"$" + body + b"*" + f"{checksum:02X}".encode("ascii") + b"\r\n"

    def _script_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids) % 100000:05d}"

    def _connect(self) -> socket.socket:
        self._hardware_lease.acquire()
        sock = socket.create_connection(
            (self.network.robot_ip, self.network.robot_listen_port),
            timeout=self.motion.socket_timeout_seconds,
        )
        sock.settimeout(self.motion.socket_timeout_seconds)
        LOGGER.info(
            "[ROBOT] Listen connection established target=%s:%s",
            self.network.robot_ip,
            self.network.robot_listen_port,
        )
        return sock

    @staticmethod
    def _raise_protocol_error(buffer: bytes, script: str) -> None:
        if b"$CPERR,2,F1," in buffer:
            raise ListenModeUnavailable(
                f"TMflow 尚未進入 Listen 節點，指令未執行：{script}"
            )
        if b"$CPERR," in buffer:
            raise RuntimeError(f"Robot 通訊協定錯誤（指令：{script}）：{buffer!r}")

    def _send_script(self, sock: socket.socket, script: str, prefix: str) -> bytes:
        script_id = self._script_id(prefix)
        sock.sendall(self.make_packet(script_id, script))
        expected = f"{script_id},OK".encode("ascii")
        deadline = time.monotonic() + self.motion.socket_timeout_seconds
        buffer = b""
        while time.monotonic() < deadline:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError(f"Robot 已關閉 Listen 連線（等待指令回覆：{script}）。")
            buffer += data
            self._raise_protocol_error(buffer, script)
            if expected in buffer:
                return buffer
        raise TimeoutError(f"等待 Robot 回覆逾時：{script}")

    @staticmethod
    def parse_pose_packet(buffer: bytes) -> Pose | None:
        marker = b"$TMSTA,"
        start = buffer.find(marker)
        while start >= 0:
            length_start = start + len(marker)
            length_end = buffer.find(b",", length_start)
            if length_end < 0:
                return None
            try:
                payload_length = int(buffer[length_start:length_end])
            except ValueError:
                start = buffer.find(marker, start + 1)
                continue
            payload_start = length_end + 1
            payload_end = payload_start + payload_length
            if len(buffer) < payload_end:
                return None
            payload = buffer[payload_start:payload_end]
            if payload.startswith(b"90,") and len(payload) >= 27:
                try:
                    pose = tuple(float(value) for value in struct.unpack("<6f", payload[3:27]))
                except struct.error:
                    return None
                if all(math.isfinite(value) for value in pose):
                    return pose  # type: ignore[return-value]
            start = buffer.find(marker, start + 1)
        return None

    def _read_pose(self, sock: socket.socket) -> Pose:
        script_id = self._script_id("POS")
        script = "ListenSend(90,Robot[0].CoordRobot)"
        sock.sendall(self.make_packet(script_id, script))
        expected = f"{script_id},OK".encode("ascii")
        deadline = time.monotonic() + self.motion.socket_timeout_seconds
        buffer = b""
        pose: Pose | None = None
        while time.monotonic() < deadline:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                raise ConnectionError("Robot 已關閉 Listen 連線（等待 Pose 回覆）。")
            buffer += data
            self._raise_protocol_error(buffer, script)
            pose = pose or self.parse_pose_packet(buffer)
            if pose is not None and expected in buffer:
                self.callbacks.pose(pose)
                return pose
        missing = []
        if pose is None:
            missing.append("TMSTA Pose")
        if expected not in buffer:
            missing.append(f"{script_id} OK")
        raise TimeoutError(f"無法完整取得 Robot Pose，缺少：{', '.join(missing)}")

    def _open_listen_session(self) -> tuple[socket.socket, Pose]:
        """Open a session only after TMflow has actually returned to Listen.

        F1 means the probe was discarded, so reconnecting and probing again
        cannot duplicate an accepted robot command.
        """
        deadline = time.monotonic() + self.motion.listen_ready_timeout_seconds
        while True:
            sock = self._connect()
            try:
                return sock, self._read_pose(sock)
            except ListenModeUnavailable as exc:
                sock.close()
                if time.monotonic() >= deadline:
                    raise ListenModeUnavailable(
                        "TMflow 未在等待時間內回到 Listen 節點；"
                        "請確認專案正在執行、流程已走到 Listen，且沒有停在 Vision 或其他節點。"
                    ) from exc
                time.sleep(self.motion.listen_retry_seconds)
            except Exception:
                sock.close()
                raise

    @staticmethod
    def _guard_not_stopped(stop_event: threading.Event | None) -> None:
        if stop_event is not None and stop_event.is_set():
            raise StopRequested("流程已停止；不再送出後續 Robot 命令。")

    def _set_gripper(
        self,
        sock: socket.socket,
        enabled: bool,
        stop_event: threading.Event | None = None,
        *,
        ignore_stop: bool = False,
    ) -> None:
        if not ignore_stop:
            self._guard_not_stopped(stop_event)
        value = 1 if enabled else 0
        script = f'IO["{self.motion.gripper_do_device}"].DO[{self.motion.gripper_do_pin}] = {value}'
        self._send_script(sock, script, "DO")
        LOGGER.info("[ROBOT] gripper output confirmed state=%s", "on" if enabled else "off")

    def _send_line(
        self,
        sock: socket.socket,
        pose: Pose,
        *,
        speed: int | None = None,
        acceleration: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._guard_not_stopped(stop_event)
        x, y, z, rx, ry, rz = pose
        line_speed = self.motion.speed if speed is None else speed
        line_acceleration = self.motion.acceleration if acceleration is None else acceleration
        if line_speed <= 0 or line_acceleration <= 0:
            raise ValueError("Line 速度與加速度必須大於 0。")
        script = (
            f'Line("CPP",{x:.3f},{y:.3f},{z:.3f},{rx:.3f},{ry:.3f},{rz:.3f},'
            f'{line_speed},{line_acceleration},{self.motion.blend},{self.motion.precise})'
        )
        self._send_script(sock, script, "MOVE")
        LOGGER.info(
            "[ROBOT] Line accepted target=(%.3f, %.3f, %.3f, %.3f, %.3f, %.3f)",
            x, y, z, rx, ry, rz,
        )

    @staticmethod
    def _xyz_distance(a: Pose, b: Pose) -> float:
        return math.sqrt(sum((a[index] - b[index]) ** 2 for index in range(3)))

    @staticmethod
    def _angle_distance_degrees(a: float, b: float) -> float:
        """Return the shortest absolute distance between two degree values."""
        return abs((a - b + 180.0) % 360.0 - 180.0)

    @classmethod
    def _orientation_error(cls, a: Pose, b: Pose) -> float:
        """Return the largest Euler-ZYX component error across RX, RY and RZ.

        TM reports these components in degrees within [-180, 180].  The fixed
        project target is away from the Euler pitch singularity, so component
        comparison with wrap-around is appropriate for this arrival check.
        """
        return max(cls._angle_distance_degrees(a[index], b[index]) for index in range(3, 6))

    def _wait_until_pose(
        self,
        sock: socket.socket,
        target: Pose,
        stop_event: threading.Event | None = None,
    ) -> None:
        deadline = time.monotonic() + self.motion.move_timeout_seconds
        last: Pose | None = None
        position_error = float("inf")
        orientation_error = float("inf")
        stable = 0
        read_failures = 0
        while time.monotonic() < deadline:
            self._guard_not_stopped(stop_event)
            try:
                pose = self._read_pose(sock)
                read_failures = 0
            except TimeoutError:
                read_failures += 1
                if read_failures >= 3:
                    raise
                time.sleep(self.motion.pose_poll_seconds)
                continue
            position_error = self._xyz_distance(pose, target)
            orientation_error = self._orientation_error(pose, target)
            position_delta = self._xyz_distance(pose, last) if last is not None else float("inf")
            orientation_delta = self._orientation_error(pose, last) if last is not None else float("inf")
            arrived = (
                position_error <= self.motion.position_tolerance_mm
                and orientation_error <= self.motion.orientation_tolerance_deg
            )
            settled = (
                position_delta <= self.motion.stable_delta_mm
                and orientation_delta <= self.motion.orientation_stable_delta_deg
            )
            stable = stable + 1 if arrived and settled else 0
            if stable >= self.motion.stable_samples:
                return
            last = pose
            time.sleep(self.motion.pose_poll_seconds)
        if last is None:
            raise TimeoutError("Robot 未在時間內回報可驗證的目標 Pose。")
        raise TimeoutError(
            "Robot 未在時間內穩定到達目標 Pose"
            f"（XYZ 誤差 {position_error:.3f} mm，姿態最大誤差 {orientation_error:.3f}°）。"
        )

    def _wait_until_stable(self, sock: socket.socket, initial: Pose) -> Pose:
        """Confirm that pose feedback is stationary for consecutive samples."""
        deadline = time.monotonic() + self.motion.listen_ready_timeout_seconds
        last = initial
        stable = 0
        while time.monotonic() < deadline:
            pose = self._read_pose(sock)
            settled = (
                self._xyz_distance(pose, last) <= self.motion.stable_delta_mm
                and self._orientation_error(pose, last)
                <= self.motion.orientation_stable_delta_deg
            )
            stable = stable + 1 if settled else 0
            if stable >= self.motion.stable_samples:
                return pose
            last = pose
            time.sleep(self.motion.pose_poll_seconds)
        raise TimeoutError("Robot Pose 未在等待時間內穩定；不能確認動作已停止。")

    def _move_to(
        self,
        sock: socket.socket,
        pose: Pose,
        *,
        speed: int | None = None,
        acceleration: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._guard_not_stopped(stop_event)
        self._send_line(
            sock,
            pose,
            speed=speed,
            acceleration=acceleration,
            stop_event=stop_event,
        )
        self._wait_until_pose(sock, pose, stop_event=stop_event)

    def _xy_for_cell(self, position: Position) -> tuple[float, float]:
        col, row = position
        if not (0 <= col < 9 and 0 <= row < 10):
            raise ValueError(f"棋盤位置超出範圍：col={col}, row={row}")
        return self.motion.board_xy_matrix()[row][col]

    def _pose(self, xy: tuple[float, float], z: float | None) -> Pose:
        if self.motion.orientation_rxyz is None or z is None:
            raise RuntimeError("姿態或 Z 高度尚未設定。")
        return xy[0], xy[1], z, *self.motion.orientation_rxyz

    def _enter_safe_plane(
        self,
        sock: socket.socket,
        current: Pose,
        stop_event: threading.Event,
    ) -> None:
        """Enter the configured horizontal transfer plane before board motion.

        The first lift preserves the current tool orientation so an interrupted
        low pose never rotates the suction assembly beside a chess piece.  The
        configured fixed orientation is applied only after reaching Z_SAFE.
        """
        if self.motion.z_safe is None or self.motion.orientation_rxyz is None:
            raise RuntimeError("安全高度或固定姿態尚未設定。")
        self._guard_not_stopped(stop_event)
        entry_z = max(current[2], self.motion.z_safe)
        current_safe: Pose = (
            current[0], current[1], entry_z,
            current[3], current[4], current[5],
        )
        if self._xyz_distance(current, current_safe) > self.motion.position_tolerance_mm:
            self.callbacks.action("原地抬升至安全高度", "套用固定姿態")
            self._move_to(sock, current_safe, stop_event=stop_event)
        oriented_safe: Pose = (
            current[0], current[1], entry_z,
            *self.motion.orientation_rxyz,
        )
        if self._orientation_error(current_safe, oriented_safe) > self.motion.orientation_tolerance_deg:
            self.callbacks.action("在安全高度套用固定姿態", "移動至棋盤目標")
            self._move_to(sock, oriented_safe, stop_event=stop_event)
        # A camera/standby pose can be above Z_SAFE. Keeping that height here
        # made the first board move a diagonal descent instead of a horizontal
        # transfer on the configured plane. Descend at the current XY first,
        # only after orientation has been established above the safe plane.
        transfer_pose: Pose = (
            current[0], current[1], self.motion.z_safe,
            *self.motion.orientation_rxyz,
        )
        if self._xyz_distance(oriented_safe, transfer_pose) > self.motion.position_tolerance_mm:
            self.callbacks.action("原地進入棋盤安全平面", "確認到位後水平移向棋盤")
            self._move_to(sock, transfer_pose, stop_event=stop_event)

    def _transfer(
        self,
        sock: socket.socket,
        source_xy: tuple[float, float],
        destination_xy: tuple[float, float],
        destination_heights: tuple[float | None, float | None, float | None] | None = None,
        stop_event: threading.Event | None = None,
        source_pick_z: float | None = None,
    ) -> None:
        destination_heights = destination_heights or (
            self.motion.z_safe,
            self.motion.z_approach,
            self.motion.z_pick,
        )
        destination_safe_z, destination_approach_z, destination_release_z = destination_heights
        safe_source = self._pose(source_xy, self.motion.z_safe)
        approach_source = self._pose(source_xy, self.motion.z_approach)
        pick_source = self._pose(
            source_xy,
            self.motion.z_pick if source_pick_z is None else source_pick_z,
        )
        safe_destination = self._pose(destination_xy, destination_safe_z)
        approach_destination = self._pose(destination_xy, destination_approach_z)
        place_destination = self._pose(destination_xy, destination_release_z)
        for pose in (safe_source, approach_source, pick_source):
            self._move_to(sock, pose, stop_event=stop_event)
        self._set_gripper(sock, True, stop_event)
        time.sleep(self.motion.vacuum_settle_seconds)
        for pose in (approach_source, safe_source, safe_destination, approach_destination, place_destination):
            self._move_to(sock, pose, stop_event=stop_event)
        self._set_gripper(sock, False, stop_event)
        time.sleep(self.motion.release_settle_seconds)
        for pose in (approach_destination, safe_destination):
            self._move_to(sock, pose, stop_event=stop_event)

    def _graveyard_drop_xy(self) -> tuple[float, float]:
        center = self.motion.graveyard_xy
        opening = self.motion.graveyard_opening_xy
        scatter_radius = self.motion.graveyard_scatter_radius_mm
        diameter = self.motion.graveyard_payload_diameter_mm
        margin = self.motion.graveyard_edge_margin_mm
        if center is None or opening is None or scatter_radius is None or diameter is None:
            raise RuntimeError("落子箱中心、淨開口、散布半徑或棋子直徑尚未設定。")
        width, height = opening
        if 2 * scatter_radius + diameter + 2 * margin > min(width, height):
            raise RuntimeError("落子箱散布半徑超出淨開口。")
        # Uniform-by-area point inside a circle. The circular boundary is safe
        # even when the square box is rotated relative to RobotBase X/Y.
        angle = self._random.uniform(0.0, math.tau)
        radius = scatter_radius * math.sqrt(self._random.random())
        return (
            center[0] + radius * math.cos(angle),
            center[1] + radius * math.sin(angle),
        )

    def probe(self) -> Pose:
        with self._lock:
            sock, pose = self._open_listen_session()
            with sock:
                pose = self._wait_until_stable(sock, pose)
                self._set_gripper(sock, False)
                return pose

    def confirm_stationary(self) -> Pose:
        """Wait for TMflow Listen availability and stable pose feedback."""
        with self._lock:
            sock, pose = self._open_listen_session()
            with sock:
                return self._wait_until_stable(sock, pose)

    def trigger_capture(self, stop_event: threading.Event) -> None:
        if stop_event.is_set():
            raise StopRequested("流程已停止。")
        self.callbacks.action("要求 TMflow 拍攝雙視角", "等待 V1 / V2 影像")
        with self._lock:
            sock, _pose = self._open_listen_session()
            with sock:
                self._send_script(sock, "ScriptExit()", "EXIT")

    def execute_move(
        self,
        move: Move,
        stop_event: threading.Event,
        *,
        source_pick_offset_mm: float = 0.0,
    ) -> dict[str, float | None]:
        started = time.monotonic()
        with self._lock:
            self._guard_not_stopped(stop_event)
            sock, current_pose = self._open_listen_session()
            with sock:
                self._enter_safe_plane(sock, current_pose, stop_event)
                graveyard_xy = self._execute_move_in_session(
                    sock,
                    move,
                    stop_event,
                    source_pick_offset_mm=source_pick_offset_mm,
                )
        return {
            "adapter_seconds": round(time.monotonic() - started, 3),
            "graveyard_x": round(graveyard_xy[0], 3) if graveyard_xy is not None else None,
            "graveyard_y": round(graveyard_xy[1], 3) if graveyard_xy is not None else None,
        }

    def _execute_move_in_session(
        self,
        sock: socket.socket,
        move: Move,
        stop_event: threading.Event,
        *,
        source_pick_offset_mm: float = 0.0,
    ) -> tuple[float, float] | None:
        if self.motion.z_pick is None:
            raise RuntimeError("棋盤取棋高度尚未設定。")
        source_pick_z = self.motion.z_pick + source_pick_offset_mm
        if (
            self.motion.board_surface_z is not None
            and source_pick_z <= self.motion.board_surface_z
        ):
            raise RuntimeError("重新取棋高度不得低於棋盤表面。")
        gripper_may_be_on = False
        primary_error: Exception | None = None
        graveyard_xy: tuple[float, float] | None = None
        try:
            if move.captured_piece:
                if stop_event.is_set():
                    raise StopRequested("流程已停止。")
                self.callbacks.action("移除 AI 將吃掉的棋子", "移動 AI 棋子")
                gripper_may_be_on = True
                graveyard_xy = self._graveyard_drop_xy()
                capture_args = (
                    sock,
                    self._xy_for_cell(move.destination),
                    graveyard_xy,
                    (
                        self.motion.graveyard_z_safe,
                        self.motion.graveyard_z_approach,
                        self.motion.graveyard_z_release,
                    ),
                    stop_event,
                )
                if source_pick_offset_mm:
                    self._transfer(*capture_args, source_pick_z=source_pick_z)
                else:
                    self._transfer(*capture_args)
                gripper_may_be_on = False
            if stop_event.is_set():
                raise StopRequested("流程已停止。")
            self.callbacks.action("移動 AI 棋子", "提交棋步並切換回合")
            gripper_may_be_on = True
            transfer_args = (
                sock,
                self._xy_for_cell(move.source),
                self._xy_for_cell(move.destination),
            )
            if source_pick_offset_mm:
                self._transfer(
                    *transfer_args,
                    stop_event=stop_event,
                    source_pick_z=source_pick_z,
                )
            else:
                self._transfer(*transfer_args, stop_event=stop_event)
            gripper_may_be_on = False
            return graveyard_xy
        except Exception as exc:
            primary_error = exc
            LOGGER.exception("[ROBOT] move failed before gripper cleanup")
            raise
        finally:
            if gripper_may_be_on:
                try:
                    self._set_gripper(sock, False, ignore_stop=True)
                except Exception as exc:
                    LOGGER.exception("[ROBOT] gripper cleanup failed")
                    original = (
                        f" 原始流程錯誤：{type(primary_error).__name__}: {primary_error}。"
                        if primary_error is not None else ""
                    )
                    raise GripperStateUnknown(
                        "吸盤關閉命令失敗，實際輸出狀態不明；禁止繼續自動動作。"
                        + original
                        + f" 關閉失敗原因：{type(exc).__name__}: {exc}"
                    ) from exc

    def close(self) -> None:
        self._hardware_lease.release()
