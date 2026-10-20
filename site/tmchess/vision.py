"""Restricted dual-view receiver and YOLO fusion adapter."""

from __future__ import annotations

import math
import logging
import json
import multiprocessing
import os
import queue
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Callable

from .board import Board, NAME_TO_ID, PIECE_LABELS, PIECE_LIMITS, is_red, validate_board
from .config import SITE_ROOT, VisionConfig


CAPTURE_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
V2_TO_MAIN = {(local_row, local_col): (local_row + 1, local_col) for local_row in range(4) for local_col in range(4)}
V2_MAIN_CELLS = set(V2_TO_MAIN.values())
LOGGER = logging.getLogger(__name__)
ConfidenceGrid = list[list[float | None]]


def _validate_model_contract(model) -> None:
    configured_names = model.names
    name_values = configured_names.values() if hasattr(configured_names, "values") else configured_names
    names = {str(value) for value in name_values}
    expected = set(NAME_TO_ID)
    if names != expected:
        missing = sorted(expected - names)
        unexpected = sorted(names - expected)
        raise RuntimeError(
            "YOLO 類別與象棋盤面定義不一致"
            f"（缺少：{missing or '無'}；多出：{unexpected or '無'}）。"
        )


def _predict_with_model(
    model,
    image_path: str,
    output_path: str,
    threshold: float,
    save_output: bool,
) -> list[dict]:
    result = model.predict(source=image_path, conf=threshold, verbose=False)[0]
    if save_output:
        result.save(filename=output_path)
    detections: list[dict] = []
    if result.boxes is not None:
        for box in result.boxes:
            class_id = int(box.cls[0])
            label = str(model.names[class_id])
            if label not in NAME_TO_ID:
                continue
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().tolist()
            detections.append({
                "piece": NAME_TO_ID[label],
                "confidence": float(box.conf[0]),
                "cx": (x1 + x2) / 2,
                "cy": (y1 + y2) / 2,
            })
    return detections


def _yolo_worker(config: VisionConfig, requests, responses) -> None:
    """Own the native ML runtime so a hung inference can be terminated."""
    settings_root = SITE_ROOT / "runtime" / "ultralytics"
    (settings_root / "Ultralytics").mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("YOLO_CONFIG_DIR", str(settings_root))
    try:
        from ultralytics import YOLO

        model = YOLO(str(config.model_path))
        _validate_model_contract(model)
    except Exception as exc:
        responses.put({
            "kind": "startup_error",
            "error_type": type(exc).__name__,
            "message": str(exc),
        })
        return
    responses.put({"kind": "ready"})
    while True:
        request = requests.get()
        if request is None:
            return
        request_id = request["request_id"]
        try:
            detections = _predict_with_model(
                model,
                request["image_path"],
                request["output_path"],
                request["threshold"],
                request["save_output"],
            )
        except Exception as exc:
            responses.put({
                "kind": "error",
                "request_id": request_id,
                "error_type": type(exc).__name__,
                "message": str(exc),
            })
        else:
            responses.put({
                "kind": "result",
                "request_id": request_id,
                "detections": detections,
            })


@dataclass(frozen=True)
class CaptureResult:
    capture_id: str
    board: Board
    v1_path: Path
    v2_path: Path
    confidence: ConfidenceGrid | None = None
    result_path: Path | None = None
    capture_seconds: float | None = None
    processing_seconds: float | None = None
    total_seconds: float | None = None


class BoardObservationError(ValueError):
    """Detection completed, but the observed board needs contextual validation."""
    def __init__(self, message: str, result: CaptureResult) -> None:
        super().__init__(message)
        self.result = result


