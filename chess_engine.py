import logging
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from config import ENGINE_NNUE_PATH, ENGINE_PATH, ENGINE_TIMEOUT_SECONDS, ENGINE_TIME_LIMIT_MS

logger = logging.getLogger(__name__)


class PikafishEngine:
    def __init__(self, executable: Path = ENGINE_PATH, nnue_path: Optional[Path] = None,
                 timeout: float = ENGINE_TIMEOUT_SECONDS) -> None:
        self.executable = Path(executable).resolve()
        default_net = ENGINE_NNUE_PATH if self.executable == ENGINE_PATH else self.executable.with_name("pikafish.nnue")
        self.nnue_path = Path(nnue_path or default_net).resolve()
        self.timeout = timeout
        self.process: Optional[subprocess.Popen] = None
        self._lines = queue.Queue()
        self._reader = None

    def check_files(self) -> None:
        if not self.executable.is_file():
            raise FileNotFoundError(f"找不到 Pikafish 執行檔：{self.executable}")
        if not self.nnue_path.is_file() or self.nnue_path.stat().st_size == 0:
            raise FileNotFoundError(
                f"缺少 Pikafish NNUE 權重：{self.nnue_path}\n"
                "請放入與 Pikafish 2025-06-27 相容的 pikafish.nnue；exe 不是權重檔。"
            )

    @staticmethod
    def _read_output(stream, lines) -> None:
        try:
            for line in stream:
                lines.put(line.strip())
        finally:
            lines.put(None)

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.stop()
        self.check_files()
        self._lines = queue.Queue()
        self.process = subprocess.Popen(
            [str(self.executable)], cwd=str(self.executable.parent),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = threading.Thread(
            target=self._read_output, args=(self.process.stdout, self._lines), daemon=True
        )
        self._reader.start()
        try:
            self._send("uci")
            self._wait_for("uciok")
            # Avoid passing the Chinese Windows directory through the C++ UCI path parser.
            net = self.nnue_path.name if self.nnue_path.parent == self.executable.parent else str(self.nnue_path)
            self._send(f"setoption name EvalFile value {net}")
            self._send("isready")
            self._wait_for("readyok")
            logger.info("[ENGINE] ready: %s", self.executable)
        except Exception:
            self.stop()
            raise

    def _send(self, command: str) -> None:
        if "\n" in command or "\r" in command:
            raise ValueError("UCI 指令不可含有換行。")
        if self.process is None or self.process.stdin is None or self.process.poll() is not None:
            raise RuntimeError("Pikafish 尚未啟動或已退出。")
        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError("Pikafish 連線已中斷。") from exc

    def _wait_for(self, marker: str) -> str:
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Pikafish 等待 {marker} 超時（{self.timeout:g} 秒）。")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"Pikafish 等待 {marker} 超時。") from exc
            if line is None:
                raise RuntimeError(f"Pikafish 在回傳 {marker} 前退出，請確認 NNUE 與引擎版本相容。")
            if line.startswith("info string ERROR:"):
                raise RuntimeError(f"Pikafish：{line.removeprefix('info string ERROR:').strip()}")
            if line.split()[:1] == [marker]:
                return line

    def best_move(self, position: str, depth: int = 4,
                  movetime_ms: int = ENGINE_TIME_LIMIT_MS) -> str:
        if not 1 <= depth <= 10:
            raise ValueError("depth must be between 1 and 10")
        if movetime_ms <= 0:
            raise ValueError("movetime_ms must be positive")
        try:
            self._send("ucinewgame")
            self._send("isready")
            self._wait_for("readyok")
            self._send(f"position {position}")
            self._send(f"go depth {depth} movetime {movetime_ms}")
            parts = self._wait_for("bestmove").split()
            if len(parts) < 2:
                raise RuntimeError("Pikafish 回傳不完整的 bestmove。")
            logger.info("[ENGINE] bestmove=%s", parts[1])
            return parts[1]
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
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
            if self._reader is not None:
                self._reader.join(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass  # A crashed child may leave a broken pipe on close.
            self._reader = None
