# 展示版本驗收與證據管理

本文件供開發者與現場操作員使用。軟體測試通過不代表實體取放成功；每次展示以固定版本、設定及現場驗收紀錄為準。

## 系統責任

玩家與監控頁 → HTTP API → 單一 GameService 與工作佇列 → 視覺／引擎／機械 adapter → JSON 安全快照 → 背景 Excel 與照片輸出。

JSON 是恢復依據，Excel 是衍生報告。禁止讓報表修改回寫機械控制狀態。重構時先抽出無副作用的統計與格式轉換，不新增第二份遊戲狀態或機械連線管理者。

## 固定版本

每次驗收記錄程式 commit、未提交差異、requirements.lock、模型與設定檔 SHA256、TMflow／控制器版本、Base／TCP、棋盤位置及操作員。無法取得的欄位填「未取得」，不得沿用舊值假裝完成。

## 軟體驗收

於 site 執行：

```powershell
.\.venv\Scripts\python.exe tools/verify_release_assets.py
.\.venv\Scripts\python.exe -W error::ResourceWarning -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app.py tmchess tests tools
.\.venv\Scripts\python.exe -m pip check
node --test tests/frontend_resilience.test.cjs
node --check static/common.js
node --check static/player.js
node --check static/monitor.js
git diff --check
```

`verify_release_assets.py` 會檢查 Python 版本、lock file、YOLO、Pikafish 與 NNUE 的大小及 SHA256。每局 JSON／Excel 亦保存程式 commit、tracked dirty 狀態與上述資產雜湊。`release_manifest.json` 目前明確標示公開發布仍受 `best.pt` 訓練／授權來源未封存所阻擋；不得把內部可重現部署誤稱為已具備公開散布條件。

任何失敗先記錄觸發條件與第一個異常邊界，再修改。兩次相同失敗沒有新證據時停止堆疊修改，重新檢查環境、假設及資料流。

## 現場驗收

| 場景 | 驗收依據 | 結果欄位 |
|---|---|---|
| 開局與玩家落子 | 現場盤面、辨識盤面、合法著法一致 | 通過／失敗／未測 |
| 一般取放與吃子 | 棋子確實到目的格、回待命、無碰觸障礙 | 同上 |
| 邊緣棋格與箱邊 | 記錄配置及實際路徑，不沿用不同配置簽核 | 同上 |
| 停止與恢復 | 後續命令停止，未知狀態保持鎖定，人工確認後恢復 | 同上 |
| 中斷後續局 | 先核對盤面，不重播已完成動作 | 同上 |
| 紀錄保存 | JSON 可重播、Excel 顯示更新結果、照片對應同次拍攝 | 同上 |

只在操作員值守及適當測試環境下進行真機驗收。網站停止不能撤回控制器已接受的動作，不能代替實體急停。

## 效能與研究指標

唯讀分析既有紀錄：

```powershell
.\.venv\Scripts\python.exe tools/summarize_performance.py --games-root runtime/games --output ../outputs/performance_new.json
```

輸出檔必須不存在，避免覆寫證據。P95 使用 nearest rank；按操作用途及成功／失敗狀態分組，缺失、負值與非有限數不當作零。片段數不是場數。不同版本及測試條件應分開準備來源目錄後分析；工具不會替舊紀錄推定版本。

分別報告整盤辨識正確率、單格正確率、取放成功率、整局完成率與恢復成功率。每個比例都有分母、成功定義及人工真值。推論信心不是準確率；命令完成不是實體取放成功。

比較單／雙視角及歷史修正時使用相同封存影像與真值；依拍攝場次切分訓練與測試，防止近似圖片跨組。快速模式與拍照驗證的比較列為未來實驗，現行控制流程不新增二次拍攝。

## 介面與書面規格

玩家頁聚焦目前回合及下一步；進階參數收合。監控頁保留詳細錯誤與實際路徑。鍵盤焦點必須可見，狀態不能只靠顏色，窄螢幕長路徑不能撐破版面。

書面順序：問題與情境、架構、方法、實驗設計、結果、限制。每張圖有圖號與條件，每個成果數字有日期、版本、來源與成功定義。全篇固定使用「動作完成」「預期盤面」「影像確認」三個不同概念。

首頁歷史 100／100 與 89／100 不代表目前整局完成率；未核實的版本、日期及真值標示未知。歷史稽核僅是當時快照，修正狀態以現行程式與 VALIDATION 補充為準。

## 時程與展示

先完成語意與資料正確性，再完成故障回歸、現場驗收、視覺檢查及展示排演。發表前至少保留一個完整工作日凍結功能，只修已確認阻斷問題。每次變更都保留可回復版本，不以刪除原始對局換取整潔。

展示腳本：說明限制 → 核對設備 → 新局 → 玩家落子 → AI 動作 → 展示紀錄 → 說明故障恢復。準備標示日期及版本的錄影備援，不把錄影當即時操作。

## 維護與封存

展示前查看磁碟空間並抽查備份可讀。按對戰與日期人工封存 JSON、照片、Excel 及相關日誌，保留來源對應；確認備份後才另行決定是否移除本機副本。系統不自動刪除歷史。
