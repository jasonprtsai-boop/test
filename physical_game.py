"""Blocking physical-game workflow intended to run in a background thread."""

from typing import Optional

from chess_board import Board
from chess_pipeline import ChessPipeline, MoveSuggestion
from config import CAMERA_IMAGE_TIMEOUT
from game_controller import GameController, Turn


class PhysicalGameSession:
    def __init__(
        self,
        pipeline: ChessPipeline,
        robot,
        camera,
        camera_corners,
        red_at_bottom: bool = True,
        depth: int = 4,
        image_timeout: float = CAMERA_IMAGE_TIMEOUT,
    ) -> None:
        if camera_corners is None:
            raise ValueError("請先在 config.py 設定 CAMERA_CORNERS。")
        self.pipeline = pipeline
        self.robot = robot
        self.camera = camera
        self.camera_corners = camera_corners
        self.red_at_bottom = red_at_bottom
        self.image_timeout = image_timeout
        self.game = GameController(
            pipeline.suggest_move,
            robot.execute_chess_move,
            depth,
        )
        self.pending_move: Optional[MoveSuggestion] = None
        self.last_move: Optional[MoveSuggestion] = None
        if hasattr(self.pipeline, "persistent_engine"):
            self.pipeline.persistent_engine = True

    def _capture_board(self) -> Board:
        if hasattr(self.camera, "clear"):
            self.camera.clear()
        self.robot.trigger_capture()
        image = self.camera.wait_for_image(self.image_timeout)
        return self.pipeline.recognize_image(
            image, self.camera_corners, self.red_at_bottom
        )

    def _verify_robot_board(self) -> None:
        actual = self._capture_board()
        if actual != self.game.board:
            raise ValueError("手臂落子後盤面與預期不符，請檢查棋子位置。")

    def start(self, first_player: str, player_side: Optional[str] = None) -> Turn:
        initial_board = self._capture_board()
        turn = self.game.start(initial_board, first_player, player_side=player_side)
        self.last_move = None
        if turn is Turn.ROBOT:
            suggestion = self.game.robot_turn()
            self.pending_move = suggestion
            self._verify_robot_board()
            self.last_move = suggestion
            self.pending_move = None
        return self.game.turn

    def player_finished(self) -> MoveSuggestion:
        current_board = self._capture_board()
        suggestion = self.game.player_finished(current_board)
        if suggestion.source is not None:
            self.pending_move = suggestion
            self._verify_robot_board()
            self.last_move = suggestion
            self.pending_move = None
        return suggestion

    def retry_last_move(self) -> MoveSuggestion:
        if self.pending_move is None:
            raise RuntimeError("目前沒有可重試的落子。")
        suggestion = self.pending_move
        captured_count = (
            self.game.captured_count if suggestion.captured_piece else None
        )
        self.robot.execute_chess_move(
            suggestion.source, suggestion.destination, captured_count
        )
        self._verify_robot_board()
        self.last_move = suggestion
        self.pending_move = None
        return suggestion

    def close(self) -> None:
        self.camera.stop()
        self.robot.close()
        if hasattr(self.pipeline, "close"):
            self.pipeline.close()