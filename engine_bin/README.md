# Pikafish 執行檔紀錄

- 匯入日期：2026-09-22。
- 來源：`C:\Users\user\Desktop\專題\專題程式\Pikafish\pikafish-avx2.exe`。
- 引擎回報版本：Pikafish 2025-06-27。
- 本專案檔名：`pikafish.exe`，符合 `config.py` 的 `ENGINE_PATH`。
- SHA-256：`C4C00F7C7C97E0C46809A08210DF86836B3616583B200F5016AE6C81CF17F644`。
- 附帶原始 `Copying.txt` 與 `AUTHORS`；未複製 GUI、開局庫、原始碼或其他 CPU 版本。

## 尚缺模型權重

來源資料夾及其子目錄中未找到 `.nnue` 權重檔。此引擎預設要求
`pikafish.nnue`，必須補入與此版本相容的權重，才能進行棋局計算。
執行檔本身不是模型權重。

已確認目前電腦支援 AVX2；來源執行檔的 UCI 測試回傳 `uciok`、
`readyok`。盤面評估測試因缺少 `pikafish.nnue` 而退出，不能視為
已完成引擎計算驗證。複製的三個檔案皆已比對 SHA-256。

官方使用文件：
https://github.com/official-pikafish/Pikafish/wiki/Download-and-usage

上述版本、缺檔與啟動結果以本地檔案及實測為依據；權重相容性尚未驗證。
