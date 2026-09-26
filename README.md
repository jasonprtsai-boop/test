# TM5-700 中國象棋機器人

本專案位於 `test` 資料夾，是 Python / Tkinter 桌面程式。
目前已串接本機棋盤圖片、YOLO 模型、盤面轉換、Pikafish 引擎與 TMflow
實體對弈流程。離線圖片分析仍可獨立使用；實體模式需要先完成相機角點與
機械手臂座標設定。

## 啟動

目前電腦已建立專案專用 `.venv` 並安裝依賴，雙擊 `start_gui.bat` 即可啟動。
也可在專案資料夾執行：

```powershell
.\.venv\Scripts\python.exe main_gui.py
```

在其他電腦，建議安裝 Python 3.11，再於專案目錄建立環境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 使用流程

1. 選擇本機棋盤圖片。
2. 依序點選 **左上、右上、右下、左下** 四個最外側棋盤交叉點。
   不要點圖片邊緣；交叉點被棋子遮住時，點棋子中心。圖片應以棋盤直立方向拍攝，
   可有透視傾斜，紅方可在上方或下方，不支援橫轉 90 度的圖片。
3. 選擇圖片中的紅方位置，按「辨識盤面」。
4. 核對右側盤面的棋子及位置；右側預覽固定紅方在下。
5. 選擇目前輪到紅方或黑方及搜尋深度，按「確認盤面並計算走法」。
6. 查看著法、座標及箭頭。每次新的實體盤面須重新選圖、定位及辨識。

辨識會檢查類別、同格重複、交叉點距離、棋子數量及將帥位置。
這些是辨識合理性檢查，不是完整象棋規則驗證，也不能保證辨識完全正確。
TMflow RPC 邊界集中在 `robot_controller.py`，實體閉環集中在
`physical_game.py`，遊戲回合狀態機在 `game_controller.py`。

## TMflow Listen 架構

TMflow 專案應維持以下三個節點循環：

```text
Start → Listen → Point(Home) → Vision → Listen
```

`Listen` 是開機後的待命節點，不會自行拍照或移動。Python 需要初始盤面或
確認玩家走棋時呼叫 `RobotController.trigger_capture()`，送出 `ScriptExit()`，
讓 TMflow 執行一次 Point/ Vision 後回到 Listen。機械走棋則在 Listen 中由
`execute_chess_move()` 發送移動與夾爪 RPC，不需要改變 TMflow 節點順序。

`camera_receiver.py` 提供標準庫 `http.server` receiver，預設監聽 6189，
接受 `/api/DET` 的 JPEG POST；`wait_for_image()` 會以事件等待下一張影像，
不會阻塞 Tkinter 主執行緒。

`GameController.start(board, first_player, robot_side)` 負責先手分流與陣營設定：
依照中國象棋紅方先手規則，「玩家先手」時玩家執紅 (`w`)、機械執黑 (`b`)；
「機械先手」時機械執紅 (`w`)、玩家執黑 (`b`)。
`PhysicalGameSession` 會將 Vision POST 轉成盤面後交給 `player_finished()`，
並校驗玩家是否移動己方棋子；所有相機等待、YOLO、引擎與手臂動作都由 GUI worker 執行，不會堵住 Tkinter。

實體模式的執行順序是：

```text
開始遊戲
  → ScriptExit() → Point → Vision → 初始盤面
  → 玩家按「我下好了」
  → ScriptExit() → Point → Vision → 差分辨識
  → Pikafish bestmove
  → UCI 座標 → grid_to_robot_xy()
  → Line() / SetToolDO() → 手臂取放棋子
  → ScriptExit() → Point → Vision → 落子盤面驗證
```

若落子後盤面驗證失敗，GUI 會保留上一手並提供「重試落子」；重試不會
重新呼叫 Pikafish，只會再次執行同一組取放座標。Pikafish 回傳無合法著法
時，GUI 會顯示「遊戲結束」，不會再開放玩家回合。

手臂動作完成採固定時間等待，不增加 TMflow 節點：
`MOTION_SETTLE_SECONDS` 控制每個 `Line()` 後的等待，
`VACUUM_SETTLE_SECONDS` 與 `RELEASE_SETTLE_SECONDS` 控制吸盤開關後的等待。
這些數值必須依實機最慢動作測量後加安全餘裕。

