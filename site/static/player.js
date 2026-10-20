(() => {
  const setupSection = document.querySelector("#setupSection");
  const resumeSection = document.querySelector("#resumeSection");
  const resumeInfo = document.querySelector("#resumeInfo");
  const resumeActorChoices = document.querySelector("#resumeActorChoices");
  const resumeButton = document.querySelector("#resumeButton");
  const lastResultSection = document.querySelector("#lastResultSection");
  const lastResultMessage = document.querySelector("#lastResultMessage");
  const currentBoardButton = document.querySelector("#currentBoardButton");
  const gameSection = document.querySelector("#gameSection");
  const setupMessage = document.querySelector("#setupMessage");
  const turnDisplay = document.querySelector("#turnDisplay");
  const gameMessage = document.querySelector("#gameMessage");
  const startButton = document.querySelector("#startButton");
  const hintButton = document.querySelector("#hintButton");
  const doneButton = document.querySelector("#doneButton");
  const executeEngineButton = document.querySelector("#executeEngineButton");
  const retryButton = document.querySelector("#retryButton");
  const stopButton = document.querySelector("#stopButton");
  const errorBox = document.querySelector("#errorBox");
  const analysisPanel = document.querySelector("#analysisPanel");
  const analysisStatus = document.querySelector("#analysisStatus");
  const analysisBest = document.querySelector("#analysisBest");
  const wdlGrid = document.querySelector("#wdlGrid");
  const redWin = document.querySelector("#redWin");
  const drawRate = document.querySelector("#drawRate");
  const blackWin = document.querySelector("#blackWin");
  const analysisLines = document.querySelector("#analysisLines");
  let pending = false;
  let resumeSelectionInitialized = false;
  let latestState = null;
  let requestError = null;

  function percent(value) {
    return `${(Number(value || 0) / 10).toFixed(1)}%`;
  }

  function scoreText(line) {
    if (line.score_type === "mate") return `將殺 ${Math.abs(line.score)} 步`;
    const sign = line.score > 0 ? "+" : "";
    return `評分 ${sign}${line.score}`;
  }

  function renderAnalysis(analysis) {
    const visible = analysis && analysis.status !== "idle";
    analysisPanel.hidden = !visible;
    if (!visible) return;

    analysisStatus.className = `subtle-badge${analysis.status === "error" ? " error" : ""}`;
    analysisStatus.textContent = analysis.status === "calculating" ? "計算中" :
      analysis.status === "ready" ? "分析完成" : "分析失敗";
    analysisLines.replaceChildren();
    wdlGrid.hidden = true;

    if (analysis.status === "calculating") {
      analysisBest.textContent = "Pikafish 正在分析目前局面…";
      return;
    }
    if (analysis.status === "error") {
      analysisBest.textContent = analysis.error?.message || "無法取得分析。";
      return;
    }

    analysisBest.textContent = analysis.bestmove ? `首選：${analysis.bestmove.uci}` : "目前沒有合法著法。";
    const primary = analysis.lines?.[0];
    if (primary && Number.isFinite(primary.red_win_per_mille)) {
      wdlGrid.hidden = false;
      redWin.textContent = percent(primary.red_win_per_mille);
      drawRate.textContent = percent(primary.draw_per_mille);
      blackWin.textContent = percent(primary.black_win_per_mille);
    }
    (analysis.lines || []).forEach((line) => {
      const item = document.createElement("li");
      const move = line.pv?.[0] || "—";
      item.textContent = `${move} · ${scoreText(line)} · 深度 ${line.depth}`;
      analysisLines.append(item);
    });
  }

  function friendlyMessage(state) {
    const messages = {
      PLAYER_TURN: "輪到玩家。走完一枚棋子後，按「玩家完成」。",
      INITIAL_CAPTURE: "AI 正在準備第一步，請稍候。",
      AI_THINKING: "AI 正在思考下一步。",
      ENGINE_READY: "引擎已算好下一步；確認棋盤周圍安全後再執行。",
      ROBOT_MOVING: "機械手臂正在移動棋子，請勿靠近。",
      VERIFYING: "正在確認棋盤，請稍候。"
    };
    return messages[state.phase] || state.message || "—";
  }

  function render(state) {
    state = TMChess.newestState(latestState, state);
    latestState = state;
    const resumeAvailable = Boolean(state.resume?.available);
    const resumableError = state.phase === "ERROR" && resumeAvailable && !state.controls.can_retry;
    const setupRecovery = state.phase === "ERROR" && !state.controls.can_retry &&
      (state.controls.can_start || state.controls.can_start_current || state.controls.can_resume);
    const gameActive = Boolean(state.first_actor) &&
      !["FINISHED", "STOPPED"].includes(state.phase) && !resumableError && !setupRecovery;
    const showLastResult = Boolean(state.first_actor) && ["FINISHED", "STOPPED"].includes(state.phase);
    setupSection.hidden = gameActive;
    gameSection.hidden = !gameActive;
    lastResultSection.hidden = !showLastResult;
    if (showLastResult) {
      const label = state.phase === "FINISHED" ? "上一局已結束" : "上一局已停止";
      lastResultMessage.textContent = `${label}：${state.message || "未提供原因。"}`;
    }
    resumeSection.hidden = !resumeAvailable;
    if (resumeAvailable) {
      const savedTurn = state.resume.current_turn === "ai" ? "AI" :
        state.resume.current_turn === "player" ? "玩家" : "未確認";
      const fixedTurnMode = ["engine_vs_engine", "analysis"].includes(state.resume.game_mode);
      const modeLabel = state.resume.game_mode === "engine_vs_engine" ? "AI 對 AI" :
        state.resume.game_mode === "analysis" ? "雙人分析" : "玩家對 AI";
      resumeActorChoices.hidden = fixedTurnMode;
      resumeInfo.textContent = fixedTurnMode ?
        `上一局：${state.resume.game_id || "—"}（${modeLabel}），將依保存的行棋方繼續。` :
        `上一局：${state.resume.game_id || "—"}，保存時回合：${savedTurn}。請依實體棋盤選擇目前回合。`;
      if (!resumeSelectionInitialized) {
        const suggested = state.resume.current_turn === "ai" ? "ai" : "player";
        const input = document.querySelector(`input[name='resumeActor'][value='${suggested}']`);
        if (input) input.checked = true;
        resumeSelectionInitialized = true;
      }
    } else {
      resumeActorChoices.hidden = false;
      resumeSelectionInitialized = false;
    }
    const sideLabel = state.side_to_move === "w" ? "紅方" : state.side_to_move === "b" ? "黑方" : "—";
    const controller = state.controllers?.[state.side_to_move];
    const controllerLabel = controller === "human" ? "玩家" : controller === "engine" ? "Pikafish" : "";
    turnDisplay.textContent = state.phase === "ERROR" ? "暫停" :
      state.side_to_move ? `${sideLabel} · ${controllerLabel}` : "—";
    gameMessage.textContent = friendlyMessage(state);
    renderAnalysis(state.analysis);
    startButton.disabled = pending || !state.controls.can_start;
    currentBoardButton.disabled = pending || !state.controls.can_start_current;
    document.querySelector("#continuationMode").disabled = pending || !state.controls.can_start_current;
    resumeButton.disabled = pending || !state.controls.can_resume;
    startButton.textContent = state.preflight.ready ? "從標準開局開始" : "系統準備中…";
    currentBoardButton.textContent = state.preflight.ready ? "辨識目前棋盤並開始" : "系統準備中…";
    const runtimePending = state.preflight.checks.some(check => check.key === "runtime_pending");
    setupMessage.textContent = state.preflight.ready ? "選擇開始方式後即可進行。" :
      runtimePending || !state.preflight.checked_at ? "系統準備中，請稍候。" : "系統尚未就緒，請通知操作人員。";
    doneButton.disabled = pending || !state.controls.can_player_done;
    doneButton.textContent = state.game_mode === "analysis" ? "本方完成" : "玩家完成";
    hintButton.disabled = pending || !state.controls.can_hint;
    executeEngineButton.disabled = pending || !state.controls.can_execute_engine_move;
    executeEngineButton.hidden = !state.controls.can_execute_engine_move;
    retryButton.disabled = pending || !state.controls.can_retry;
    retryButton.hidden = !state.controls.can_retry;
    stopButton.disabled = !state.controls.can_stop;
    document.querySelectorAll("input[name='gameMode']").forEach(input => {
      input.disabled = pending || !state.controls.can_start;
    });
    document.querySelectorAll("input[name='resumeActor']").forEach(input => {
      input.disabled = pending || !state.controls.can_resume;
    });
    document.querySelectorAll("input[name='currentPlayerColor'], input[name='currentBoardActor']").forEach(input => {
      input.disabled = pending || !state.controls.can_start_current;
    });
    const visibleError = state.error ? `${state.error.code}：${state.error.message}` : requestError;
    errorBox.hidden = !visibleError;
    errorBox.textContent = visibleError || "";
  }

  async function command(path, body) {
    if (pending && path !== "/api/game/stop") return;
    const isStop = path === "/api/game/stop";
    if (isStop) {
      try { render(await TMChess.request(path, {method: "POST", body: "{}"})); }
      catch (error) { requestError = error.message; if (latestState) render(latestState); }
      return;
    }
    pending = true;
    requestError = null;
    if (latestState) render(latestState);
    try {
      render(await TMChess.request(path, {method: "POST", body: JSON.stringify(body || {})}));
    } catch (error) {
      requestError = error.message;
      if (latestState) {
        render(latestState);
      } else {
        errorBox.hidden = false;
        errorBox.textContent = requestError;
      }
    } finally {
      pending = false;
      if (latestState) render(latestState);
      try { render(await TMChess.request("/api/state")); } catch (_) { /* Keep last state. */ }
    }
  }

  startButton.addEventListener("click", () => command("/api/game/start-mode", {
    mode: document.querySelector("input[name='gameMode']:checked").value
  }));
  currentBoardButton.addEventListener("click", () => command("/api/game/start-current", {
    player_color: document.querySelector("input[name='currentPlayerColor']:checked").value,
    current_actor: document.querySelector("input[name='currentBoardActor']:checked").value,
    continuation: document.querySelector("#continuationMode").value
  }));
  resumeButton.addEventListener("click", () => command("/api/game/resume", {
    current_actor: document.querySelector("input[name='resumeActor']:checked").value
  }));
  doneButton.addEventListener("click", () => command("/api/game/player-done"));
  hintButton.addEventListener("click", () => command("/api/game/hint"));
  executeEngineButton.addEventListener("click", () => command("/api/game/execute-engine"));
  retryButton.addEventListener("click", () => command("/api/game/retry"));
  stopButton.addEventListener("click", () => command("/api/game/stop"));
  // Opening a view must never detach an active or resumable game.
  TMChess.connect(render);

  window.render_game_to_text = () => JSON.stringify({
    coordinate_system: "Pikafish UCI files a-i and ranks 0-9",
    phase: latestState?.phase || null,
    current_turn: latestState?.current_turn || null,
    game_mode: latestState?.game_mode || null,
    controllers: latestState?.controllers || null,
    side_to_move: latestState?.side_to_move || null,
    message: latestState?.message || null,
    analysis: latestState?.analysis || null,
    controls: latestState?.controls || null
  });
  window.advanceTime = () => {};
})();
