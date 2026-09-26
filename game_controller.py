"""Game-turn state machine independent from Tkinter and TMflow transport."""

from enum import Enum
from typing import Callable, Optional

from chess_board import Board, Position
from chess_pipeline import MoveSuggestion
from vision_processor import find_player_move


class Turn(Enum):
    PLAYER = "PLAYER"
    ROBOT = "ROBOT"
    FINISHED = "FINISHED"


class GameController:
    """Coordinate turns after the initial board image has been captured."""

    def __init__(
        self,
        suggest_move: Callable[[Board, str, int], MoveSuggestion],
        execute_move: Callable[[Position, Position, Optional[int]], None],
        depth: int = 4,
    ) -> None:
        self.suggest_move = suggest_move
        self.execute_move = execute_move
        self.depth = depth
        self.turn = Turn.FINISHED
        self.side = "w"
        self.player_side = "w"
        self.robot_side = "b"
        self.captured_count = 0
        self.board: Optional[Board] = None

    def start(
        self,
        board: Board,
        first_player: str,
        robot_side: Optional[str] = None,
        player_side: Optional[str] = None,
        first_color: Optional[str] = None,
    ) -> Turn:
        if first_player not in (Turn.PLAYER.value, Turn.ROBOT.value):
            raise ValueError("first_player must be PLAYER or ROBOT")
        if robot_side is not None and robot_side not in ("w", "b"):
            raise ValueError("robot_side must be w or b")
        if player_side is not None and player_side not in ("w", "b"):
            raise ValueError("player_side must be w or b")

        if player_side is not None:
            self.player_side = player_side
            self.robot_side = "b" if player_side == "w" else "w"
        elif robot_side is not None:
            self.robot_side = robot_side
            self.player_side = "b" if robot_side == "w" else "w"
        elif first_player == Turn.PLAYER.value:
            self.player_side = "w"
            self.robot_side = "b"
        else:
            self.robot_side = "w"
            self.player_side = "b"

        self.board = [row[:] for row in board]
        self.turn = Turn.PLAYER if first_player == Turn.PLAYER.value else Turn.ROBOT
        if first_color is not None:
            self.side = first_color
        else:
            self.side = self.player_side if self.turn is Turn.PLAYER else self.robot_side
        self.captured_count = 0
        return self.turn

    def robot_turn(self) -> MoveSuggestion:
        if self.turn is not Turn.ROBOT or self.board is None:
            raise RuntimeError("robot is not waiting for a turn")
        suggestion = self.suggest_move(self.board, self.robot_side, self.depth)
        if suggestion.source is None or suggestion.destination is None:
            self.turn = Turn.FINISHED
            return suggestion
        if suggestion.captured_piece:
            self.captured_count += 1
        captured_count = self.captured_count if suggestion.captured_piece else None
        self.execute_move(suggestion.source, suggestion.destination, captured_count)
        self._apply(suggestion)
        self.turn = Turn.PLAYER
        self.side = self.player_side
        return suggestion

    def player_finished(self, current_board: Board) -> MoveSuggestion:
        if self.turn is not Turn.PLAYER or self.board is None:
            raise RuntimeError("it is not the player's turn")
        source, destination = find_player_move(self.board, current_board)
        piece = self.board[source[1]][source[0]]
        is_red = piece >= 8
        expected_red = (self.player_side == "w")
        if is_red != expected_red:
            color_name = "紅方" if expected_red else "黑方"
            raise ValueError(f"請移動己方棋子（{color_name}）。")

        self.board[source[1]][source[0]] = 0
        self.board[destination[1]][destination[0]] = piece
        self.turn = Turn.ROBOT
        self.side = self.robot_side
        suggestion = self.suggest_move(self.board, self.robot_side, self.depth)
        if suggestion.source is None or suggestion.destination is None:
            self.turn = Turn.FINISHED
            return suggestion
        if suggestion.captured_piece:
            self.captured_count += 1
        captured_count = self.captured_count if suggestion.captured_piece else None
        self.execute_move(suggestion.source, suggestion.destination, captured_count)
        self._apply(suggestion)
        self.turn = Turn.PLAYER
        self.side = self.player_side
        return suggestion

    def _apply(self, suggestion: MoveSuggestion) -> None:
        assert self.board is not None
        piece = self.board[suggestion.source[1]][suggestion.source[0]]
        self.board[suggestion.source[1]][suggestion.source[0]] = 0
        self.board[suggestion.destination[1]][suggestion.destination[0]] = piece