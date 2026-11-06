"""Strict framing for the TM Listen Node TCP protocol."""

from __future__ import annotations

from dataclasses import dataclass


MAX_PAYLOAD_BYTES = 1024 * 1024
SUPPORTED_HEADERS = {"TMSCT", "TMSTA", "CPERR"}


class RobotProtocolError(RuntimeError):
    """A controller message violates the documented Listen Node frame format."""


@dataclass(frozen=True)
class RobotFrame:
    header: str
    payload: bytes
    raw: bytes


def _checksum(body: bytes) -> int:
    result = 0
    for value in body:
        result ^= value
    return result


def encode_frame(header: str, payload: bytes) -> bytes:
    """Build ``$Header,Length,Data,*CS\r\n`` with an XOR checksum."""
    if header not in SUPPORTED_HEADERS:
        raise ValueError(f"不支援的 Robot frame header：{header}")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("Robot frame payload 過大。")
    body = header.encode("ascii") + b"," + str(len(payload)).encode("ascii") + b"," + payload + b","
    return b"$" + body + b"*" + f"{_checksum(body):02X}".encode("ascii") + b"\r\n"


def _payload_length(token: bytes) -> int:
    try:
        text = token.decode("ascii")
        base = 16 if text.lower().startswith("0x") else 2 if text.lower().startswith("0b") else 10
        value = int(text, base)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RobotProtocolError(f"Robot frame length 無效：{token!r}") from exc
    if value < 0 or value > MAX_PAYLOAD_BYTES:
        raise RobotProtocolError(f"Robot frame length 超出範圍：{value}")
    return value


def decode_frames(buffer: bytes) -> tuple[list[RobotFrame], bytes]:
    """Decode all complete frames and return any trailing partial frame.

    Invalid leading bytes, delimiters, checksums, or terminators fail closed.
    """
    frames: list[RobotFrame] = []
    remaining = buffer
    while remaining:
        if not remaining.startswith(b"$"):
            raise RobotProtocolError(f"Robot 回覆不是合法 frame 起點：{remaining[:32]!r}")

        header_end = remaining.find(b",", 1)
        if header_end < 0:
            if len(remaining) > 32:
                raise RobotProtocolError("Robot frame header 缺少分隔符。")
            break
        try:
            header = remaining[1:header_end].decode("ascii")
        except UnicodeDecodeError as exc:
            raise RobotProtocolError("Robot frame header 不是 ASCII。") from exc
        if header not in SUPPORTED_HEADERS:
            raise RobotProtocolError(f"不支援的 Robot frame header：{header!r}")

        length_end = remaining.find(b",", header_end + 1)
        if length_end < 0:
            if len(remaining) - header_end > 32:
                raise RobotProtocolError("Robot frame length 缺少分隔符。")
            break
        payload_length = _payload_length(remaining[header_end + 1:length_end])
        payload_start = length_end + 1
        payload_end = payload_start + payload_length
        frame_end = payload_end + 6  # ,* + 2 hex + CRLF
        if len(remaining) < frame_end:
            break

        if remaining[payload_end:payload_end + 2] != b",*":
            raise RobotProtocolError("Robot frame payload 後缺少 checksum 分隔符。")
        checksum_bytes = remaining[payload_end + 2:payload_end + 4]
        try:
            received_checksum = int(checksum_bytes.decode("ascii"), 16)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RobotProtocolError(f"Robot frame checksum 無效：{checksum_bytes!r}") from exc
        if remaining[payload_end + 4:frame_end] != b"\r\n":
            raise RobotProtocolError("Robot frame 缺少 CRLF 結尾。")

        checksum_body = remaining[1:payload_end + 1]
        expected_checksum = _checksum(checksum_body)
        if received_checksum != expected_checksum:
            raise RobotProtocolError(
                f"Robot frame checksum 不符：收到 {received_checksum:02X}，預期 {expected_checksum:02X}。"
            )

        raw = remaining[:frame_end]
        frames.append(RobotFrame(header, remaining[payload_start:payload_end], raw))
        remaining = remaining[frame_end:]
    return frames, remaining
