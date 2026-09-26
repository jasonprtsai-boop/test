"""Shared board format: board[row][col], row 0 at Red's home rank."""

import re
from collections import Counter
from typing import List, Sequence, Tuple

Board = List[List[int]]
Position = Tuple[int, int]

# Canonical IDs are the supplied best.pt class IDs + 1; zero means empty.
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


def validate_board(board: Sequence[Sequence[int]]) -> None:
    """Reject malformed recognition, not a substitute for a rules engine."""
    if len(board) != 10 or any(len(row) != 9 for row in board):
        raise ValueError("盤面必須是 10 列 × 9 行。")
    pieces = [piece for row in board for piece in row]
    if any(type(piece) is not int or piece not in range(15) for piece in pieces):
        raise ValueError("盤面含有未知棋子類別。")
    counts = Counter(pieces)
    for piece, symbol in PIECE_TO_FEN.items():
        limit = 1 if symbol.lower() == "k" else 5 if symbol.lower() == "p" else 2
        if counts[piece] > limit:
            raise ValueError(f"{PIECE_LABELS[piece]} 數量異常，請檢查辨識結果。")
    for piece, ranks in ((13, range(3)), (6, range(7, 10))):
        if counts[piece] != 1:
            raise ValueError("必須各辨識到一枚紅帥與黑將，請確認圖片、四角與方向。")
        if not any(board[row][col] == piece for row in ranks for col in range(3, 6)):
            raise ValueError("將帥不在對應九宮內，請確認紅方位置及棋盤四角。")
    red = next((col, row) for row in range(10) for col in range(9) if board[row][col] == 13)
    black = next((col, row) for row in range(10) for col in range(9) if board[row][col] == 6)
    if red[0] == black[0] and not any(board[row][red[0]] for row in range(red[1] + 1, black[1])):
        raise ValueError("將帥直接照面，請檢查是否漏辨識棋子。")


def board_to_fen(board: Sequence[Sequence[int]], side: str = "w") -> str:
    validate_board(board)
    if side not in ("w", "b"):
        raise ValueError("side 必須是 w（紅方）或 b（黑方）。")
    ranks = []
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


def parse_uci_move(move: str) -> Tuple[Position, Position]:
    if not re.fullmatch(r"[a-i][0-9][a-i][0-9]", move):
        raise ValueError(f"無法解析 Pikafish 著法：{move}")
    start = (ord(move[0]) - ord("a"), int(move[1]))
    end = (ord(move[2]) - ord("a"), int(move[3]))
    if start == end:
        raise ValueError("著法起點與終點相同。")
    return start, end