class DualViewInbox:
    def __init__(
        self,
        capture_root: Path,
        allowed_ip: str,
        max_bytes: int,
        expected_view_sizes: dict[str, tuple[int, int]] | None = None,
    ) -> None:
        self.storage_root = capture_root.resolve()
        self.capture_root = self.storage_root
        self.allowed_ip = allowed_ip
        self.max_bytes = max_bytes
        self.expected_view_sizes = dict(expected_view_sizes or {})
        if self.expected_view_sizes:
            if set(self.expected_view_sizes) != {"v1", "v2"}:
                raise ValueError("雙視角解析度設定必須同時包含 v1 與 v2。")
            if len(set(self.expected_view_sizes.values())) != 2:
                raise ValueError("V1 與 V2 解析度必須不同，才能安全識別視角。")
        self._condition = threading.Condition()
        self._active_id: str | None = None
        self._filename_prefix: str | None = None
        self._paths: dict[str, Path] = {}
        self._closed = True
        self._cancelled = False

    def set_receiver_ready(self, ready: bool) -> None:
        with self._condition:
            self._closed = not ready
            self._condition.notify_all()

    @property
    def receiver_ready(self) -> bool:
        with self._condition:
            return not self._closed

    def select_game(self, game_id: str) -> None:
        if not CAPTURE_ID_PATTERN.fullmatch(game_id):
            raise ValueError("game id 格式錯誤。")
        with self._condition:
            if self._active_id is not None:
                raise RuntimeError("拍攝進行中，不能切換對局資料夾。")
            directory = (self.storage_root / game_id).resolve()
            if self.storage_root not in directory.parents:
                raise ValueError("對局資料夾路徑無效。")
            directory.mkdir(parents=True, exist_ok=True)
            self.capture_root = directory

    def arm(self, capture_id: str, filename_prefix: str | None = None) -> None:
        if not CAPTURE_ID_PATTERN.fullmatch(capture_id):
            raise ValueError("capture id 格式錯誤。")
        with self._condition:
            if self._active_id is not None:
                raise RuntimeError("已有雙視角拍攝正在等待中。")
            prefix = filename_prefix or capture_id
            if not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
                raise ValueError("拍攝檔名前綴格式錯誤。")
            if any(self.capture_root.glob(f"{prefix}_*")):
                raise FileExistsError(f"拍攝檔名已存在：{prefix}")
            self._active_id = capture_id
            self._filename_prefix = prefix
            self._paths = {}
            self._cancelled = False

    @staticmethod
    def _jpeg_size(data: bytes) -> tuple[int, int]:
        try:
            from PIL import Image
            with Image.open(BytesIO(data)) as image:
                if image.format != "JPEG":
                    raise ValueError("上傳內容不是 JPEG。")
                image.verify()
            with Image.open(BytesIO(data)) as image:
                return image.size
        except (OSError, ValueError) as exc:
            raise ValueError("JPEG 內容損壞或無法解析。") from exc

    def accept(
        self, remote_ip: str | None, mimetype: str, data: bytes
    ) -> tuple[str, str, tuple[int, int]]:
        if remote_ip != self.allowed_ip:
            raise PermissionError("來源 IP 不允許。")
        if mimetype.lower() not in ("image/jpeg", "image/jpg"):
            raise ValueError("只接受 JPEG multipart upload。")
        if not data or len(data) > self.max_bytes or not data.startswith(b"\xff\xd8\xff"):
            raise ValueError("JPEG 檔案為空、過大或格式不正確。")
        image_size = self._jpeg_size(data)
        with self._condition:
            if self._active_id is None:
                raise RuntimeError("目前沒有等待中的拍攝。")
            if len(self._paths) >= 2:
                raise RuntimeError("本次拍攝已收到兩張影像。")
            if self.expected_view_sizes:
                matches = [
                    view
                    for view, expected_size in self.expected_view_sizes.items()
                    if image_size == expected_size
                ]
                if len(matches) != 1:
                    expected = "、".join(
                        f"{view}={size[0]}×{size[1]}"
                        for view, size in sorted(self.expected_view_sizes.items())
                    )
                    raise ValueError(f"影像解析度 {image_size[0]}×{image_size[1]} 無法識別視角；預期 {expected}。")
                view = matches[0]
                if view in self._paths:
                    raise RuntimeError(f"本次拍攝已收到 {view}，拒絕重複視角。")
            else:
                view = f"v{len(self._paths) + 1}"
            if self._filename_prefix is None:
                raise RuntimeError("拍攝檔名尚未設定。")
            path = self.capture_root / f"{self._filename_prefix}_{view}.jpg"
            path.write_bytes(data)
            self._paths[view] = path
            capture_id = self._active_id
            LOGGER.info(
                "[VISION] frame accepted capture=%s view=%s size=%sx%s",
                capture_id,
                view,
                image_size[0],
                image_size[1],
            )
            self._condition.notify_all()
            return capture_id, view, image_size

    def wait(self, timeout: float) -> tuple[Path, Path]:
        with self._condition:
            first_ready = self._condition.wait_for(
                lambda: len(self._paths) >= 1 or self._closed or self._cancelled,
                timeout=timeout,
            )
            if not first_ready or len(self._paths) < 1:
                self._active_id = None
                self._filename_prefix = None
                self._paths = {}
                self._cancelled = False
                raise TimeoutError(f"等待 V1 影像逾時（{timeout:g} 秒）。")
            second_ready = self._condition.wait_for(
                lambda: len(self._paths) >= 2 or self._closed or self._cancelled,
                timeout=timeout,
            )
            if not second_ready or len(self._paths) < 2:
                self._active_id = None
                self._filename_prefix = None
                self._paths = {}
                self._cancelled = False
                raise TimeoutError(f"已收到一個視角，但等待另一個視角逾時（{timeout:g} 秒）。")
            if "v1" not in self._paths or "v2" not in self._paths:
                self._active_id = None
                self._filename_prefix = None
                self._paths = {}
                self._cancelled = False
                raise RuntimeError("雙視角影像身分不完整。")
            paths = (self._paths["v1"], self._paths["v2"])
            self._active_id = None
            self._filename_prefix = None
            self._paths = {}
            self._cancelled = False
            return paths

    def cancel(self) -> None:
        with self._condition:
            self._active_id = None
            self._filename_prefix = None
            self._paths = {}
            self._cancelled = True
            self._condition.notify_all()


