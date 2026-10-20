"""Read-only reconstruction for reports. Never authorizes robot motion."""
from .board import Move, apply_move, board_to_fen, clone_board, is_red, parse_uci_move, validate_board


def audit_history(payload):
    frames, issues = [], []
    try:
        board = payload.get("initial_board")
        if board is None:
            return [], ["缺少初始盤面，無法重建逐步棋盤。"]
        validate_board(board)
        board = clone_board(board)
        side = payload.get("initial_side_to_move")
        board_to_fen(board, side)
        frames.append({"sequence": 0, "board": clone_board(board), "side": side, "uci": "初始盤面"})
        history = payload.get("move_history")
        if not isinstance(history, list):
            raise ValueError("缺少著法列表。")
        for sequence, item in enumerate(history, 1):
            source, destination = parse_uci_move(item["uci"])
            piece = board[source[1]][source[0]]
            if not piece or is_red(piece) != (side == "w"):
                raise ValueError(f"第 {sequence} 步起點棋子與應行棋方不符。")
            if item.get("side") not in (None, side):
                issues.append(f"第 {sequence} 步棋色欄與重播不一致。")
            before = board_to_fen(board, side)
            if item.get("fen_before") and item["fen_before"] != before:
                issues.append(f"第 {sequence} 步 FEN 前不一致。")
            captured = board[destination[1]][destination[0]]
            if item.get("captured_piece_id") not in (None, captured):
                issues.append(f"第 {sequence} 步被吃棋子不一致。")
            board = apply_move(board, Move(source, destination, captured, item["uci"]))
            side = "b" if side == "w" else "w"
            after = board_to_fen(board, side)
            if item.get("fen_after") and item["fen_after"] != after:
                issues.append(f"第 {sequence} 步 FEN 後不一致。")
            frames.append({"sequence": sequence, "board": clone_board(board), "side": side,
                           "uci": item["uci"], "source": source, "destination": destination})
        if board != payload.get("board"):
            issues.append("歷史重播的最終盤面與保存盤面不一致。")
        if side != payload.get("side_to_move"):
            issues.append("歷史重播的行棋方與保存行棋方不一致。")
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        issues.append(f"重播中止：{exc}")
    return frames, issues
