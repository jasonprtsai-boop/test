# Pikafish 執行資產紀錄

本目錄只保留網站無介面運行所需的引擎、NNUE 權重與授權文件；GUI 安裝程式、其他作業系統執行檔、開局庫與 C++ 原始碼不會被產品程式載入。

## 目前已驗證的配對

來源：`C:\Users\user\Desktop\專題\專題程式\pikafish\Pikafish皮卡鱼20260925`

- `pikafish.exe`
  - 來源檔：`Pikafish-Windows-x86-64-universal.exe`
  - 引擎回報：Pikafish 2026-09-25
  - SHA-256：`9824FFF4E3C4A72AFBC6CFDC611C140B32F79E3C5FD5FE4BF6D747833875B86D`
- `pikafish.nnue`
  - SHA-256：`7D13D73569A9B571BA0EB20CF1596247BC2A42738967E61AFEF6482B231E900E`

此配對已完成 UCI 初始化、`isready`、NNUE 載入、初始盤面計算及 `bestmove` 解析測試。來源更新紀錄表示此版更新搜尋、權重與規則，並由 universal 執行檔自動選擇 CPU 指令集。

## 路徑相容性

此 Windows 執行檔無法正確解析含中文的 NNUE 絕對路徑。網站 adapter 會把工作目錄設為 `engine_bin`，並傳入相對檔名 `pikafish.nnue`；不得改回含中文的絕對 `EvalFile` 路徑。

## 檔案

- `pikafish.exe`：UCI 象棋引擎。
- `pikafish.nnue`：與上述引擎配對的局面評估權重。
- `AUTHORS`、`Copying.txt`：同一來源快照的作者與 GPL 授權。
- `NNUE-License.md`：2026-09-25 發行套件內的權重使用條款。

引擎與網站的實際整合測試位於 `site/tests/test_engine.py`。
