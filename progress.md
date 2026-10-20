Original prompt: 先開始製作

## Goal context

Implement the planned Pikafish modes in the existing TM5-700 xiangqi website: explicit Red/Black controllers, hints and WDL analysis, and a safe step-based engine-vs-engine flow. Do not enable unattended continuous robot play in the first version.

## Current iteration

- [x] Add structured Pikafish analysis parsing and tests.
- [x] Add controller/side-to-move state without weakening robot safety boundaries.
- [x] Add hint and step-execution APIs.
- [x] Add player-page controls and analysis display.
- [x] Run backend, browser, visual, and regression validation.

## Constraints and gotchas

- Pikafish 2026-09-25 emits `wdl` directly but does not advertise `UCI_ShowWDL`; detect actual options instead of sending unsupported options.
- Keep `EvalFile` relative to `engine_bin` because this binary mishandles the Chinese absolute path.
- Hint/analysis operations must never create a robot pending move or call the robot adapter.
- The first engine-vs-engine version must require an explicit execute action for each robot move.

## Completed

- Added `EngineAnalysis` / `AnalysisLine`, robust UCI analysis parsing, MultiPV support, and a serialized engine search lock.
- Verified the bundled 2026-09-25 binary returns three ranked WDL lines; `tests.test_engine` passes 5/5.
- Added four explicit standard-start modes: Human Red, Human Black, step-based Engine vs Engine, and two-human Analysis.
- Added an `ENGINE_READY` safety boundary: engine-vs-engine computes a pending move but cannot call the robot until the execute endpoint is explicitly confirmed.
- Added fixed Red/Black controller and side-to-move state, hint/WDL UI, and regression coverage; service/web tests pass 76/76.
- Final regression: 149/149 tests pass; Python compile and JavaScript syntax checks pass.
- Chrome verification covered the four mode cards, Human Red start, controller/turn display, hint panel, `render_game_to_text`, and console errors (none). Browser harness artifacts were removed after inspection.
