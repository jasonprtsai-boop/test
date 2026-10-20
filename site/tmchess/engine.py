"""Pikafish UCI adapter with bounded waits and explicit legality checking."""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .board import Board, Move, apply_move, board_to_fen, clone_board, parse_uci_move
from .config import EngineConfig


@dataclass(frozen=True)
class AnalysisLine:
    multipv: int
    depth: int
    seldepth: int | None
    score_type: str
    score: int
    bound: str | None
    wdl: tuple[int, int, int] | None
    nodes: int | None
    nps: int | None
    time_ms: int | None
    pv: tuple[str, ...]


@dataclass(frozen=True)
class EngineAnalysis:
    bestmove: Move | None
    ponder: str | None
    lines: tuple[AnalysisLine, ...]


def parse_analysis_line(value: str) -> AnalysisLine | None:
    """Parse one complete UCI ``info`` line emitted for a principal variation."""
    parts = value.split()
    if not parts or parts[0] != "info" or "score" not in parts or "pv" not in parts:
        return None

    def integer_after(name: str) -> int | None:
        try:
            return int(parts[parts.index(name) + 1])
        except (ValueError, IndexError):
            return None

    depth = integer_after("depth")
    multipv = integer_after("multipv") or 1
    score_index = parts.index("score")
    try:
        score_type = parts[score_index + 1]
        score = int(parts[score_index + 2])
    except (ValueError, IndexError):
        return None
    if depth is None or score_type not in ("cp", "mate"):
        return None

    bound = None
    if score_index + 3 < len(parts) and parts[score_index + 3] in ("lowerbound", "upperbound"):
        bound = parts[score_index + 3]

    wdl = None
    if "wdl" in parts:
        wdl_index = parts.index("wdl")
        try:
            candidate = tuple(int(item) for item in parts[wdl_index + 1:wdl_index + 4])
        except ValueError:
            candidate = ()
        if len(candidate) == 3 and sum(candidate) == 1000 and all(item >= 0 for item in candidate):
            wdl = candidate

    pv_index = parts.index("pv")
    pv = tuple(parts[pv_index + 1:])
    if not pv:
        return None
    return AnalysisLine(
        multipv=multipv,
        depth=depth,
        seldepth=integer_after("seldepth"),
        score_type=score_type,
        score=score,
        bound=bound,
        wdl=wdl,
        nodes=integer_after("nodes"),
        nps=integer_after("nps"),
        time_ms=integer_after("time"),
        pv=pv,
    )