`config.py` 的 `CAMERA_CORNERS` 必須填入相機影像中的 `TL, TR, BR, BL`
四個交叉點後才能啟動實體模式；`SIMULATION_MODE=False` 前也必須先完成
TMflow Listen 埠號、Tool DO、Z 高度與四角座標的實機測試。

## 模型與引擎

| 用途 | 本專案路徑 | 狀態 |
| --- | --- | --- |
| YOLO 棋子辨識 | `models/best.pt` | 已載入並完成實際推論測試 |
| Pikafish 執行檔 | `engine_bin/pikafish.exe` | AVX2，版本 2025-06-27 |
| Pikafish NNUE 權重 | `engine_bin/pikafish.nnue` | 已備齊，經實測與本地 Pikafish 2025-06-27 相容，可直接進行完整對弈運算 |

目前本機已備齊 `pikafish.exe` 與相容的 `pikafish.nnue`，可執行完整的盤面分析與最佳著法計算。
若缺少 NNUE，程式會保留辨識盤面並顯示明確錯誤。
所有檔案路徑集中在 `config.py`，以專案目錄為基準，不依賴啟動時的工作目錄。

所有檔案路徑集中在 `config.py`，以專案目錄為基準，不依賴啟動時的工作目錄。
YOLO 採 CPU 推論；相同介面生命週期只載入一次模型。
Pikafish 計算同時使用深度上限與 500 ms 搜尋時間上限，較早到達者限制搜尋；
通訊等待上限預設 15 秒，錯誤或逾時會清理子程序。

## 模組與資料格式

```text
main_gui.py：選圖、定位、結果預覽與背景工作
  → chess_pipeline.py：辨識／分析流程
    → vision_processor.py：YOLO 偵測中心 → 四角透視座標 → 10×9 盤面
    → chess_board.py：14 類棋子對照、盤面檢查、FEN 與 UCI 座標
    → chess_engine.py：檔案檢查、UCI 握手、EvalFile、搜尋、退出
```

`board[row][col]` 中 0 表示空格，1–14 為 `chess_board.py` 的固定棋子 ID。
`row=0` 是紅方底線；`col=0` 對應 UCI 的 a 線。
Pikafish 使用 `w` 表示紅方、`b` 表示黑方；例如 `b2e2` 轉為 `(1, 2) → (4, 2)`。
使用模型的類別名稱映射棋子，不直接假設任意模型的 class ID 排序都相同。

`robot_controller.py` 保留既有模擬控制骨架；GUI 不呼叫它。
`SIMULATION_MODE=True` 仍是預設，原本實機通訊封包尚未完成，不應開啟實機動作。

## 驗證

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip check
```

測試覆蓋盤面轉換、透視與旋轉、重複辨識拒絕、中文路徑、缺權重、逾時、
實際子程序通訊、GUI 工作流程及真實 Pikafish 對產生 FEN 的讀取。
成功搜尋回應使用測試替身，不能視為真實 NNUE 計算驗證。
Tk 無法啟動的環境會明確跳過 GUI 測試；目前電腦需在一般桌面執行環境測試。

2026-09-22 實測：30 項測試全部通過，套件相依性檢查通過；
提供的 YOLO 模型已載入，14 類名稱吻合，空白圖片推論成功。
尚未取得真實棋盤測試圖片，因此辨識準確率未驗證；完整引擎計算等待 NNUE 權重。

## 本次技術紀錄（2026-09-22）

- 模型 metadata 與實際載入：YOLO26n、Ultralytics 8.4.120，故固定該版本。
- 棋子類別：黑／紅各含 cannon、car、elephant、guard、horse、king、soldier。
- FEN 對照依 Pikafish 原始碼及本地引擎確認：車 R、馬 N、相 B、仕 A、帥 K、炮 C、兵 P；黑方小寫。
- 決策：以圖片加四角定位接入現有辨識入口，避免假定未知相機介面或把整張圖片均分為棋格。
- 決策：辨識與計算分兩步，使用者能核對盤面；缺 NNUE 時仍能使用辨識。
- 限制：獨立盤面分析沒有完整歷史，不能判定依賴重複局面的長將等情況。
- 官方 YOLO 推論介面：https://docs.ultralytics.com/modes/predict/
- 官方 Pikafish UCI：https://github.com/official-pikafish/Pikafish/wiki/UCI-%26-Commands
- 可信度：模型類別、介面通訊與 FEN 為本地實測；真實棋盤辨識及完整引擎計算尚待驗證。
