import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from chess_board import PIECE_LABELS, board_to_fen
from chess_pipeline import ChessPipeline
from camera_receiver import CameraReceiver
from config import CAMERA_CORNERS, ENGINE_NNUE_PATH, ENGINE_PATH, SERVER_PORT, YOLO_MODEL_PATH
from game_controller import Turn
from physical_game import PhysicalGameSession
from robot_controller import RobotController
from vision_processor import read_image


class ChessRobotApp:
    def __init__(self, root: tk.Tk, pipeline=None) -> None:
        self.root = root
        self.root.title("TM5-700 象棋｜圖片辨識與引擎分析")
        self.pipeline = pipeline if pipeline is not None else ChessPipeline()
        self.board = None
        self.image_path = None
        self.corners = []
        self._busy = False
        self._closing = False
        self._results = queue.Queue()
        self._photo = None
        self._physical_session = None
        self.physical_mode = tk.BooleanVar(value=False)
        self.player_color = tk.StringVar(value="玩家執紅")
        self.first_player = tk.StringVar(value="玩家先手")
        self.status = tk.StringVar(value="選擇圖片，再依序點選棋盤四個最外側交叉點。")
        self.depth = tk.IntVar(value=4)
        self.side = tk.StringVar(value="紅方")
        self.orientation = tk.StringVar(value="紅方在下")
        self.resources = tk.StringVar()

        frame = ttk.Frame(root, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="YOLO 辨識 → 確認盤面 → Pikafish 建議走法", font=("Microsoft JhengHei", 14)).pack(anchor="w")
        ttk.Label(frame, textvariable=self.resources).pack(anchor="w", pady=5)
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=5)
        self.load_button = ttk.Button(bar, text="1. 選擇棋盤圖片", command=self.load_image)
        self.load_button.pack(side="left", padx=(0, 6))
        self.reset_button = ttk.Button(bar, text="重選四角", command=self.reset_corners)
        self.reset_button.pack(side="left", padx=6)
        self.set_corners_button = ttk.Button(bar, text="設為相機四角", command=self.set_camera_corners)
        self.set_corners_button.pack(side="left", padx=6)
        self.orientation_box = ttk.Combobox(bar, textvariable=self.orientation, values=("紅方在下", "紅方在上"), state="readonly", width=9)
        self.orientation_box.pack(side="left", padx=6)
        self.orientation_box.bind("<<ComboboxSelected>>", lambda event: self._clear_board())
        self.detect_button = ttk.Button(bar, text="2. 辨識盤面", command=self.recognize_board)
        self.detect_button.pack(side="left", padx=6)
        ttk.Checkbutton(bar, text="實體對弈模式", variable=self.physical_mode,
                        command=self._update_controls).pack(side="left", padx=10)
        self.player_color_box = ttk.Combobox(
            bar, textvariable=self.player_color,
            values=("玩家執紅", "玩家執黑"), state="readonly", width=8,
        )
        self.player_color_box.pack(side="left", padx=6)
        self.first_player_box = ttk.Combobox(
            bar, textvariable=self.first_player,
            values=("玩家先手", "機械先手"), state="readonly", width=8,
        )
        self.first_player_box.pack(side="left", padx=6)
        self.start_game_button = ttk.Button(bar, text="開始遊戲", command=self.start_game)
        self.start_game_button.pack(side="left", padx=6)
        self.reset_game_button = ttk.Button(bar, text="結束/重置對弈", command=self.reset_game)
        self.reset_game_button.pack(side="left", padx=6)
        ttk.Label(frame, text="四角順序：左上 → 右上 → 右下 → 左下。請選棋盤線的交叉點，不是圖片邊緣。\n四角有棋子遮住時，點該交叉點上的棋子中心；紅方可在圖片上方或下方。").pack(anchor="w", pady=4)

        views = ttk.Frame(frame)
        views.pack(fill="both", expand=True)
        self.image_canvas = tk.Canvas(views, width=640, height=480, background="#eeeeee", highlightthickness=1)
        self.image_canvas.pack(side="left", anchor="n", padx=(0, 12))
        self.image_canvas.bind("<Button-1>", self.select_corner)
        self.board_canvas = tk.Canvas(views, width=340, height=450, background="#f4e4bd", highlightthickness=1)
        self.board_canvas.pack(side="left", anchor="n")
        self._draw_board()

        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=10)
        ttk.Label(controls, text="目前輪到").pack(side="left")
        self.side_box = ttk.Combobox(controls, textvariable=self.side, values=("紅方", "黑方"), width=6, state="readonly")
        self.side_box.pack(side="left", padx=6)
        self.side_box.bind("<<ComboboxSelected>>", lambda event: self._show_board())
        ttk.Label(controls, text="搜尋深度").pack(side="left", padx=(12, 0))
        self.depth_box = ttk.Combobox(controls, textvariable=self.depth, values=list(range(1, 11)), width=4, state="readonly")
        self.depth_box.pack(side="left", padx=6)
        self.confirm_button = ttk.Button(controls, text="3. 確認盤面並計算走法", command=self.confirm_move)
        self.confirm_button.pack(side="left", padx=12)
        self.player_done_button = ttk.Button(controls, text="我下好了", command=self.player_finished)
        self.player_done_button.pack(side="left", padx=6)
        self.retry_move_button = ttk.Button(controls, text="重試落子", command=self.retry_move)
        self.retry_move_button.pack(side="left", padx=6)
        self.output = tk.Text(frame, height=4, width=100, wrap="word", state="disabled")
        self.output.pack(fill="x")
        ttk.Label(frame, textvariable=self.status, wraplength=960).pack(anchor="w", pady=(8, 0))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._refresh_resources()
        self._update_controls()
        self._poll_id = self.root.after(100, self._poll_results)

    def _refresh_resources(self):
        yolo = "YOLO 模型檔已找到" if YOLO_MODEL_PATH.is_file() else "缺少 YOLO 模型檔"
        engine = "引擎與權重檔已找到" if ENGINE_PATH.is_file() and ENGINE_NNUE_PATH.is_file() else "引擎缺少執行檔或 NNUE 權重，仍可辨識圖片"
        self.resources.set(f"{yolo}；{engine}。")

    def _write_output(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", text)
        self.output.configure(state="disabled")

    def _update_controls(self):
        for button, available in ((self.load_button, True), (self.reset_button, self.image_path is not None),
                                  (self.detect_button, len(self.corners) == 4), (self.confirm_button, self.board is not None)):
            button.configure(state="normal" if available and not self._busy else "disabled")
        for box in (self.orientation_box, self.side_box, self.depth_box):
            box.configure(state="disabled" if self._busy else "readonly")
        self.set_corners_button.configure(state="normal" if len(self.corners) == 4 and not self._busy else "disabled")
        physical_available = self.physical_mode.get() and not self._busy
        self.start_game_button.configure(
            state="normal" if physical_available and self._physical_session is None else "disabled"
        )
        self.reset_game_button.configure(
            state="normal" if self._physical_session is not None and not self._busy else "disabled"
        )
        self.player_color_box.configure(state="readonly" if physical_available and self._physical_session is None else "disabled")
        self.first_player_box.configure(state="readonly" if physical_available and self._physical_session is None else "disabled")
        retry_available = (
            self._physical_session is not None
            and self._physical_session.pending_move is not None
        )
        player_turn = (
            self._physical_session is not None
            and self._physical_session.game.turn is Turn.PLAYER
            and not retry_available
        )
        self.player_done_button.configure(state="normal" if player_turn and not self._busy else "disabled")
        self.retry_move_button.configure(
            state="normal" if retry_available and not self._busy else "disabled"
        )

    def _clear_board(self):
        self.board = None
        self._write_output("")
        self._draw_board()
        self._update_controls()

    def load_image(self):
        path = filedialog.askopenfilename(title="選擇棋盤圖片", filetypes=[("圖片", "*.png *.jpg *.jpeg *.bmp *.webp"), ("全部檔案", "*.*")])
        if not path:
            return
        try:
            from PIL import Image, ImageTk
            pixels = read_image(path)
            image = Image.fromarray(pixels[:, :, ::-1])
            width, height = image.size
            image.thumbnail((640, 480))
            self._scale_x, self._scale_y = width / image.width, height / image.height
            self._preview_size = image.size
            self._photo = ImageTk.PhotoImage(image, master=self.root)
        except Exception as exc:
            messagebox.showerror("圖片載入失敗", str(exc))
            return
        self.image_path = Path(path)
        self.image_canvas.delete("all")
        self.image_canvas.create_image(0, 0, image=self._photo, anchor="nw")
        self.reset_corners()

    def reset_corners(self):
        self.corners = []
        self.image_canvas.delete("corner")
        self._clear_board()
        self.status.set("請依序點選：左上、右上、右下、左下四個最外側交叉點。")

    def select_corner(self, event):
        if self._busy or self.image_path is None or len(self.corners) == 4:
            return
        if not (0 <= event.x < self._preview_size[0] and 0 <= event.y < self._preview_size[1]):
            return
        self.corners.append((event.x * self._scale_x, event.y * self._scale_y))
        self.image_canvas.create_oval(event.x - 5, event.y - 5, event.x + 5, event.y + 5, fill="#ff4b30", tags="corner")
        self.image_canvas.create_text(event.x + 12, event.y + 12, text=str(len(self.corners)), fill="#d00000", tags="corner")
        self.status.set(f"已選取 {len(self.corners)}/4 個交叉點。" + ("可以辨識盤面。" if len(self.corners) == 4 else ""))
        self._update_controls()

    def recognize_board(self):
        if self._busy or self.image_path is None or len(self.corners) != 4:
            return
        self._clear_board()
        self._start_task("board", self.pipeline.recognize, self.image_path, tuple(self.corners), self.orientation.get() == "紅方在下")

    def _show_board(self):
        self._draw_board()
        if self.board is not None:
            fen = board_to_fen(self.board, "w" if self.side.get() == "紅方" else "b")
            self._write_output(f"請核對右側棋盤（固定紅方在下）與目前輪到哪一方。\nFEN：{fen}")

    def _draw_board(self, suggestion=None):
        canvas = self.board_canvas
        canvas.delete("all")
        for col in range(9):
            x = 34 + col * 34
            canvas.create_line(x, 40, x, 400, fill="#927043")
            canvas.create_text(x, 426, text=chr(ord("a") + col))
        for row in range(10):
            y = 400 - row * 40
            canvas.create_line(34, y, 306, y, fill="#927043")
            canvas.create_text(15, y, text=str(row))
        if self.board is not None:
            for row in range(10):
                for col in range(9):
                    piece = self.board[row][col]
                    if piece:
                        x, y = 34 + col * 34, 400 - row * 40
                        canvas.create_oval(x - 15, y - 15, x + 15, y + 15, fill="#fff7dd", outline="#997243")
                        canvas.create_text(x, y, text=PIECE_LABELS[piece], fill="#b00000" if piece >= 8 else "#222222", font=("Microsoft JhengHei", 16))
        if suggestion and suggestion.source is not None:
            a, b = suggestion.source, suggestion.destination
            canvas.create_line(34 + a[0] * 34, 400 - a[1] * 40, 34 + b[0] * 34, 400 - b[1] * 40, arrow="last", width=4, fill="#167eae")

    def confirm_move(self):
        if self._busy or self.board is None:
            return
        self._refresh_resources()
        self._show_board()  # Clear any suggestion from an earlier analysis.
        board = [row[:] for row in self.board]
        self._start_task("move", self.pipeline.suggest_move, board, "w" if self.side.get() == "紅方" else "b", self.depth.get())

    def set_camera_corners(self):
        if len(self.corners) != 4:
            messagebox.showwarning("提示", "請先在圖片上依序點選四個最外側交叉點。")
            return
        import config
        config.CAMERA_CORNERS = list(self.corners)
        self.status.set(f"已設定實體相機四角：{self.corners}")
        messagebox.showinfo("相機角點設定完成", f"實體對弈相機四角已更新為：\n{self.corners}")
        self._update_controls()

    def reset_game(self):
        if self._busy:
            return
        self._close_physical_session()
        self.status.set("實體對弈已重置。可重新選擇設定並開始遊戲。")
        self._clear_board()
        self._update_controls()

    def start_game(self):
        if self._busy or not self.physical_mode.get() or self._physical_session is not None:
            return
        import config
        corners = config.CAMERA_CORNERS if config.CAMERA_CORNERS is not None else (list(self.corners) if len(self.corners) == 4 else None)
        if corners is None:
            messagebox.showerror("缺少相機角點", "請先在圖片上選取四角並點擊「設為相機四角」，或在 config.py 設定 CAMERA_CORNERS。")
            return
        config.CAMERA_CORNERS = corners
        try:
            receiver = CameraReceiver(port=SERVER_PORT)
            receiver.start()
            robot = RobotController()
            self._physical_session = PhysicalGameSession(
                self.pipeline, robot, receiver, corners,
                self.orientation.get() == "紅方在下", self.depth.get(),
            )
        except Exception as exc:
            self._close_physical_session()
            self.status.set(str(exc).splitlines()[0])
            messagebox.showerror("實體模式無法啟動", str(exc))
            self._update_controls()
            return
        p_side = "w" if self.player_color.get() == "玩家執紅" else "b"
        first = "PLAYER" if self.first_player.get() == "玩家先手" else "ROBOT"
        self._start_task("physical_start", self._physical_session.start, first, p_side)

    def player_finished(self):
        if self._busy or self._physical_session is None:
            return
        self._start_task("physical_player", self._physical_session.player_finished)

    def retry_move(self):
        if self._busy or self._physical_session is None:
            return
        self._start_task("physical_retry", self._physical_session.retry_last_move)

    def _start_task(self, kind, function, *args):
        self._busy = True
        self._update_controls()
        if kind == "board":
            status = "正在辨識盤面…"
        elif kind.startswith("physical"):
            status = "實體流程執行中：拍照、辨識、計算或手臂動作…"
        else:
            status = "Pikafish 計算中…"
        self.status.set(status)

        def worker():
            try:
                self._results.put((kind, function(*args), None))
            except Exception as exc:
                self._results.put((kind, None, exc))
        threading.Thread(target=worker, daemon=True).start()

    def _poll_results(self):
        self._poll_id = None
        try:
            kind, result, error = self._results.get_nowait()
        except queue.Empty:
            pass
        else:
            self._busy = False
            if not self._closing:
                try:
                    if error:
                        raise error
                    if kind == "board":
                        self.board = result
                        self._show_board()
                        self.status.set("辨識完成。核對棋子位置、紅黑方向及行棋方後，可計算建議走法。")
                    elif kind.startswith("physical"):
                        self.board = [row[:] for row in self._physical_session.game.board]
                        if self._physical_session and self._physical_session.game:
                            self.side.set("紅方" if self._physical_session.game.side == "w" else "黑方")
                        if kind == "physical_start":
                            last_move = getattr(self._physical_session, "last_move", None)
                            if result is Turn.FINISHED or (last_move and last_move.source is None):
                                self._show_board()
                                self.status.set("遊戲結束：目前沒有合法著法。")
                            elif last_move and last_move.source is not None:
                                self._draw_board(last_move)
                                self._write_output(
                                    f"機械先手走子：{last_move.move}；座標 {last_move.source} → {last_move.destination}\nFEN：{last_move.fen}"
                                )
                                self.status.set("機械已完成先手走棋。輪到玩家下棋，走子後按「我下好了」。")
                            else:
                                self._show_board()
                                self.status.set("輪到玩家先手下棋。完成實體走子後按「我下好了」。")
                        elif result.source is None:
                            self._show_board()
                            self.status.set("遊戲結束：目前沒有合法著法。")
                        elif kind in ("physical_player", "physical_retry"):
                            self._draw_board(result)
                            self._write_output(
                                f"AI 走棋：{result.move}；座標 {result.source} → {result.destination}\nFEN：{result.fen}"
                            )
                            self.status.set("輪到玩家下棋。完成實體走子後按「我下好了」。")
                        else:
                            self._show_board()
                            self.status.set("輪到玩家下棋。完成實體走子後按「我下好了」。")
                    else:
                        self._draw_board(result)
                        detail = "目前沒有合法著法。" if result.source is None else f"建議：{result.move}；座標 {result.source} → {result.destination}"
                        self._write_output(f"{detail}\nFEN：{result.fen}")
                        self.status.set("引擎分析完成；此介面只顯示建議走法。")
                except Exception as exc:
                    if kind == "board":
                        # Leave the drawn result visible for diagnosis, but prevent engine input.
                        self.board = None
                    if kind.startswith("physical") and self._physical_session and self._physical_session.pending_move is not None:
                        self.status.set("落子驗證未通過；請檢查棋盤後按「重試落子」。")
                    elif kind == "physical_start":
                        self._close_physical_session()
                        self.status.set(str(exc).splitlines()[0])
                    else:
                        self.status.set(str(exc).splitlines()[0])
                    messagebox.showerror("處理失敗", str(exc))
                self._update_controls()
        if self._closing and not self._busy:
            self._close_physical_session()
            self.root.destroy()
            return
        self._poll_id = self.root.after(100, self._poll_results)

    def close(self):
        if self._closing:
            return
        self._closing = True
        self.root.withdraw()
        # The worker owns engine cleanup; do not race it with a GUI-thread stop.
        if not self._busy:
            self._close_physical_session()
            if self._poll_id is not None:
                self.root.after_cancel(self._poll_id)
                self._poll_id = None
            self.root.destroy()

    def _close_physical_session(self):
        if self._physical_session is None:
            return
        try:
            self._physical_session.close()
        finally:
            self._physical_session = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app_root = tk.Tk()
    ChessRobotApp(app_root)
    app_root.mainloop()