class PikafishEngine:
    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.process: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._search_lock = threading.RLock()
        self.last_analysis: EngineAnalysis | None = None

    def check_files(self) -> None:
        if not self.config.executable.is_file():
            raise FileNotFoundError(f"找不到 Pikafish：{self.config.executable}")
        if not self.config.nnue.is_file() or self.config.nnue.stat().st_size == 0:
            raise FileNotFoundError(f"找不到 Pikafish NNUE：{self.config.nnue}")

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.stop()
        self.check_files()
        self._lines = queue.Queue()
        self.process = subprocess.Popen(
            [str(self.config.executable)], cwd=str(self.config.executable.parent),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert self.process.stdout is not None
        self._reader = threading.Thread(target=self._read_output, args=(self.process.stdout,), daemon=True)
        self._reader.start()
        try:
            self._send("uci")
            self._wait_for("uciok")
            net = self.config.nnue.name if self.config.nnue.parent == self.config.executable.parent else str(self.config.nnue)
            self._send(f"setoption name EvalFile value {net}")
            self._send("isready")
            self._wait_for("readyok")
        except Exception:
            self.stop()
            raise

    def _read_output(self, stream) -> None:
        try:
            for line in stream:
                self._lines.put(line.strip())
        finally:
            self._lines.put(None)

    def _send(self, command: str) -> None:
        if "\n" in command or "\r" in command:
            raise ValueError("UCI 指令不可含換行。")
        if self.process is None or self.process.stdin is None or self.process.poll() is not None:
            raise RuntimeError("Pikafish 尚未啟動或已退出。")
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def _wait_for(self, marker: str) -> str:
        deadline = time.monotonic() + self.config.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Pikafish 等待 {marker} 逾時。")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"Pikafish 等待 {marker} 逾時。") from exc
            if line is None:
                raise RuntimeError(f"Pikafish 在回傳 {marker} 前退出。")
            if line.startswith("info string ERROR:"):
                raise RuntimeError(line.removeprefix("info string ERROR:").strip())
            if line.split()[:1] == [marker]:
                return line

    @staticmethod
    def _position_command(
        board: Board,
        side: str,
        *,
        initial_board: Board | None = None,
        initial_side: str | None = None,
        moves: Sequence[str] = (),
    ) -> str:
        """Build a UCI position and prove persisted history matches the board."""
        if not moves:
            return f"position fen {board_to_fen(board, side)}"
        if initial_board is None or initial_side not in ("w", "b"):
            raise ValueError("完整著法歷史缺少初始盤面或起始行棋方。")

        replay = clone_board(initial_board)
        replay_side = initial_side
        normalized: list[str] = []
        for value in moves:
            value = str(value)
            source, destination = parse_uci_move(value)
            piece = replay[source[1]][source[0]]
            captured = replay[destination[1]][destination[0]]
            if piece == 0 or (piece >= 8) != (replay_side == "w"):
                raise ValueError(f"著法歷史與盤面不一致：{value}")
            if captured and (captured >= 8) == (piece >= 8):
                raise ValueError(f"著法歷史終點被同色棋子佔用：{value}")
            replay = apply_move(replay, Move(source, destination, captured, value))
            normalized.append(value)
            replay_side = "b" if replay_side == "w" else "w"
        if replay != board or replay_side != side:
            raise ValueError("完整著法歷史無法重建目前盤面與行棋方。")
        return (
            f"position fen {board_to_fen(initial_board, initial_side)} moves "
            + " ".join(normalized)
        )

    def _search(
        self,
        position_command: str,
        suffix: str,
        *,
        multipv: int = 1,
    ) -> tuple[str, str | None, tuple[AnalysisLine, ...]]:
        try:
            self.start()
            self._send("ucinewgame")
            self._send(f"setoption name MultiPV value {multipv}")
            self._send("isready")
            self._wait_for("readyok")
            self._send(position_command)
            self._send(suffix)
            deadline = time.monotonic() + self.config.timeout_seconds
            observed: dict[tuple[int, int], AnalysisLine] = {}
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Pikafish 等待 bestmove 逾時。")
                try:
                    line = self._lines.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError("Pikafish 等待 bestmove 逾時。") from exc
                if line is None:
                    raise RuntimeError("Pikafish 在回傳 bestmove 前退出。")
                if line.startswith("info string ERROR:"):
                    raise RuntimeError(line.removeprefix("info string ERROR:").strip())
                parsed = parse_analysis_line(line)
                if parsed is not None:
                    observed[(parsed.depth, parsed.multipv)] = parsed
                if not line.startswith("bestmove "):
                    continue
                parts = line.split()
                if len(parts) < 2:
                    raise RuntimeError("Pikafish 回傳不完整的 bestmove。")
                bestmove = parts[1]
                ponder = parts[3] if len(parts) >= 4 and parts[2] == "ponder" else None
                break
        except Exception:
            # A delayed bestmove from a failed search must never enter the next
            # position. Restarting also replaces the reader queue in start().
            self.stop()
            raise

        lines: tuple[AnalysisLine, ...] = ()
        if observed:
            primary_depths = [depth for depth, index in observed if index == 1]
            if primary_depths:
                final_depth = max(primary_depths)
                lines = tuple(
                    observed[key]
                    for key in sorted(observed)
                    if key[0] == final_depth
                )
        return bestmove, ponder, lines

    @staticmethod
    def _move_from_uci(board: Board, side: str, value: str) -> Move | None:
        if value in ("(none)", "0000"):
            return None
        source, destination = parse_uci_move(value)
        piece = board[source[1]][source[0]]
        captured = board[destination[1]][destination[0]]
        if piece == 0 or (piece >= 8) != (side == "w"):
            raise ValueError("引擎著法起點與盤面不符。")
        if captured and (captured >= 8) == (piece >= 8):
            raise ValueError("引擎著法終點被同色棋子佔用。")
        return Move(source, destination, captured, value)

    def analyze(
        self,
        board: Board,
        side: str,
        *,
        multipv: int = 1,
        initial_board: Board | None = None,
        initial_side: str | None = None,
        moves: Sequence[str] = (),
    ) -> EngineAnalysis:
        if side not in ("w", "b"):
            raise ValueError("Pikafish 分析方必須是 w 或 b。")
        if multipv < 1 or multipv > 128:
            raise ValueError("Pikafish MultiPV 必須介於 1 到 128。")
        position = self._position_command(
            board,
            side,
            initial_board=initial_board,
            initial_side=initial_side,
            moves=moves,
        )
        with self._search_lock:
            value, ponder, lines = self._search(
                position,
                f"go depth {self.config.depth} movetime {self.config.movetime_ms}",
                multipv=multipv,
            )
        analysis = EngineAnalysis(self._move_from_uci(board, side, value), ponder, lines)
        self.last_analysis = analysis
        return analysis

    def is_legal(
        self,
        board: Board,
        side: str,
        move: Move,
        *,
        initial_board: Board | None = None,
        initial_side: str | None = None,
        moves: Sequence[str] = (),
    ) -> bool:
        value = move.uci
        position = self._position_command(
            board,
            side,
            initial_board=initial_board,
            initial_side=initial_side,
            moves=moves,
        )
        with self._search_lock:
            best, _ponder, _lines = self._search(
                position, f"go depth 1 searchmoves {value}"
            )
        return best == value

    def suggest(
        self,
        board: Board,
        side: str,
        *,
        initial_board: Board | None = None,
        initial_side: str | None = None,
        moves: Sequence[str] = (),
    ) -> Move | None:
        return self.analyze(
            board,
            side,
            initial_board=initial_board,
            initial_side=initial_side,
            moves=moves,
        ).bestmove

    def has_legal_move(
        self,
        board: Board,
        side: str,
        *,
        initial_board: Board | None = None,
        initial_side: str | None = None,
        moves: Sequence[str] = (),
    ) -> bool:
        """Return whether the side to move has any legal move.

        Under Xiangqi rules used by Pikafish, having no legal move ends the
        game. This covers checkmate and stalemate. Supplying the committed move
        history also lets Pikafish apply its repetition, chase and rule-60 state.
        """
        position = self._position_command(
            board,
            side,
            initial_board=initial_board,
            initial_side=initial_side,
            moves=moves,
        )
        with self._search_lock:
            value, _ponder, _lines = self._search(position, "go depth 1")
        return value not in ("(none)", "0000")

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    assert process.stdin is not None
                    process.stdin.write("quit\n")
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        finally:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            if self._reader is not None:
                self._reader.join(timeout=1)
            self._reader = None
