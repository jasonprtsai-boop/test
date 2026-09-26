import socket
import time
from typing import Optional, Tuple

import numpy as np

from config import (
    CORNER_COORDS,
    GRAVEYARD_DROP_POS,
    GRIPPER_DO_DEVICE,
    GRIPPER_DO_PIN,
    LISTEN_PORT,
    MOTION_SETTLE_SECONDS,
    RELEASE_SETTLE_SECONDS,
    ROBOT_IP,
    SIMULATION_MODE,
    VACUUM_SETTLE_SECONDS,
    Z_APPROACH,
    Z_PICK,
    Z_SAFE,
)


XY = Tuple[float, float]


def grid_to_robot_xy(col: int, row: int) -> XY:
    if not (0 <= col <= 8 and 0 <= row <= 9):
        raise ValueError("grid position must be col 0..8 and row 0..9")
    u = col / 8.0
    v = row / 9.0
    bl = np.array(CORNER_COORDS["BL"])
    br = np.array(CORNER_COORDS["BR"])
    tl = np.array(CORNER_COORDS["TL"])
    tr = np.array(CORNER_COORDS["TR"])
    bottom = (1 - u) * bl + u * br
    top = (1 - u) * tl + u * tr
    target = (1 - v) * bottom + v * top
    return float(target[0]), float(target[1])


class RobotController:
    def __init__(self, simulation: bool = SIMULATION_MODE) -> None:
        self.simulation = simulation
        self.sock: Optional[socket.socket] = None
        if not self.simulation:
            self._ensure_connected()

    def _ensure_connected(self, timeout: float = 5.0) -> None:
        if self.simulation or self.sock is not None:
            return
        deadline = time.monotonic() + timeout
        last_err = None
        while time.monotonic() < deadline:
            try:
                self.sock = socket.create_connection(
                    (ROBOT_IP, LISTEN_PORT), timeout=2.0
                )
                return
            except (OSError, socket.error) as err:
                last_err = err
                time.sleep(0.3)
        raise RuntimeError(
            f"無法連線至機械手臂 Listen 節點 ({ROBOT_IP}:{LISTEN_PORT})：{last_err}"
        )

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def send_script(self, script_cmd: str) -> bytes:
        if self.simulation:
            print(f"[SIMULATION] {script_cmd}")
            return b"SIMULATION"
        self._ensure_connected()
        body = f"TMSCT,{len(script_cmd)},{script_cmd},"
        checksum = 0
        for byte in body.encode("utf-8"):
            checksum ^= byte
        packet = f"${body}*{checksum:02X}\r\n"
        try:
            self.sock.sendall(packet.encode("utf-8"))
            return self.sock.recv(1024)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close()
            self._ensure_connected()
            self.sock.sendall(packet.encode("utf-8"))
            return self.sock.recv(1024)

    def trigger_capture(self) -> None:
        """Release TMflow Listen so it performs one Point -> Vision cycle."""
        try:
            self.send_script("ScriptExit()")
        finally:
            # TMflow exits the Listen node; close socket so it cleanly reconnects
            # when TMflow returns to the Listen node after vision.
            self.close()

    def execute_chess_move(
        self,
        from_pos: Tuple[int, int],
        to_pos: Tuple[int, int],
        captured_count: Optional[int] = None,
    ) -> None:
        """Move a piece while TMflow is waiting in its Listen node."""
        if captured_count is not None:
            self.remove_grid_piece(to_pos, captured_count)
        self.move_grid_piece(from_pos, to_pos)

    def set_gripper(self, state: bool) -> None:
        self.send_script(
            f'IO["{GRIPPER_DO_DEVICE}"].DO[{GRIPPER_DO_PIN}] = {1 if state else 0}'
        )
        time.sleep(
            VACUUM_SETTLE_SECONDS if state else RELEASE_SETTLE_SECONDS
        )

    def _line(self, xy: XY, z: float, speed: int = 100) -> None:
        x, y = xy
        command = f'Line("CAP", {x}, {y}, {z}, 180, 0, 0, {speed}, 200, 0)'
        self.send_script(command)
        time.sleep(MOTION_SETTLE_SECONDS)

    def pick_and_place(self, from_xy: XY, to_xy: XY) -> None:
        self._line(from_xy, Z_SAFE)
        self._line(from_xy, Z_APPROACH, 60)
        self._line(from_xy, Z_PICK, 30)
        self.set_gripper(True)
        self._line(from_xy, Z_APPROACH, 50)
        self._line(from_xy, Z_SAFE, 80)
        self._line(to_xy, Z_SAFE)
        self._line(to_xy, Z_APPROACH, 60)
        self._line(to_xy, Z_PICK, 30)
        self.set_gripper(False)
        self._line(to_xy, Z_APPROACH, 50)
        self._line(to_xy, Z_SAFE, 80)

    def move_grid_piece(
        self, from_pos: Tuple[int, int], to_pos: Tuple[int, int]
    ) -> None:
        self.pick_and_place(
            grid_to_robot_xy(*from_pos), grid_to_robot_xy(*to_pos)
        )

    def remove_grid_piece(
        self, pos: Tuple[int, int], captured_count: Optional[int] = None
    ) -> None:
        """Pick a captured piece from the board and drop it from high altitude into the graveyard box."""
        from_xy = grid_to_robot_xy(*pos)
        self._line(from_xy, Z_SAFE)
        self._line(from_xy, Z_APPROACH, 60)
        self._line(from_xy, Z_PICK, 30)
        self.set_gripper(True)
        self._line(from_xy, Z_APPROACH, 50)
        self._line(from_xy, Z_SAFE, 80)
        self._line(GRAVEYARD_DROP_POS, Z_SAFE)
        self.set_gripper(False)
