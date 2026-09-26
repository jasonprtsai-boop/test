"""Image recognition and engine analysis share one canonical board format."""

from dataclasses import dataclass
from typing import Optional

from chess_board import Board, Position, board_to_fen, parse_uci_move
from chess_engine import PikafishEngine
from config import ENGINE_TIME_LIMIT_MS
from vision_processor import BoardDetector


@dataclass(frozen=True)
class MoveSuggestion:
    fen: str
    move: str
    source: Optional[Position]
    destination: Optional[Position]
    captured_piece: int = 0


class ChessPipeline:
    def __init__(self, detector=None, engine=None, persistent_engine: bool = False):
        self.detector = detector if detector is not None else BoardDetector()
        self.engine = engine if engine is not None else PikafishEngine()
        self.persistent_engine = persistent_engine

    def recognize(self, image_path, corners, red_at_bottom=True) -> Board:
        return self.detector.detect(image_path, corners, red_at_bottom)

    def recognize_image(self, image, corners, red_at_bottom=True) -> Board:
        return self.detector.detect_image(image, corners, red_at_bottom)

    def suggest_move(self, board: Board, side="w", depth=4) -> MoveSuggestion:
        fen = board_to_fen(board, side)
        try:
            self.engine.start()
            move = self.engine.best_move(f"fen {fen}", depth, ENGINE_TIME_LIMIT_MS)
        finally:
            if not self.persistent_engine:
                self.engine.stop()
        if move in ("(none)", "0000"):
            return MoveSuggestion(fen, move, None, None)
        source, destination = parse_uci_move(move)
        piece = board[source[1]][source[0]]
        captured = board[destination[1]][destination[0]]
        if piece == 0 or (piece >= 8) != (side == "w"):
            raise ValueError("引擎著法起點與辨識盤面不符。")
        if captured and (captured >= 8) == (piece >= 8):
            raise ValueError("引擎著法終點被同色棋子佔用。")
        return MoveSuggestion(fen, move, source, destination, captured)

    def close(self) -> None:
        self.engine.stop()
