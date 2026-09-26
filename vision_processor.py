import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Sequence, Tuple

from chess_board import Board, NAME_TO_ID, Position
from config import PROJECT_DIR, YOLO_CONFIDENCE, YOLO_MODEL_PATH

logger = logging.getLogger(__name__)


def find_player_move(
    prev_board: Sequence[Sequence[int]], curr_board: Sequence[Sequence[int]]
) -> Tuple[Position, Position]:
    if len(prev_board) != 10 or len(curr_board) != 10:
        raise ValueError("board must have 10 rows")
    changed = []
    for row in range(10):
        if len(prev_board[row]) != 9 or len(curr_board[row]) != 9:
            raise ValueError("board must have 9 columns")
        for col in range(9):
            before = prev_board[row][col]
            after = curr_board[row][col]
            if before != after:
                changed.append((col, row, before, after))

    from_candidates = [
        (col, row)
        for col, row, before, after in changed
        if before != 0 and after == 0
    ]
    to_candidates = [
        (col, row)
        for col, row, before, after in changed
        if after != 0 and (before == 0 or before != after)
    ]
    if len(from_candidates) == 1 and len(to_candidates) == 1:
        source, target = from_candidates[0], to_candidates[0]
        if prev_board[source[1]][source[0]] != curr_board[target[1]][target[0]]:
            raise ValueError("source and destination piece identities differ")
        return source, target

    # Robust matching against previous board when minor detection noise occurs
    matched = []
    for f_col, f_row in from_candidates:
        piece_type = prev_board[f_row][f_col]
        for t_col, t_row in to_candidates:
            if curr_board[t_row][t_col] == piece_type:
                matched.append(((f_col, f_row), (t_col, t_row)))

    if len(matched) == 1:
        return matched[0]

    raise ValueError(
        "ambiguous board difference: need one source and destination"
    )


def warp_board(image, corners):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError(
            "opencv-python is required for perspective correction"
        ) from exc
    points = np.asarray(corners, dtype="float32")
    if points.shape != (4, 2):
        raise ValueError("corners must contain TL, TR, BR, BL")
    destination = np.asarray(
        [[0, 0], [900, 0], [900, 1000], [0, 1000]], dtype="float32"
    )
    matrix = cv2.getPerspectiveTransform(points, destination)
    return cv2.warpPerspective(image, matrix, (900, 1000))


def read_image(image_path):
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("缺少影像套件，請安裝 requirements.txt。") from exc
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到棋盤圖片：{path}")
    # imread does not reliably support Chinese Windows paths across OpenCV versions.
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if image is None:
        raise ValueError(f"無法讀取圖片：{path}")
    return image


def detections_to_board(boxes, names, corners, red_at_bottom=True) -> Board:
    import cv2
    import numpy as np

    points = np.asarray(corners, dtype=np.float32)
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise ValueError("請依序選擇左上、右上、右下、左下四個最外側交叉點。")
    if not cv2.isContourConvex(points) or cv2.contourArea(points, oriented=True) <= 1:
        raise ValueError("四角順序或位置不正確，請重新選取。")
    if not (points[0, 0] < points[1, 0] and points[3, 0] < points[2, 0]
            and points[0, 1] < points[3, 1] and points[1, 1] < points[2, 1]):
        raise ValueError("請由圖片左上角起，按順時針方向選擇棋盤四角。")
    matrix = cv2.getPerspectiveTransform(
        points, np.asarray([[0, 0], [8, 0], [8, 9], [0, 9]], dtype=np.float32)
    )
    board: Board = [[0 for _ in range(9)] for _ in range(10)]
    for box in boxes:
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        point = np.asarray([[[(x1 + x2) / 2, (y1 + y2) / 2]]], dtype=np.float32)
        x, y = cv2.perspectiveTransform(point, matrix)[0, 0]
        if not np.isfinite([x, y]).all():
            raise ValueError("棋盤座標轉換失敗，請重新選擇四角。")
        if not (-0.38 <= x <= 8.38 and -0.38 <= y <= 9.38):
            continue  # Captured pieces outside the board are not active pieces.
        col, image_row = round(float(x)), round(float(y))
        if abs(x - col) > 0.38 or abs(y - image_row) > 0.38:
            raise ValueError("有棋子未對準交叉點，請確認棋盤四角或棋子位置。")
        row, col = (9 - image_row, col) if red_at_bottom else (image_row, 8 - col)
        class_id = int(box.cls[0])
        try:
            piece_id = NAME_TO_ID[names[class_id]]
        except (KeyError, IndexError) as exc:
            raise ValueError(f"模型包含未知棋子類別：{class_id}") from exc
        if board[row][col]:
            raise ValueError(f"同一交叉點出現多枚棋子（{col}, {row}），請檢查辨識。")
        board[row][col] = piece_id
    return board


class BoardDetector:
    def __init__(self, model_path=YOLO_MODEL_PATH):
        self.model_path = Path(model_path).resolve()
        self._model = None

    def _load_model(self):
        if self._model is None:
            if not self.model_path.is_file():
                raise FileNotFoundError(f"找不到 YOLO 模型：{self.model_path}")
            # Ultralytics checks the parent before creating its settings folder.
            # Create project-local cache parents before importing either library.
            for variable, folder in (("YOLO_CONFIG_DIR", PROJECT_DIR / ".runtime"),
                                     ("MPLCONFIGDIR", PROJECT_DIR / ".runtime" / "matplotlib")):
                if variable not in os.environ:
                    folder.mkdir(parents=True, exist_ok=True)
                    os.environ[variable] = str(folder)
            try:
                from ultralytics import YOLO
            except ImportError as exc:
                raise RuntimeError("缺少 YOLO 套件，請安裝 requirements.txt。") from exc
            model = YOLO(str(self.model_path), task="detect")
            names = model.names.values() if isinstance(model.names, dict) else model.names
            if set(names) != set(NAME_TO_ID):
                raise ValueError("YOLO 模型的棋子類別與本專案的 14 類定義不符。")
            self._model = model
            logger.info("[YOLO] loaded: %s (14 classes)", self.model_path)
        return self._model

    def detect(self, image_path, corners, red_at_bottom=True) -> Board:
        image = read_image(image_path)
        return self.detect_image(image, corners, red_at_bottom)

    def detect_image(self, image, corners, red_at_bottom=True) -> Board:
        if corners is None:
            raise ValueError("辨識前請先選擇棋盤的四個最外側交叉點。")
        model = self._load_model()
        result = model.predict(
            source=image, conf=YOLO_CONFIDENCE, device="cpu", imgsz=640,
            verbose=False, save=False,
        )
        board = detections_to_board(result[0].boxes, model.names, corners, red_at_bottom)
        logger.info("[VISION] board produced: %s pieces", sum(bool(p) for row in board for p in row))
        return board


@lru_cache(maxsize=1)
def _detector(model_path):
    return BoardDetector(model_path)


def detect_board(image_path: str, model_path=YOLO_MODEL_PATH, *, corners=None,
                 red_at_bottom=True) -> Board:
    return _detector(str(Path(model_path).resolve())).detect(image_path, corners, red_at_bottom)
