# 專案資料樹與用途

清理狀態：2026-10-23 已依核准清單刪除 114 個項目，釋出 2,812,496,727 bytes。下列為主要保留結構。

```text
test/
├─ README.md                  啟動與操作入口
├─ PROJECT_TREE.md            本資料樹
├─ CLEANUP_REVIEW.md          已執行的刪除清單
├─ best.pt                    目前 YOLO 模型
├─ engine_bin/                Pikafish 引擎、NNUE、授權
├─ site/
│  ├─ app.py                  網站入口
│  ├─ .venv/                  目前執行環境
│  ├─ requirements*.txt       套件需求
│  ├─ requirements.lock       鎖定版本
│  ├─ tmchess/                對局、視覺、機械、保存核心
│  ├─ templates/、static/     網頁與靜態資源
│  ├─ tests/、tools/          回歸測試與維護工具
│  ├─ *.md                    操作、架構、研究與歷史決策
│  ├─ 對局紀錄/整場對戰/
│  │  ├─ 2026-10-21/吳.xlsx
│  │  └─ 2026-10-22/陳郁政.xlsx、簡瑞廷.xlsx
│  ├─ 系統紀錄/system.log     完整系統日誌
│  └─ runtime/
│     ├─ games/               照片、辨識結果與 game.json
│     ├─ calibration/         校正實測資料
│     ├─ model-backups/       模型回復用備份
│     └─ *.lock               執行鎖，勿手動刪除
├─ TM_4Point_DualVision/      早期實驗原始證據，保留
└─ cycle_test.py 等           舊硬體實驗程式，保留供參照
```

## 保留紀錄

- 陳郁政：20261022T052757Z-dfe2305f。
- 簡瑞廷：20261022T070949Z-2f088aef。
- 吳：由合併 Excel 的來源編號追查，連同前置片段共保留 44 個資料夾。
- games 維持原 ID 資料夾，避免破壞照片與續局路徑；Excel 依台灣開始日期分類。
- .git、現行環境、使用者未提交程式與測試均不列入刪除。

## 保留原因尚待確認

`.test_deps/`、`runtime/captures/` 與早期原始實驗資料未證實可重建，本次保留。
