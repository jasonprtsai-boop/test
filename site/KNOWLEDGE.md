# Knowledge notes

## Pikafish UCI analysis

- **Technology:** Pikafish UCI engine
- **Project version:** Pikafish 2026-09-25 Windows x86-64 universal build
- **Official behavior:** UCI 支援 `MultiPV`，分析輸出可包含 `score`、`wdl` 與 `pv`；本專案實際執行檔也已用三條候選路線驗證。
- **Known limitation:** WDL 來自引擎自我對弈模型，不能解讀為真人對局的保證勝率；從現場盤面建立的新基準無法還原基準之前的局史。
- **Project relevance:** 所有本局引擎查詢都以初始 FEN 加完整 UCI moves 建立位置；服務會先重播並比對目前盤面，避免錯誤歷史進入引擎。人類回合可唯讀分析；AI 對 AI 採 `ENGINE_READY` 逐步確認。
- **Sources:** [Pikafish 官方儲存庫](https://github.com/official-pikafish/Pikafish#readme)、[官方 UCI 與指令文件](https://github.com/official-pikafish/Pikafish/wiki/UCI-%26-Commands)、專案內 2026-09-25 執行檔整合測試。
- **Confidence:** MultiPV／WDL 輸出為高；WDL 對實際玩家勝率的外推不成立，因此 UI 明確標示為估計。

## Python runtime and WSGI server

- **Technology:** CPython / Waitress WSGI server
- **Project version:** Python 3.14.8；Waitress 3.0.2
- **Official behavior:** Python 3.14.8 是含安全修補的維護版本；Waitress 是支援 Windows 的 production-quality WSGI server。Werkzeug 官方明確表示內建開發伺服器不適合正式部署。
- **Known limitation:** 本機與區網仍是未加密 HTTP；Waitress 不會把此專案變成可直接暴露至公網的服務。UI 控制面只有 localhost Origin、JSON 格式及真機安全條件，沒有使用者身分或啟動碼驗證。
- **Project relevance:** UI 與 TMvision receiver 已改由 Waitress 啟動；Python 3.14.8 環境完成完整測試、相依性檢查、真實 Pikafish 與 `best.pt` 載入，封存 V1/V2 的 46 筆 detections 與舊環境逐值一致。
- **Sources:** [Python 3.14.8 官方發行頁](https://www.python.org/downloads/release/python-3148/)、[Waitress 3.0.2 官方文件](https://docs.pylonsproject.org/projects/waitress/en/stable/)、[Werkzeug 官方部署文件](https://werkzeug.palletsprojects.com/en/stable/deployment/)
- **Confidence:** 高；版本與執行路徑均在本機驗證。真機網路與長時間 SSE 穩定度仍須現場運轉測試。

## Pillow image parsing

- **Technology:** Pillow
- **Project version:** 12.3.0
- **Official behavior:** 專案只接受經 Pillow 解碼並確認 format 為 JPEG 的影像；12.3.0 修補 JPEG 2000 記憶體耗盡問題，並包含先前 PSD 越界寫入修補。
- **Known limitation:** MIME 與 JPEG 限制會縮小格式攻擊面，但不能取代持續維護影像解碼器；接收器仍限制來源 IP、檔案大小與每次 capture 張數。
- **Project relevance:** 從 TMvision 接收的內容是不可信輸入，不能保留已知有公開安全公告的舊解析器。
- **Sources:** [Pillow 12.3.0 release notes](https://github.com/python-pillow/Pillow/blob/main/docs/releasenotes/12.3.0.rst)、[GHSA-cfh3-3jmp-rvhc](https://github.com/advisories/GHSA-cfh3-3jmp-rvhc)、[GHSA-vjc4-5qp5-m44j](https://github.com/advisories/GHSA-vjc4-5qp5-m44j)
- **Confidence:** 高；版本已鎖定，實際虛擬環境安裝版本與測試結果可驗證。

## TM Listen `$CPERR,2,F1`

- **Technology:** TMSCT／TMflow Listen Node
- **Project version:** TMflow 1.82.51；官方 1.80 與 1.84 版 Listen Node 手冊對 F1 的定義一致。
- **Official behavior:** F1 表示控制器不在 External Script Mode，也就是 TMflow 尚未進入 Listen 節點；該次命令會被丟棄而不執行。
- **Known limitation:** 第二張 TMvision 影像已上傳，只能證明影像節點送出資料，不能證明 TMflow 流程已經回到下一個 Listen 節點。
- **Project relevance:** 網站在觸發拍照或真機移動前，會以唯讀 Pose 查詢確認 Listen 已就緒；收到 F1 時重建連線並等待，逾時後停止流程，禁止直接重送可能已被接受的動作命令。
- **Sources:** [Omron／Techman Expression Editor and Listen Node Reference Manual](https://files.omron.eu/downloads/latest/manual/en/i848_tm_expression_editor_and_listen_node_reference_manual_en.pdf?v=1)、[Techman Developer Area](https://www.tm-robot.com/en/support/developer-area/tm-ros-repositories)
- **Confidence:** F1 定義為高；本專案發生於 Vision 後尚未回到 Listen 的時序判斷為高，但修正後仍需一次現場真機驗證。

## TM Robot Pose 姿態表示

- **Technology:** TM Robot／TMflow Pose `[X, Y, Z, Rx, Ry, Rz]`
- **Project version:** TMflow 1.82.51
- **Official behavior:** TM Robot 使用 Euler ZYX 表示姿態；RX／RY／RZ 單位為度，範圍為 `[-180, +180]`。
- **Known limitation:** Euler 表示在 Ry 接近 ±90° 時存在奇異性；逐分量角差不是任意姿態下的通用旋轉距離。
- **Project relevance:** 專案固定目標姿態已由操作員更新為 `(-180, 0, 80)`，Ry 遠離奇異區；RX 的 `-180°` 與回授可能出現的 `+180°` 必須以最短環狀角差比較，不能直接相減。
- **Source:** [Techman Robot — What Are Base and TCP? How to Use Them?](https://www.tm-robot.com/en/support/technical-document/what-are-base-and-tcp-and-how-to-use-them-en)（官方頁面標示適用 TMflow 1.76 以上）
- **Confidence:** 高；官方版本範圍涵蓋目前 1.82.51，且目前姿態不在 Euler 奇異區附近。

## 2026-10-20：引擎輸出保存（本機實測）

- Technology / Version：隨附 Pikafish 2026-09-25，使用專案 NNUE。
- Observed：UCI 選項沒有 UCI_ShowWDL，但 depth 2 分析實際回傳 PV、分數與 WDL；不套用其他引擎的選項。
- Project relevance：suggest 原本只保留 bestmove，現保留該次完整分析至 engine_history；WDL 依行棋方轉成紅／和／黑，信心或 WDL 不當作真人勝率保證。
- Source：本機 uci 握手、PikafishEngine.analyze／GameService._query_engine 實測及 runtime/battle-workbook-qa/engine-analysis-verified.xlsx。
- Confidence：本機此版本觀測為高；未推論其他版本行為。
