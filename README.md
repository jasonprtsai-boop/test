# TM5-700 中國象棋機器人

目前有效的應用程式位於 [`site/`](site/)。根目錄其餘 Python 腳本與影像資料是開發／實驗資產，不是網站執行入口。

## 啟動位置

```powershell
cd site
.\.venv\Scripts\python.exe app.py
```

操作與真機設定請見 [`site/README.md`](site/README.md)，目前驗證邊界請見 [`site/VALIDATION.md`](site/VALIDATION.md)。

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
