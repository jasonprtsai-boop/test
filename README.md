# TM5-700 中國象棋機器人

目前有效的應用程式位於 [`site/`](site/)。根目錄其餘 Python 腳本與影像資料是開發／實驗資產，不是網站執行入口。

## 啟動位置

```powershell
cd site
.\.venv\Scripts\python.exe app.py
```

操作與真機設定請見 [`site/README.md`](site/README.md)，目前驗證邊界請見 [`site/VALIDATION.md`](site/VALIDATION.md)。

## 操作入口與對戰紀錄

- 玩家頁提供「偵測目前棋盤」與「從頭開始」；偵測預設核對並恢復同場，也可選擇同場重建基準或全新對戰。
- 新對戰可自訂 Excel 名稱，系統自動附台灣日期時間與識別碼；同場恢復及重建基準沿用同一本 Excel。
- 玩家頁與監控台顯示 Excel 實際儲存位置及更新狀態；新對戰檔案位於 `site/對局紀錄/整場對戰/`。
- 監控台展開例行檢查及未通過項目，其餘已通過項目收合；所有安全檢查仍照常執行。

詳細操作與歷史缺口處理請見 [`site/README.md`](site/README.md#對局保存與續局)。

## 執行資產

- `best.pt`：網站使用的 YOLO 權重，放在專案根目錄並納入版本控制。
- `engine_bin/pikafish.exe` 與 `engine_bin/pikafish.nnue`：Pikafish 執行所需資產。
- `site/runtime/`：執行期影像與暫存資料，不納入 Git。

## 提交前檢查

```powershell
cd site
.\.venv\Scripts\python.exe -W error::ResourceWarning -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q app.py tmchess tests tools
.\.venv\Scripts\python.exe -m pip check
```