class DualViewProcessor:
    def __init__(self, config: VisionConfig) -> None:
        if config.model_load_timeout_seconds <= 0 or config.inference_timeout_seconds <= 0:
            raise ValueError("YOLO 模型載入與推論逾時必須大於 0。")
        self.config = config
        self._model = None
        self._lock = threading.RLock()
        self._context = multiprocessing.get_context("spawn")
        self._process = None
        self._requests = None
        self._responses = None
        self._request_sequence = 0
        self._cancel_event = threading.Event()
        self._close_event = threading.Event()

    def check_model(self) -> None:
        if not self.config.model_path.is_file():
            raise FileNotFoundError(f"找不到 YOLO 模型：{self.config.model_path}")

    def _load_model(self):
        self.check_model()
        if self._model is None:
            settings_root = SITE_ROOT / "runtime" / "ultralytics"
            (settings_root / "Ultralytics").mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("YOLO_CONFIG_DIR", str(settings_root))
            try:
                from ultralytics import YOLO
            except ImportError as exc:
                raise RuntimeError("尚未安裝 ultralytics。") from exc
            model = YOLO(str(self.config.model_path))
            _validate_model_contract(model)
            # Publish the model only after its class contract is validated.  A
            # failed first load must not leave a partially accepted instance.
            self._model = model
        return self._model

    def _stop_worker_locked(self, *, force: bool) -> None:
        process = self._process
        requests = self._requests
        responses = self._responses
        self._process = self._requests = self._responses = None
        if process is not None:
            if process.is_alive() and not force and requests is not None:
                try:
                    requests.put_nowait(None)
                except (OSError, queue.Full, ValueError):
                    pass
                process.join(timeout=0.5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            process.close()
        for channel in (requests, responses):
            if channel is not None:
                try:
                    channel.cancel_join_thread()
                    channel.close()
                except (OSError, ValueError):
                    pass

    def _wait_worker_message_locked(self, timeout: float, *, operation: str) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            if self._close_event.is_set():
                self._stop_worker_locked(force=True)
                raise RuntimeError("YOLO 處理器已關閉。")
            if operation == "推論" and self._cancel_event.is_set():
                self._stop_worker_locked(force=True)
                raise RuntimeError("YOLO 推論已取消。")
            process = self._process
            responses = self._responses
            if process is None or responses is None:
                raise RuntimeError("YOLO 隔離程序尚未啟動。")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop_worker_locked(force=True)
                raise TimeoutError(f"YOLO {operation}超過 {timeout:g} 秒，已終止隔離程序。")
            try:
                return responses.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if not process.is_alive():
                    exit_code = process.exitcode
                    self._stop_worker_locked(force=True)
                    raise RuntimeError(f"YOLO 隔離程序異常結束（exit={exit_code}）。")

    def _start_worker_locked(self) -> None:
        self.check_model()
        if self._close_event.is_set():
            raise RuntimeError("YOLO 處理器已關閉。")
        if self._process is not None and self._process.is_alive():
            return
        self._stop_worker_locked(force=True)
        self._requests = self._context.Queue(maxsize=1)
        self._responses = self._context.Queue(maxsize=1)
        self._process = self._context.Process(
            target=_yolo_worker,
            args=(self.config, self._requests, self._responses),
            name="yolo-inference",
            daemon=True,
        )
        try:
            self._process.start()
            message = self._wait_worker_message_locked(
                self.config.model_load_timeout_seconds,
                operation="模型載入",
            )
        except Exception:
            self._stop_worker_locked(force=True)
            raise
        if message.get("kind") != "ready":
            self._stop_worker_locked(force=True)
            raise RuntimeError(
                "YOLO 模型載入失敗"
                f"（{message.get('error_type', 'Error')}）：{message.get('message', '未知錯誤')}"
            )

    def check_ready(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            self._start_worker_locked()

    def _detect_isolated_locked(
        self,
        image_path: Path,
        output_path: Path,
        threshold: float,
        save_output: bool,
    ) -> list[dict]:
        self._start_worker_locked()
        self._request_sequence += 1
        request_id = self._request_sequence
        assert self._requests is not None
        self._requests.put({
            "request_id": request_id,
            "image_path": str(image_path),
            "output_path": str(output_path),
            "threshold": threshold,
            "save_output": save_output,
        })
        while True:
            message = self._wait_worker_message_locked(
                self.config.inference_timeout_seconds,
                operation="推論",
            )
            if message.get("request_id") != request_id:
                continue
            if message.get("kind") == "error":
                raise RuntimeError(
                    "YOLO 推論失敗"
                    f"（{message.get('error_type', 'Error')}）：{message.get('message', '未知錯誤')}"
                )
            if message.get("kind") != "result" or not isinstance(message.get("detections"), list):
                raise RuntimeError("YOLO 隔離程序回傳無效結果。")
            return message["detections"]

    def _detect(
        self,
        image_path: Path,
        output_path: Path,
        *,
        confidence: float | None = None,
        save_output: bool = True,
    ) -> tuple[list[dict], tuple[int, int]]:
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError("尚未安裝 Pillow。") from exc
        with Image.open(image_path) as image:
            image.verify()
        with Image.open(image_path) as image:
            size = image.size
        threshold = self.config.confidence if confidence is None else confidence
        with self._lock:
            if self._model is not None:
                detections = _predict_with_model(
                    self._model,
                    str(image_path),
                    str(output_path),
                    threshold,
                    save_output,
                )
            else:
                detections = self._detect_isolated_locked(
                    image_path,
                    output_path,
                    threshold,
                    save_output,
                )
        return detections, size

    def cancel(self) -> None:
        self._cancel_event.set()

    def close(self) -> None:
        self._close_event.set()
        self._cancel_event.set()
        with self._lock:
            self._stop_worker_locked(force=True)

    def _v1_candidates(
        self, detections: list[dict], size: tuple[int, int]
    ) -> dict[tuple[int, int], list[dict]]:
        sx, sy = size[0] / self.config.v1_reference[0], size[1] / self.config.v1_reference[1]
        grid = {
            (row, col): (x * sx, y * sy)
            for row, y in enumerate(self.config.v1_row_y)
            for col, x in enumerate(self.config.v1_col_x)
        }
        limit = self.config.v1_max_distance * (sx + sy) / 2
        candidates: dict[tuple[int, int], list[dict]] = {}
        for detection in detections:
            cell, distance = self._nearest(detection["cx"], detection["cy"], grid)
            if distance <= limit and cell not in V2_MAIN_CELLS:
                candidates.setdefault(cell, []).append(detection)
        return candidates

    def _v2_candidates(
        self, detections: list[dict], size: tuple[int, int]
    ) -> dict[tuple[int, int], list[dict]]:
        sx, sy = size[0] / self.config.v2_reference[0], size[1] / self.config.v2_reference[1]
        grid = {
            (row, col): (x * sx, y * sy)
            for row, y in enumerate(self.config.v2_row_y)
            for col, x in enumerate(self.config.v2_col_x)
        }
        candidates: dict[tuple[int, int], list[dict]] = {}
        for detection in detections:
            local, _distance = self._nearest(detection["cx"], detection["cy"], grid)
            gx, gy = grid[local]
            if (
                abs(detection["cx"] - gx) <= self.config.v2_max_dx * sx
                and abs(detection["cy"] - gy) <= self.config.v2_max_dy * sy
            ):
                candidates.setdefault(V2_TO_MAIN[local], []).append(detection)
        return candidates

    @staticmethod
    def _nearest(cx: float, cy: float, grid: dict[tuple[int, int], tuple[float, float]]) -> tuple[tuple[int, int], float]:
        cell, point = min(grid.items(), key=lambda item: math.hypot(cx - item[1][0], cy - item[1][1]))
        return cell, math.hypot(cx - point[0], cy - point[1])

    def _map_v1_with_confidence(
        self, detections: list[dict], size: tuple[int, int]
    ) -> tuple[Board, ConfidenceGrid]:
        board = [[0 for _ in range(9)] for _ in range(10)]
        confidence: ConfidenceGrid = [[None for _ in range(9)] for _ in range(10)]
        for (row, col), choices in self._v1_candidates(detections, size).items():
            detection = max(choices, key=lambda item: item["confidence"])
            board[row][col] = detection["piece"]
            confidence[row][col] = detection["confidence"]
        return board, confidence

    def _map_v1(self, detections: list[dict], size: tuple[int, int]) -> Board:
        return self._map_v1_with_confidence(detections, size)[0]

    def _map_v2_with_confidence(
        self, detections: list[dict], size: tuple[int, int]
    ) -> dict[tuple[int, int], tuple[int, float]]:
        return {
            cell: (selected["piece"], selected["confidence"])
            for cell, choices in self._v2_candidates(detections, size).items()
            for selected in [max(choices, key=lambda item: item["confidence"])]
        }

    def _map_v2(self, detections: list[dict], size: tuple[int, int]) -> dict[tuple[int, int], int]:
        return {
            cell: piece
            for cell, (piece, _confidence) in self._map_v2_with_confidence(detections, size).items()
        }

    @staticmethod
    def _repair_piece_limit_misclassifications(
        board: Board,
        candidates: dict[tuple[int, int], list[dict]],
    ) -> list[tuple[tuple[int, int], int, int, float]]:
        """Use an alternate label only when the primary label is impossible.

        A correction must stay on the same occupied cell, keep the same color,
        and respect the hard Xiangqi piece-count limits. Missing detections and
        ordinary legal captures are never synthesized here.
        """
        counts = Counter(piece for row in board for piece in row if piece)
        corrections: list[tuple[tuple[int, int], int, int, float]] = []
        while True:
            over = [piece for piece, limit in PIECE_LIMITS.items() if counts[piece] > limit]
            if not over:
                return corrections
            changed = False
            for piece in over:
                cells = [
                    (row, col)
                    for row in range(10)
                    for col in range(9)
                    if board[row][col] == piece
                ]
                cells.sort(
                    key=lambda cell: max(
                        (
                            item["confidence"]
                            for item in candidates.get(cell, [])
                            if item["piece"] == piece
                        ),
                        default=0.0,
                    )
                )
                for row, col in cells:
                    alternatives = sorted(
                        (
                            item for item in candidates.get((row, col), [])
                            if item["piece"] != piece
                            and is_red(item["piece"]) == is_red(piece)
                            and counts[item["piece"]] < PIECE_LIMITS[item["piece"]]
                        ),
                        key=lambda item: item["confidence"],
                        reverse=True,
                    )
                    if not alternatives:
                        continue
                    replacement = alternatives[0]
                    board[row][col] = replacement["piece"]
                    counts[piece] -= 1
                    counts[replacement["piece"]] += 1
                    corrections.append(
                        ((row, col), piece, replacement["piece"], replacement["confidence"])
                    )
                    changed = True
                    break
            if not changed:
                return corrections

    def process(self, capture_id: str, v1_raw: Path, v2_raw: Path) -> CaptureResult:
        LOGGER.info("[VISION] processing started capture=%s", capture_id)
        directory = v1_raw.parent
        if not v1_raw.stem.endswith("_v1") or not v2_raw.stem.endswith("_v2"):
            raise ValueError("雙視角檔名格式錯誤。")
        prefix = v1_raw.stem[:-3]
        if v2_raw.stem[:-3] != prefix:
            raise ValueError("雙視角檔名前綴不一致。")
        v1_output = directory / f"{prefix}_v1_yolo.jpg"
        v2_output = directory / f"{prefix}_v2_yolo.jpg"
        with self._lock:
            if self._close_event.is_set():
                raise RuntimeError("YOLO 處理器已關閉。")
            self._cancel_event.clear()
            v1_detections, v1_size = self._detect(v1_raw, v1_output)
            v2_detections, v2_size = self._detect(v2_raw, v2_output)
        board, confidence = self._map_v1_with_confidence(v1_detections, v1_size)
        for row, col in V2_MAIN_CELLS:
            board[row][col] = 0
            confidence[row][col] = None
        for (row, col), (piece, score) in self._map_v2_with_confidence(v2_detections, v2_size).items():
            board[row][col] = piece
            confidence[row][col] = score
        validation_error = None
        try:
            validate_board(board)
        except ValueError as primary_error:
            fallback_v1, _ = self._detect(
                v1_raw,
                v1_output,
                confidence=self.config.fallback_confidence,
                save_output=False,
            )
            fallback_v2, _ = self._detect(
                v2_raw,
                v2_output,
                confidence=self.config.fallback_confidence,
                save_output=False,
            )
            candidates = self._v1_candidates(fallback_v1, v1_size)
            candidates.update(self._v2_candidates(fallback_v2, v2_size))
            corrections = self._repair_piece_limit_misclassifications(board, candidates)
            for (row, col), _old, _new, score in corrections:
                confidence[row][col] = score
            if corrections:
                LOGGER.warning(
                    "[VISION] corrected impossible piece counts: %s",
                    [
                        {
                            "cell": cell,
                            "from": PIECE_LABELS[old],
                            "to": PIECE_LABELS[new],
                            "confidence": round(confidence, 4),
                        }
                        for cell, old, new, confidence in corrections
                    ],
                )
            try:
                validate_board(board)
            except ValueError:
                validation_error = str(primary_error)
        result_path = self._write_result(
            directory,
            capture_id,
            board,
            confidence,
            filename=f"{prefix}_result.json",
        )
        LOGGER.info("[VISION] processing completed capture=%s", capture_id)
        result = CaptureResult(
            capture_id,
            board,
            v1_output,
            v2_output,
            confidence,
            result_path=result_path,
        )
        if validation_error:
            raise BoardObservationError(validation_error, result)
        return result

    @staticmethod
    def _write_result(
        directory: Path,
        capture_id: str,
        board: Board,
        confidence: ConfidenceGrid,
        filename: str = "result.json",
    ) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
            raise ValueError("辨識結果檔名格式錯誤。")
        target = directory / filename
        temporary = directory / f".{filename}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "capture_id": capture_id,
                    "processed_at": time.time(),
                    "board": board,
                    "confidence": confidence,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(target)
        return target


class VisionAdapter:
    def __init__(self, inbox: DualViewInbox, processor: DualViewProcessor, timeout: float) -> None:
        self.inbox = inbox
        self.processor = processor
        self.timeout = timeout
        self._filename_prefix: str | None = None

    def check_ready(self) -> None:
        if not self.inbox.receiver_ready:
            raise RuntimeError("TMvision receiver 尚未啟動。")
        self.processor.check_ready()

    def select_game(self, game_id: str) -> None:
        self.inbox.select_game(game_id)

    def configure_capture(self, round_number: int, sequence: int, purpose_code: str) -> str:
        if round_number < 1 or sequence < 1:
            raise ValueError("拍攝輪次與序號必須大於 0。")
        if not re.fullmatch(r"[a-z0-9_]+", purpose_code):
            raise ValueError("拍攝用途代碼格式錯誤。")
        self._filename_prefix = f"R{round_number:03d}_C{sequence:03d}_{purpose_code}"
        return self._filename_prefix

    def capture(self, capture_id: str, trigger: Callable[[], None]) -> CaptureResult:
        prefix = (
            f"{self._filename_prefix}_{capture_id}"
            if self._filename_prefix
            else capture_id
        )
        self._filename_prefix = None
        started = time.monotonic()
        self.inbox.arm(capture_id, prefix)
        try:
            trigger()
            v1_raw, v2_raw = self.inbox.wait(self.timeout)
            captured = time.monotonic()
            result = self.processor.process(capture_id, v1_raw, v2_raw)
            finished = time.monotonic()
            return replace(
                result,
                capture_seconds=round(captured - started, 3),
                processing_seconds=round(finished - captured, 3),
                total_seconds=round(finished - started, 3),
            )
        except Exception:
            self.inbox.cancel()
            raise

    def close(self) -> None:
        self.inbox.cancel()
        self.processor.close()

    def cancel(self) -> None:
        self.inbox.cancel()
        self.processor.cancel()
