import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from chess_pipeline import ChessPipeline
from config import PROJECT_DIR
from main_gui import ChessRobotApp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_integration import initial_board


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            root = tk.Tk()
            root.withdraw()
            root.destroy()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk unavailable in this environment: {exc}")

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.detector = Mock()
        self.detector.detect.return_value = initial_board()
        self.engine = Mock()
        self.engine.best_move.return_value = "b2e2"
        self.app = ChessRobotApp(self.root, ChessPipeline(self.detector, self.engine))
        self.errors = patch("main_gui.messagebox.showerror").start()
        self.folder = tempfile.TemporaryDirectory(dir=PROJECT_DIR)
        self.image = Path(self.folder.name) / "棋盤.png"
        cv2.imencode(".png", np.zeros((1000, 900, 3), dtype=np.uint8))[1].tofile(str(self.image))

    def tearDown(self):
        self.app.close()
        self.folder.cleanup()
        patch.stopall()

    def finish_worker(self):
        deadline = time.monotonic() + 5
        while self.app._busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertFalse(self.app._busy, "GUI worker did not finish")

    def load_and_recognize(self):
        with patch("main_gui.filedialog.askopenfilename", return_value=str(self.image)):
            self.app.load_image()
        for x, y in ((50, 50), (850, 50), (850, 950), (50, 950)):
            self.app.select_corner(SimpleNamespace(x=x / self.app._scale_x, y=y / self.app._scale_y))
        self.app.recognize_board()
        self.finish_worker()

    def test_image_to_suggestion_ui(self):
        self.load_and_recognize()
        self.assertEqual(self.app.board, initial_board())
        self.assertFalse(self.app.confirm_button.instate(["disabled"]))
        self.app.confirm_move()
        self.finish_worker()
        self.assertIn("b2e2", self.app.output.get("1.0", "end"))
        self.errors.assert_not_called()
        self.engine.stop.assert_called_once()
        self.app.reset_corners()
        self.assertIsNone(self.app.board)
        self.assertTrue(self.app.confirm_button.instate(["disabled"]))

    def test_missing_nnue_preserves_recognized_board(self):
        self.load_and_recognize()
        self.engine.start.side_effect = FileNotFoundError("缺少 NNUE 權重")
        self.app.confirm_move()
        self.finish_worker()
        self.assertEqual(self.app.board, initial_board())
        self.assertIn("NNUE", self.app.status.get())
        self.errors.assert_called_once()
        self.engine.best_move.assert_not_called()

    def test_invalid_board_blocks_engine(self):
        self.detector.detect.return_value = [[0] * 9 for _ in range(10)]
        self.load_and_recognize()
        self.assertIsNone(self.app.board)
        self.assertTrue(self.app.confirm_button.instate(["disabled"]))
        self.errors.assert_called_once()
        self.engine.start.assert_not_called()

    def test_side_change_updates_fen(self):
        self.load_and_recognize()
        self.app.side.set("黑方")
        self.app._show_board()
        self.assertIn(" b - - 0 1", self.app.output.get("1.0", "end"))

    def test_close_cancels_pending_ui_callback(self):
        pending = self.app._poll_id
        self.assertIsNotNone(pending)
        self.app.close()
        self.assertIsNone(self.app._poll_id)

    def test_set_camera_corners_from_gui(self):
        import config
        with patch("main_gui.messagebox.showinfo"):
            for x, y in ((50, 50), (850, 50), (850, 950), (50, 950)):
                self.app.corners.append((x, y))
            self.app._update_controls()
            self.assertFalse(self.app.set_corners_button.instate(["disabled"]))
            self.app.set_camera_corners()
            self.assertEqual(config.CAMERA_CORNERS, [(50, 50), (850, 50), (850, 950), (50, 950)])

    def test_reset_game_clears_session_and_controls(self):
        self.app._physical_session = Mock()
        self.app.board = initial_board()
        self.app._update_controls()
        self.assertFalse(self.app.reset_game_button.instate(["disabled"]))
        self.app.reset_game()
        self.assertIsNone(self.app._physical_session)
        self.assertIsNone(self.app.board)
        self.assertTrue(self.app.reset_game_button.instate(["disabled"]))


if __name__ == "__main__":
    unittest.main()
