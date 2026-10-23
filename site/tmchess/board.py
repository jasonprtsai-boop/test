"""Canonical board representation and transactional move helpers.

board[row][column], with row 0 at Red's home rank.  Piece IDs match best.pt
class order plus one; zero is empty.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

Board = list[list[int]]
Position = tuple[int, int]

PIECE_NAMES = (
    "chess-black-cannon", "chess-black-car", "chess-black-elephant",
    "chess-black-guard", "chess-black-horse", "chess-black-king",
    "chess-black-soldier", "chess-red-cannon", "chess-red-car",
    "chess-red-elephant", "chess-red-guard", "chess-red-horse",
    "chess-red-king", "chess-red-soldier",
)
NAME_TO_ID = {name: index + 1 for index, name in enumerate(PIECE_NAMES)}
PIECE_TO_FEN = dict(enumerate("crbankpCRBANKP", start=1))
PIECE_LABELS = dict(enumerate("砲車象士馬將卒炮俥相仕傌帥兵", start=1))
PIECE_LIMITS = {
    piece: 1 if symbol.lower() == "k" else 5 if symbol.lower() == "p" else 2
    for piece, symbol in PIECE_TO_FEN.items()
}


@dataclass(frozen=True)
class Move:
    source: Position
    destination: Position
    captured_piece: int = 0
    uci: str = ""


def clone_board(board: Sequence[Sequence[int]]) -> Board:
    return [list(row) for row in board]


def standard_starting_board() -> Board:
    """Return the canonical physical starting position used before the first capture."""
    board = [[0 for _ in range(9)] for _ in range(10)]
    board[0] = [9, 12, 10, 11, 13, 11, 10, 12, 9]
    board[2][1] = board[2][7] = 8
    for col in (0, 2, 4, 6, 8):
        board[3][col] = 14
        board[6][col] = 7
    board[7][1] = board[7][7] = 1
    board[9] = [2, 5, 3, 4, 6, 4, 3, 5, 2]
    return board


def is_red(piece: int) -> bool:
    return piece >= 8


def validate_board(board: Sequence[Sequence[int]]) -> None:
    if len(board) != 10 or any(len(row) != 9 for row in board):
        raise ValueError("盤面必須是 10 列 × 9 行。")
    pieces = [piece for row in board for piece in row]
    if any(type(piece) is not int or piece not in range(15) for piece in pieces):
        raise ValueError("盤面含有未知棋子類別。")
    counts = Counter(pieces)
    for piece, limit in PIECE_LIMITS.items():
        if counts[piece] > limit:
            cells = ", ".join(
                chr(ord("a") + col) + str(row)
                for row in range(10) for col in range(9) if board[row][col] == piece
            )
            raise ValueError(
                f"{PIECE_LABELS[piece]} 辨識到 {counts[piece]} 枚，超過最多 {limit} 枚"
                f"（格位：{cells}）。吃子後數量減少可以接受；請核對多辨識或錯分類的棋子。"
            )
    for piece, ranks in ((13, range(3)), (6, range(7, 10))):
        if counts[piece] != 1:
            raise ValueError("必須各辨識到一枚紅帥與黑將。")
        if not any(board[row][col] == piece for row in ranks for col in range(3, 6)):
            raise ValueError("將帥不在對應九宮內，請確認棋盤方向與辨識結果。")
    red = next((col, row) for row in range(10) for col in range(9) if board[row][col] == 13)
    black = next((col, row) for row in range(10) for col in range(9) if board[row][col] == 6)
    if red[0] == black[0] and not any(board[row][red[0]] for row in range(red[1] + 1, black[1])):
        raise ValueError("將帥直接照面，請檢查是否漏辨識棋子。")


def validate_pawn_positions(board: Sequence[Sequence[int]]) -> None:
    """Reject unreachable pawn squares before adopting a new baseline.

    Domain row 0 is Red's home rank. Pawns cannot retreat or move sideways
    before crossing the river; this does not establish full position legality.
    """
    validate_board(board)
    invalid = []
    for row in range(10):
        for col in range(9):
            piece = board[row][col]
            if ((piece == 14 and (row < 3 or (row < 5 and col % 2)))
                    or (piece == 7 and (row > 6 or (row > 4 and col % 2)))):
                invalid.append(f"{chr(97 + col)}{row}（{PIECE_LABELS[piece]}）")
    if invalid:
        raise ValueError("兵／卒位於不可到達的格位：" + ", ".join(invalid) + "。請核對影像與實體盤面。")


def board_to_fen(board: Sequence[Sequence[int]], side: str) -> str:
    validate_board(board)
    if side not in ("w", "b"):
        raise ValueError("side 必須是 w（紅）或 b（黑）。")
    ranks: list[str] = []
    for row in reversed(board):
        rank, empty = "", 0
        for piece in row:
            if piece == 0:
                empty += 1
            else:
                if empty:
                    rank += str(empty)
                    empty = 0
                rank += PIECE_TO_FEN[piece]
        ranks.append(rank + (str(empty) if empty else ""))
    return "/".join(ranks) + f" {side} - - 0 1"


def position_to_uci(position: Position) -> str:
    return chr(ord("a") + position[0]) + str(position[1])


def parse_uci_move(value: str) -> tuple[Position, Position]:
    if not re.fullmatch(r"[a-i][0-9][a-i][0-9]", value):
        raise ValueError(f"無法解析 Pikafish 著法：{value}")
    source = (ord(value[0]) - ord("a"), int(value[1]))
    destination = (ord(value[2]) - ord("a"), int(value[3]))
    if source == destination:
        raise ValueError("著法起點與終點相同。")
    return source, destination


def apply_move(board: Sequence[Sequence[int]], move: Move) -> Board:
    result = clone_board(board)
    sx, sy = move.source
    dx, dy = move.destination
    piece = result[sy][sx]
    if piece == 0:
        raise ValueError("著法起點沒有棋子。")
    target = result[dy][dx]
    if target and is_red(target) == is_red(piece):
        raise ValueError("著法終點已有己方棋子。")
    result[sy][sx] = 0
    result[dy][dx] = piece
    return result


def find_player_move(before: Sequence[Sequence[int]], after: Sequence[Sequence[int]], side: str) -> Move:
    validate_board(before)
    validate_board(after)
    changed = [(col, row) for row in range(10) for col in range(9) if before[row][col] != after[row][col]]
    if len(changed) != 2:
        raise ValueError("一次只能移動一枚棋子，且盤面必須只變更起點與終點。")
    sources = [(col, row) for col, row in changed if before[row][col] and after[row][col] == 0]
    if len(sources) != 1:
        raise ValueError("無法判定玩家走子的起點。")
    source = sources[0]
    moved_piece = before[source[1]][source[0]]
    destinations = [
        (col, row) for col, row in changed
        if (col, row) != source and after[row][col] == moved_piece
    ]
    if len(destinations) != 1:
        raise ValueError("無法判定玩家走子的終點。")
    destination = destinations[0]
    expected_red = side == "w"
    if is_red(moved_piece) != expected_red:
        raise ValueError("玩家移動了非己方棋子。")
    captured = before[destination[1]][destination[0]]
    if captured and is_red(captured) == expected_red:
        raise ValueError("不可吃掉己方棋子。")
    move = Move(source, destination, captured, position_to_uci(source) + position_to_uci(destination))
    if apply_move(before, move) != clone_board(after):
        raise ValueError("辨識盤面與單一步驟不一致。")
    return move
