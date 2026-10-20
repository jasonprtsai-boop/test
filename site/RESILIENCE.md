# 錯誤隔離與恢復修復（2026-10-19）

## 範圍與架構

有效產品入口為 `app.py`，根目錄的實驗腳本不屬於網站服務。
玩家／監控頁 → Flask API → 單一 GameService／StateStore → 序列化工作佇列 → Vision、Pikafish、Robot。
TMvision 透過獨立 HTTP receiver 上傳；YOLO 在隔離程序執行；安全快照同步保存於 game.json，Excel 由獨立背景工作輸出。
本次保留原有棋規、辨識門檻、機械路徑與安全鎖。

## 已確認並修復

| 嚴重度 | 根因與影響 | 修復與驗證 |
| --- | --- | --- |
| P1 | `/api/events` 無期限占用 Waitress worker；多頁可耗盡全部 8 個 worker | 網頁改成每次完成後間隔 1 秒查詢狀態，不重疊請求。舊 SSE 路由最多 4 條、每條最多約 30 秒；測試容量額滿後仍可查狀態與停止，關閉連線可釋放名額 |
| P1 | fetch 沒有逾時；pending 期間鎖住停止，後續狀態查詢失敗時按鈕可能保持停用 | 10 秒逾時包含回應 body；finally 立即更新按鈕；停止獨立送出；命令不自動重送。Node 測試注入雙重失敗與重複點擊 |
| P1 | `_handle_error` 重新載入續局資料時的第二個例外可殺死唯一工作執行緒 | 在工作邊界隔離恢復例外、顯示 recovery_failed；測試磁碟讀取失敗後可接受下一次開始 |
| P2 | Excel 排程／路徑例外會回傳主流程，甚至阻止安全快照取得狀態 | 將非必要的 Excel 例外隔離、保留診斷並在監控頁顯示；注入排程及目錄錯誤仍能進入玩家回合 |
| P2 | 預檢例外只寫 log，畫面可能一直顯示準備中 | 公開失敗原因並保留下一輪重試；故障後恢復測試通過 |
| P2 | 一個關閉步驟丟例外會跳過其他 HTTP server 清理 | 分別清理 service／server，socket close 失敗仍關閉 worker；對應測試通過 |

## 驗證

- 完整 Python unittest：187 項通過，啟用 ResourceWarning 檢查。
- Node 前端行為測試：4 項通過（逾時、斷線恢復、錯誤 JSON、按鈕及停止控制）。
- Python compileall、三份 JavaScript 語法、pip check、git diff --check 通過。
- Flask 測試客戶端涵蓋實際 route、串流容量與停止 API；硬體使用測試替身。
- 本環境實際 HTTP socket 測試受 WinError 10013 阻擋，未完成瀏覽器／真實網路壓力測試。
- 未連接或移動 TM5-700；現場斷線、停止及恢復仍待真機驗證。

執行新增前端測試（在 site 目錄）：`node --test tests/frontend_resilience.test.cjs`。

## 判斷邊界

這些修復處理已確認的軟體失敗路徑，不代表所有故障都不會使服務失效。
OS／磁碟 I/O 永久阻塞、程序被終止、啟動時資料目錄無法建立等情境仍可能需要修復環境或重新啟動。
UI 逾時不代表後端命令已取消，因此不自動重送開始、走棋或其他動作。
Robot 動作狀態不明時仍須操作員確認停止；網站停止不能取代實體 Emergency Stop。

## 技術查證與決策

- Technology / Version：Waitress 3.0.2（requirements.txt）。
- Official behavior：同步 WSGI 工作占用固定 worker；若所有 worker 永不結束，服務不再回應。
- Source：https://docs.pylonsproject.org/projects/waitress/en/latest/design.html
- Confidence：高；與現有 app.py 的 8 workers 及 web.py 無期限 generator 相符。
- Decision：網頁使用短請求查詢；保留有容量與期限限制的 SSE 相容路由。
- Alternatives：單純增加 threads 只延後耗盡；改換非同步伺服器涉及較大部署改動。
- Trade-off：狀態通常約晚 1 秒顯示，換取控制請求不受常駐頁面數占滿。
